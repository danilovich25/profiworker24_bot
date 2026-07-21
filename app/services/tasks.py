"""Напоминания: задачи Bitrix24 (tasks.task.add) и Telegram-планировщик.

Напоминание (intent=reminder) — не сделка: вместо карточки и записи в CRM
создаётся задача с заголовком из текста и дедлайном из распознанного срока.

Идемпотентность — по образцу сделок: ключ сообщения хранится ТЕГОМ задачи
(у задач нет пользовательского поля «из коробки», а по тегу tasks.task.list
умеет фильтровать). Перед созданием задача ищется по тегу; после
неоднозначного сбоя task.add обработчик сверяется той же find_reminder_task —
повтор не создаёт вторую задачу.

Telegram-напоминания (reminder_loop) — второй, гарантированный канал:
колокольчик веб-версии Bitrix не звучит, а mobile-push зависит от настроек
телефона, поэтому в момент срока заявки бот сам пишет сотруднику в Telegram —
это обычный push со звуком. Очередь лежит в SQLite (таблица reminders) и
переживает рестарт контейнера: цикл каждые REMINDER_CHECK_INTERVAL секунд
перечитывает наступившие сроки из базы.

Синхронизация CRM → бот: «назначенная дата» заявки живёт в ДЕЛЕ сделки
(crm.activity.todo), и заказчик переносит её прямо в карточке Bitrix24 —
правит дело бота или заводит своё. Поэтому очередь не доверяет сохранённому
сроку слепо: перед отправкой и раз в RECONCILE_INTERVAL (плюс при старте)
каждое ожидающее напоминание сверяется с незавершёнными делами сделки —
переносится за ними (в обе стороны), а когда дел не осталось, отменяется.
"""

import asyncio
import logging
import re
import time
from typing import Any

from app.config import settings
from app.db import Database
from app.services import dates
from app.services.bitrix import (
    BitrixClient,
    MalformedBitrixResponse,
    call_once,
    list_all_checked,
    list_deal_todos,
    require_positive_id,
)

log = logging.getLogger(__name__)

# Как часто планировщик проверяет наступившие напоминания (секунды).
REMINDER_CHECK_INTERVAL = 20

# Сколько раз повторять неудачную отправку, прежде чем сдаться: без предела
# заблокированный чат заставлял бы планировщик долбиться вечно.
REMINDER_MAX_ATTEMPTS = 5

REMINDER_MESSAGE = "⏰ Напоминание: {text}"


def _key_tag(key: str) -> str:
    """Тег задачи из ключа идемпотентности (инъективно для знака chat_id).

    Только буквы, цифры и дефис: спецсимволы ключа («msg:1:100») портал в
    теге мог бы порезать, и сверка перестала бы находить задачу. Минус
    кодируется буквой («-123» → «n123»), а не схлопывается в разделитель:
    иначе msg:123:7 (приват) и msg:-123:7 (группа) с одинаковым message_id
    давали бы один тег, и два разных напоминания делили бы одну задачу.
    В самих ключах (msg:/fwd:/rem:/cb:, см. app/middlewares/dedup.py) минус
    встречается только в отрицательных ID, поэтому замена однозначна.
    """
    encoded = key.lower().replace("-", "n")
    return "tg-" + re.sub(r"[^0-9a-zа-яё]+", "-", encoded).strip("-")


def _extract_task_id(result: Any) -> int:
    """Достаёт id из штатного результата parser-а tasks.task.add."""
    if not isinstance(result, dict) or "id" not in result:
        raise MalformedBitrixResponse(
            "Bitrix вернул неверный result для tasks.task.add"
        )
    return require_positive_id(result["id"], "tasks.task.add")


async def create_reminder_task(
    bx: BitrixClient,
    title: str,
    deadline: str | None = None,
    responsible_id: int | None = None,
    deal_id: int | None = None,
    key: str | None = None,
) -> int:
    """Создаёт задачу-напоминание, при необходимости привязывает к сделке.

    Ответственный по умолчанию — settings.bitrix_responsible_id: push о
    задаче должен уходить пользователю заказчика, а не владельцу вебхука.
    """
    if responsible_id is None:
        responsible_id = settings.bitrix_responsible_id
    fields: dict[str, Any] = {"TITLE": title[:255], "RESPONSIBLE_ID": responsible_id}
    if deadline:
        fields["DEADLINE"] = deadline
    if deal_id:
        # Привязка к сделке в формате CRM-связей задач: D_<id сделки>.
        fields["UF_CRM_TASK"] = [f"D_{deal_id}"]
    if key:
        # Ключ идемпотентности — тегом: по нему задача находится сверкой.
        fields["TAGS"] = [_key_tag(key)]
    result = await call_once(bx, "tasks.task.add", {"fields": fields})
    task_id = _extract_task_id(result)
    log.info("Создана задача-напоминание id=%s", task_id)
    return task_id


async def find_reminder_task(bx: BitrixClient, key: str) -> int | None:
    """Задача с тегом-ключом или None — сверка идемпотентности напоминаний."""
    rows = await list_all_checked(
        bx,
        "tasks.task.list", {"filter": {"TAG": _key_tag(key)}, "select": ["ID"]}
    )
    return require_positive_id(rows[0]["id"], "tasks.task.list") if rows else None


# ---------------------------------------------------------------------------
# Telegram-напоминания: очередь в SQLite + фоновый планировщик
# ---------------------------------------------------------------------------

# Как часто ожидающие напоминания сверяются с делами CRM (секунды). Сверка
# нужна и ДО наступления срока: «назначенную дату» в Bitrix24 переносят и на
# более раннее время, проверка только в момент отправки такую правку прозевала
# бы. Раз в интервал — один crm.activity.list на каждую ожидающую сделку.
RECONCILE_INTERVAL = 300

# Разница сроков, которую сверка считает совпадением: портал хранит дедлайн
# с точностью до минуты, дёргать очередь из-за секундного дрейфа незачем.
SYNC_TOLERANCE_SECONDS = 60

# Хвост «Срок: …» в тексте напоминания (его пишут _schedule_deal_reminder и
# _reschedule_reminders); при переносе срока сверкой хвост переписывается.
_DEADLINE_TAIL_RE = re.compile(r"Срок: .*$", re.S)


def _text_with_deadline(text: str, due_ts: int) -> str:
    """Текст напоминания с актуальным сроком вместо прежнего."""
    pretty = dates.format_epoch(due_ts)
    if _DEADLINE_TAIL_RE.search(text):
        return _DEADLINE_TAIL_RE.sub(f"Срок: {pretty}", text, count=1)
    return f"{text}. Срок: {pretty}" if text else f"Срок: {pretty}"


def _nearest_todo(todos: list[dict[str, Any]]) -> tuple[int, int] | None:
    """(id дела, срок-epoch) ближайшего по времени дела или None.

    Ближайшее выбирается по РАЗОБРАННОМУ сроку, а не по строке: сравнение
    ISO-строк с разными зонами врёт. Дела без валидного срока пропускаются.
    """
    best: tuple[int, int] | None = None
    for todo in todos:
        due_ts = dates.bitrix_deadline_epoch(todo.get("DEADLINE"))
        if due_ts is None:
            continue
        try:
            activity_id = require_positive_id(todo.get("ID"), "crm.activity.list")
        except MalformedBitrixResponse:
            continue
        if best is None or due_ts < best[1]:
            best = (activity_id, due_ts)
    return best


async def apply_deal_todos(
    db: Database, reminder: dict[str, Any], todos: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Приводит напоминание к делам CRM; возвращает актуальную запись.

    Правила (источник правды — незавершённые дела сделки, list_deal_todos):
    - дел со сроком не осталось (завершили или удалили в CRM) — напоминание
      отменяется, возвращается None: с датой разобрались без бота;
    - ближайшее дело на другом сроке — напоминание переносится за ним
      (срок, текст, привязка к делу), в том числе на более раннее время;
    - срок совпадает с точностью до SYNC_TOLERANCE_SECONDS — без изменений.
    """
    best = _nearest_todo(todos)
    if best is None:
        if await db.cancel_reminder(reminder["id"]):
            log.info(
                "Напоминание id=%s отменено: у сделки %s не осталось дел со сроком",
                reminder["id"],
                reminder.get("entity_id"),
            )
        return None
    activity_id, due_ts = best
    if abs(due_ts - int(reminder["due_ts"])) <= SYNC_TOLERANCE_SECONDS:
        return reminder
    text = _text_with_deadline(str(reminder["text"]), due_ts)
    if not await db.reschedule_reminder(reminder["id"], due_ts, text, activity_id):
        # Запись уже не pending (параллельная правка/отправка) — как есть.
        return reminder
    log.info(
        "Напоминание id=%s перенесено за делом id=%s (сделка %s)",
        reminder["id"],
        activity_id,
        reminder.get("entity_id"),
    )
    return {**reminder, "due_ts": due_ts, "text": text, "activity_id": activity_id}


async def sync_deal_reminder(
    bx: BitrixClient, db: Database, reminder: dict[str, Any]
) -> dict[str, Any] | None:
    """Сверяет напоминание сделки с CRM; None — напоминание отменено.

    Сбой чтения CRM отпускает напоминание без изменений: очередь работает по
    сохранённому сроку — молчание из-за недоступного портала хуже напоминания
    по чуть устаревшей дате.
    """
    deal_id = reminder.get("entity_id")
    if not deal_id:
        return reminder
    try:
        todos = await list_deal_todos(bx, int(deal_id))
    except Exception:
        log.warning(
            "Дела сделки %s не прочитаны — напоминание живёт по сохранённому сроку",
            deal_id,
            exc_info=True,
        )
        return reminder
    return await apply_deal_todos(db, reminder, todos)


async def reconcile_deal_reminders(bx: BitrixClient, db: Database) -> int:
    """Сверяет ВСЕ ожидающие напоминания сделок с CRM, возвращает счёт.

    Работает по каждому ожидающему напоминанию, а не по фильтру DATE_MODIFY
    сделок: перенос «назначенной даты» правит ДЕЛО, и DATE_MODIFY самой
    сделки при этом меняться не обязан — такой фильтр правки бы терял.
    Ожидающих записей единицы, поэтому цена сверки — один crm.activity.list
    на сделку. Заодно закрываются правки, сделанные пока бот был выключен
    (первый вызов — сразу при старте reminder_loop).
    """
    count = 0
    for reminder in await db.pending_deal_reminders():
        try:
            await sync_deal_reminder(bx, db, reminder)
        except Exception:
            log.exception("Сверка напоминания id=%s не удалась", reminder["id"])
            continue
        count += 1
    return count


async def send_due_reminders(
    bot: Any, db: Database, now_ts: int | None = None, bitrix: BitrixClient | None = None
) -> int:
    """Один проход планировщика: шлёт наступившие напоминания, возвращает счёт.

    Перед отправкой напоминание сделки сверяется с CRM (sync_deal_reminder):
    перенесённый в Bitrix24 срок переносит и отправку, завершённое дело её
    отменяет, а сбой сверки не блокирует отправку по сохранённому сроку.

    Порядок «отправить, потом пометить» осознанный: упасть между отправкой и
    отметкой может только процесс целиком, и после рестарта напоминание
    уйдёт второй раз — дубль лучше молчания, пропустить срок нельзя. Сбой
    отправки считается попыткой; после REMINDER_MAX_ATTEMPTS напоминание
    помечается failed и больше не трогается.
    """
    if now_ts is None:
        now_ts = int(time.time())
    sent = 0
    for reminder in await db.due_reminders(now_ts):
        if bitrix is not None and reminder.get("kind") == "deal":
            try:
                synced = await sync_deal_reminder(bitrix, db, reminder)
            except Exception:
                log.exception(
                    "Сверка напоминания id=%s не удалась — шлю по сохранённому сроку",
                    reminder["id"],
                )
                synced = reminder
            if synced is None:
                continue
            reminder = synced
            if int(reminder["due_ts"]) > now_ts:
                # Срок уехал в будущее — напоминание подождёт нового момента.
                continue
        try:
            await bot.send_message(
                reminder["chat_id"], REMINDER_MESSAGE.format(text=reminder["text"])
            )
        except Exception:
            log.exception("Напоминание id=%s не отправлено", reminder["id"])
            await db.record_reminder_attempt(reminder["id"], REMINDER_MAX_ATTEMPTS)
            continue
        await db.mark_reminder_sent(reminder["id"])
        sent += 1
    return sent


async def reminder_loop(bot: Any, db: Database, bitrix: BitrixClient | None = None) -> None:
    """Фоновый цикл Telegram-напоминаний.

    Очередь читается из SQLite на каждом проходе, поэтому рестарт контейнера
    ничего не теряет: неотправленные напоминания уйдут после подъёма. Раз в
    RECONCILE_INTERVAL — и сразу при старте — очередь сверяется с делами CRM
    (см. reconcile_deal_reminders). Ошибка одного прохода (недоступная база,
    сеть) не роняет цикл.
    """
    reconcile_every = max(1, RECONCILE_INTERVAL // REMINDER_CHECK_INTERVAL)
    tick = 0
    while True:
        try:
            if bitrix is not None and tick % reconcile_every == 0:
                await reconcile_deal_reminders(bitrix, db)
            await send_due_reminders(bot, db, bitrix=bitrix)
        except Exception:
            log.exception("Проход планировщика напоминаний не удался")
        tick += 1
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)
