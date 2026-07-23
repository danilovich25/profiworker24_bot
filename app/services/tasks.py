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
Отменённое не хоронится навсегда: появившееся у сделки дело со сроком в
будущем воскрешает напоминание (revive_from_todos) — иначе гонка «завершил
дело бота, своё завёл через пару минут» оставляла бы Telegram немым.
"""

import asyncio
import logging
import re
import time
from typing import Any

from app.config import settings
from app.db import REMINDER_PENDING, Database
from app.services import dates
from app.services.bitrix import (
    BitrixClient,
    MalformedBitrixResponse,
    call_once,
    is_server_refusal,
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


# Статус задачи Bitrix24 «завершена» (STATUS в tasks.task.*): напоминать
# больше не о чем. «Ждёт контроля» (4) завершением не считается — пинг
# полезен, пока постановщик не подтвердил результат.
TASK_STATUS_COMPLETED = "5"

# Ответы портала, означающие именно «задачи нет» (tasks.task.get по
# удалённой задаче): человекочитаемое «задача не найдена» БЕЗ оговорки про
# недоступность. Формулировки «…или недоступна» / «…or not accessible» (в
# любой локали) неоднозначны — задача может существовать, но быть закрытой
# правами, — и вместе с ACCESS_DENIED/METHOD_NOT_FOUND задачей-нет НЕ
# считаются: снятие пинга по ним хоронило бы живое напоминание. Цена
# ложного fail-open — лишний пинг по удалённой задаче, его можно отменить
# кнопкой; цена ложного «нет» — тишина навсегда.
_TASK_MISSING_RE = re.compile(r"задача не найдена|\btask not found\b", re.IGNORECASE)
# «Недоступна» в тексте ЛЮБОЙ локали или в самом коде ошибки (с
# подчёркиваниями): такой ответ допускает «нет прав», а не «удалена».
_TASK_AMBIGUOUS_RE = re.compile(
    r"недоступ|not\s+accessible|NOT_ACCESSIBLE", re.IGNORECASE
)


def _task_is_missing(error_text: str) -> bool:
    return bool(
        _TASK_MISSING_RE.search(error_text)
        and not _TASK_AMBIGUOUS_RE.search(error_text)
    )


async def get_reminder_task(bx: BitrixClient, task_id: int) -> dict[str, Any] | None:
    """Задача Bitrix24 (tasks.task.get) с полями срока и статуса.

    None — портал ЯВНО ответил, что ЗАДАЧИ НЕТ (удалена): только такой
    исход однозначен. Остальные отказы сервера и транспортные сбои
    пробрасываются — решение (fail-open) остаётся за вызывающим.
    Поля ответа tasks.task.get приходят в нижнем регистре (deadline, status).
    """
    try:
        result = await call_once(
            bx,
            "tasks.task.get",
            {"taskId": task_id, "select": ["ID", "DEADLINE", "STATUS", "UF_CRM_TASK"]},
        )
    except Exception as exc:
        if is_server_refusal(exc) and _task_is_missing(str(exc)):
            return None
        raise
    if isinstance(result, list) and not result:
        # Живой контракт портала (проверено tasks.task.get по несуществующему
        # id 23.07): на отсутствующую задачу приходит ПУСТОЙ result без
        # ошибки. Для ЧУЖОЙ задачи пустой result значил бы и «нет доступа»,
        # но сюда попадают только задачи, созданные этим же вебхуком
        # (create_reminder_task): создатель видит свою задачу, пока она
        # существует, поэтому пустой result для неё — удаление. Ветка с
        # текстом ошибки выше — запас на другие конфигурации портала.
        return None
    if not isinstance(result, dict):
        raise MalformedBitrixResponse("Bitrix вернул неверный result для tasks.task.get")
    return result


def task_deal_id(task: dict[str, Any]) -> int | None:
    """Сделка из фактической привязки задачи (UF_CRM_TASK, D_<id>) или None.

    Поле в ответе tasks.task.get приходит как ufCrmTask; учитывается только
    сделочная связь D_*, прочие типы CRM-связей привязкой заявки не считаются.
    """
    for item in task.get("ufCrmTask") or []:
        match = re.fullmatch(r"D_(\d+)", str(item))
        if match is not None:
            return int(match.group(1))
    return None


async def complete_reminder_task(bx: BitrixClient, task_id: int) -> bool:
    """Завершает задачу-напоминание (tasks.task.complete), best-effort.

    Вызывается при отмене напоминания через бота: иначе Bitrix продолжал бы
    слать свой колокольчик по отменённому. Сбой и повисший портал (дедлайн
    SYNC_CRM_DEADLINE) только логируются — Telegram-пинг уже снят, а задачу
    заказчик может закрыть и руками.
    """
    try:
        async with asyncio.timeout(SYNC_CRM_DEADLINE):
            await call_once(bx, "tasks.task.complete", {"taskId": task_id})
    except Exception:
        log.warning("Задача-напоминание %s не завершена в Bitrix24", task_id, exc_info=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Telegram-напоминания: очередь в SQLite + фоновый планировщик
# ---------------------------------------------------------------------------

# Как часто ожидающие напоминания сверяются с делами CRM (секунды). Сверка
# нужна и ДО наступления срока: «назначенную дату» в Bitrix24 переносят и на
# более раннее время, проверка только в момент отправки такую правку прозевала
# бы. Раз в интервал — один crm.activity.list на каждую ожидающую сделку.
RECONCILE_INTERVAL = 300

# Разница сроков МЕНЬШЕ этой границы — совпадение: портал хранит дедлайн с
# точностью до минуты, дёргать очередь из-за секундного дрейфа незачем.
# Ровно минута — уже перенос: «передвинул на минуту» не должен теряться.
SYNC_TOLERANCE_SECONDS = 60

# Дедлайн одного чтения дел CRM в сверке (секунды). Повисший портал не должен
# останавливать проход планировщика: один зависший crm.activity.list без
# дедлайна заморозил бы все отправки прохода (в хендлерах такое же чтение
# ограничено своим таймаутом).
SYNC_CRM_DEADLINE = 25

# Grace перевооружения: дело, чей срок пришёлся на зазор между проходами
# скана (или чью сделку случайный LIMIT отложил на проход-другой), к
# следующему проходу уже «просрочено» — жёсткий отсев терял бы его пинг
# навсегда. В пределах пары интервалов сверки поздний пинг лучше
# потерянного; всё, что старше, — по-настоящему просрочено и молчит.
REARM_OVERDUE_GRACE_SECONDS = 2 * RECONCILE_INTERVAL + SYNC_TOLERANCE_SECONDS

# Хвост «Срок: …» в тексте напоминания (его пишут _schedule_deal_reminder и
# _reschedule_reminders); при переносе срока сверкой хвост переписывается.
_DEADLINE_TAIL_RE = re.compile(r"Срок: .*$", re.S)


def _text_with_deadline(text: str, due_ts: int) -> str:
    """Текст напоминания с актуальным сроком вместо прежнего."""
    pretty = dates.format_epoch(due_ts)
    if _DEADLINE_TAIL_RE.search(text):
        return _DEADLINE_TAIL_RE.sub(f"Срок: {pretty}", text, count=1)
    return f"{text}. Срок: {pretty}" if text else f"Срок: {pretty}"


def nearest_todo(
    todos: list[dict[str, Any]], now_ts: int | None = None
) -> tuple[int, int] | None:
    """(id дела, срок-epoch) актуального дела сделки или None.

    Срок сравнивается РАЗОБРАННЫМ, а не строкой: сравнение ISO-строк с
    разными зонами врёт. Дела без валидного срока пропускаются.

    Ненаступившие дела (с запасом SYNC_TOLERANCE_SECONDS) важнее просроченных:
    бот не завершает свои дела после отправки, и рядом с актуальным делом в
    сделке висят старые хвосты — перенос напоминания на такой хвост утащил бы
    его в прошлое и выстрелил немедленно. Из ненаступивших берётся самое
    раннее; если ненаступивших нет — самое позднее из просроченных
    (fallback: опоздавшая отправка лучше потерянной).
    """
    if now_ts is None:
        now_ts = int(time.time())
    upcoming: tuple[int, int] | None = None
    overdue: tuple[int, int] | None = None
    for todo in todos:
        due_ts = dates.bitrix_deadline_epoch(todo.get("DEADLINE"))
        if due_ts is None:
            continue
        try:
            activity_id = require_positive_id(todo.get("ID"), "crm.activity.list")
        except MalformedBitrixResponse:
            continue
        if due_ts >= now_ts - SYNC_TOLERANCE_SECONDS:
            if upcoming is None or due_ts < upcoming[1]:
                upcoming = (activity_id, due_ts)
        elif overdue is None or due_ts > overdue[1]:
            overdue = (activity_id, due_ts)
    return upcoming if upcoming is not None else overdue


async def apply_deal_todos(
    db: Database,
    reminder: dict[str, Any],
    todos: list[dict[str, Any]],
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Приводит напоминание к делам CRM; возвращает актуальную запись.

    Правила (источник правды — незавершённые дела сделки, list_deal_todos):
    - дел не осталось СОВСЕМ (завершили или удалили в CRM) — напоминание
      отменяется, возвращается None: с датой разобрались без бота. Но только
      если дело у напоминания БЫЛО (activity_id): когда дело не создалось ещё
      при заведении заявки, пустой список — не «разобрались», а «сверять не с
      чем», и гарантированный Telegram-канал живёт по сохранённому сроку;
    - дела есть, но ни один срок не разобрался — fail-open: это сбой чтения,
      а не решение заказчика, напоминание живёт по сохранённому сроку;
    - актуальное дело на другом сроке — напоминание переносится за ним
      (срок, текст, привязка к делу), в том числе на более раннее время;
    - разница сроков меньше SYNC_TOLERANCE_SECONDS — совпадение (точность
      портала — минута), без изменений.
    """
    best = nearest_todo(todos, now_ts)
    if best is None:
        if todos:
            log.warning(
                "У сделки %s есть дела, но их сроки не разобраны — напоминание "
                "id=%s живёт по сохранённому сроку",
                reminder.get("entity_id"),
                reminder["id"],
            )
            return reminder
        if not reminder.get("activity_id"):
            return reminder
        if await db.cancel_reminder(reminder["id"]):
            log.info(
                "Напоминание id=%s отменено: у сделки %s не осталось дел",
                reminder["id"],
                reminder.get("entity_id"),
            )
        return None
    activity_id, due_ts = best
    if abs(due_ts - int(reminder["due_ts"])) < SYNC_TOLERANCE_SECONDS:
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


async def _read_deal_todos(
    bx: BitrixClient, deal_id: int
) -> list[dict[str, Any]] | None:
    """Дела сделки под дедлайном SYNC_CRM_DEADLINE; None — прочитать не вышло.

    Сбой и таймаут равнозначны: вызывающий работает fail-open, по
    сохранённому сроку — молчание из-за недоступного портала хуже
    напоминания по чуть устаревшей дате.
    """
    try:
        async with asyncio.timeout(SYNC_CRM_DEADLINE):
            return await list_deal_todos(bx, deal_id)
    except Exception:
        log.warning(
            "Дела сделки %s не прочитаны — очередь живёт по сохранённым срокам",
            deal_id,
            exc_info=True,
        )
        return None


async def sync_deal_reminder(
    bx: BitrixClient,
    db: Database,
    reminder: dict[str, Any],
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Сверяет напоминание сделки с CRM; None — напоминание отменено.

    Сбой или таймаут чтения CRM отпускает напоминание без изменений:
    очередь работает по сохранённому сроку (см. _read_deal_todos).
    """
    deal_id = reminder.get("entity_id")
    if not deal_id:
        return reminder
    todos = await _read_deal_todos(bx, int(deal_id))
    if todos is None:
        return reminder
    return await apply_deal_todos(db, reminder, todos, now_ts)


async def revive_from_todos(
    db: Database,
    reminder: dict[str, Any],
    todos: list[dict[str, Any]],
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Воскрешает отменённое напоминание, если у сделки снова есть дело.

    Закрывает гонку отмены: заказчик завершил дело бота и через пару минут
    завёл в карточке своё, а между этими действиями успела пройти сверка —
    она увидела пустой список дел и отменила напоминание. Незавершённое
    НЕНАСТУПИВШЕЕ дело возвращает запись в очередь; просроченное — нет:
    срабатывание задним числом хуже тишины, с той датой уже разобрались.

    Граница «ненаступившего» — та же, что у nearest_todo (допуск
    SYNC_TOLERANCE_SECONDS: точность портала — минута). Дело в пределах
    допуска от «сейчас» побеждает будущие в nearest_todo, и отвергать его
    здесь значило бы транзиентно блокировать воскрешение при живых делах
    у сделки; его минута настала — напоминание уходит немедленно.
    От дублей защищает CAS в revive_reminder: запись не оживает, пока у
    сделки есть другое ожидающее напоминание, и не оживает по сроку, по
    которому у сделки уже есть ОТПРАВЛЕННАЯ запись (в пределах того же
    допуска) — иначе в первую минуту после отправки открытое дело бота
    воскрешало бы отменённый хвост и слало второй «⏰» задним числом.
    """
    if now_ts is None:
        now_ts = int(time.time())
    best = nearest_todo(todos, now_ts)
    if best is None:
        return None
    activity_id, due_ts = best
    if due_ts < now_ts - SYNC_TOLERANCE_SECONDS:
        return None
    text = _text_with_deadline(str(reminder["text"]), due_ts)
    if not await db.revive_reminder(
        reminder["id"], due_ts, text, activity_id, SYNC_TOLERANCE_SECONDS
    ):
        return None
    log.info(
        "Напоминание id=%s воскрешено делом id=%s (сделка %s)",
        reminder["id"],
        activity_id,
        reminder.get("entity_id"),
    )
    return {**reminder, "due_ts": due_ts, "text": text, "activity_id": activity_id}


async def rearm_deal_reminder(
    db: Database,
    reminder: dict[str, Any],
    todos: list[dict[str, Any]],
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Вооружает следующий пинг сделки после отправленного напоминания.

    Кейс заказчика: своё дело-напоминание «за 2 часа» отработало, а дело с
    самим сроком заявки ещё впереди — после отправки у сделки не оставалось
    ожидающей записи, и в момент заявки бот молчал. Здесь из актуальных дел
    по возрастанию срока выбирается первое, на которое проходит
    spawn_deal_reminder: его гарды отсекают дату только что отправленного
    пинга (и любую другую, по которой сделка уже отработала) и гонку с
    параллельно возникшим pending. Актуальны ненаступившие дела и недавно
    наступившие в пределах REARM_OVERDUE_GRACE_SECONDS (срок попал в зазор
    между проходами скана — поздний пинг лучше потерянного); давно
    просроченные пинг не вооружают: задним числом хуже тишины.
    """
    if now_ts is None:
        now_ts = int(time.time())
    deal_id = reminder.get("entity_id")
    if not deal_id:
        return None
    candidates: list[tuple[int, int]] = []
    for todo in todos:
        due_ts = dates.bitrix_deadline_epoch(todo.get("DEADLINE"))
        if due_ts is None or due_ts < now_ts - REARM_OVERDUE_GRACE_SECONDS:
            continue
        try:
            activity_id = require_positive_id(todo.get("ID"), "crm.activity.list")
        except MalformedBitrixResponse:
            continue
        candidates.append((due_ts, activity_id))
    for due_ts, activity_id in sorted(candidates):
        text = _text_with_deadline(str(reminder["text"]), due_ts)
        if await db.spawn_deal_reminder(
            int(deal_id),
            reminder["chat_id"],
            text,
            due_ts,
            activity_id,
            SYNC_TOLERANCE_SECONDS,
        ):
            log.info(
                "Очередь перевооружена делом id=%s (сделка %s): пинг на само "
                "время заявки",
                activity_id,
                deal_id,
            )
            return {**reminder, "due_ts": due_ts, "text": text, "activity_id": activity_id}
    return None


async def resync_deal_reminder(
    db: Database,
    deal_id: int,
    todos: list[dict[str, Any]],
    now_ts: int | None = None,
) -> None:
    """Сверяет напоминание сделки с УЖЕ прочитанными делами (без похода в CRM).

    Вызывается при открытии карточки заявки: дела для неё только что
    загружены, и очередь догоняет правки Bitrix24 сразу, не дожидаясь
    периодической сверки. Если ожидающего напоминания нет, отменённое
    воскрешается делом со сроком в будущем (revive_from_todos), а после
    отправленного очередь перевооружается следующим делом
    (rearm_deal_reminder) — пинг на само время заявки не ждёт сверку.
    """
    reminder = await db.pending_deal_reminder(deal_id)
    if reminder is not None:
        await apply_deal_todos(db, {**reminder, "entity_id": deal_id}, todos, now_ts)
        return
    cancelled = await db.cancelled_deal_reminder(deal_id)
    if cancelled is not None:
        await revive_from_todos(db, {**cancelled, "entity_id": deal_id}, todos, now_ts)
        return
    sent = await db.sent_deal_reminder(deal_id)
    if sent is not None:
        await rearm_deal_reminder(db, {**sent, "entity_id": deal_id}, todos, now_ts)


async def sync_task_reminder(
    bx: BitrixClient,
    db: Database,
    reminder: dict[str, Any],
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Сверяет отдельное напоминание с его задачей Bitrix24; None — снято.

    Правила (источник правды — задача tasks.task.get):
    - задача удалена или завершена — пинг отменяется: с напоминанием
      разобрались в портале;
    - крайний срок перенесли — пинг переезжает за ним (в обе стороны);
    - срок пуст или не разобрался — fail-open по сохранённому сроку: это
      не решение заказчика;
    - портал недоступен или повис (дедлайн SYNC_CRM_DEADLINE) — fail-open,
      молчание хуже пинга по чуть устаревшей дате.
    """
    task_id = reminder.get("entity_id")
    if not task_id:
        return reminder
    try:
        async with asyncio.timeout(SYNC_CRM_DEADLINE):
            task = await get_reminder_task(bx, int(task_id))
    except Exception:
        log.warning(
            "Задача %s не прочитана — пинг живёт по сохранённому сроку",
            task_id,
            exc_info=True,
        )
        return reminder
    if task is None or str(task.get("status")) == TASK_STATUS_COMPLETED:
        if await db.cancel_reminder(reminder["id"]):
            log.info(
                "Пинг id=%s снят: задача %s завершена или удалена",
                reminder["id"],
                task_id,
            )
        return None
    due_ts = dates.bitrix_deadline_epoch(task.get("deadline"))
    if due_ts is None:
        return reminder
    if abs(due_ts - int(reminder["due_ts"])) < SYNC_TOLERANCE_SECONDS:
        return reminder
    text = _text_with_deadline(str(reminder["text"]), due_ts)
    if not await db.reschedule_reminder(
        reminder["id"], due_ts, text, reminder.get("activity_id")
    ):
        # Запись уже не pending (параллельная отправка/отмена) — как есть.
        return reminder
    log.info("Пинг id=%s перенесён за задачей %s", reminder["id"], task_id)
    return {**reminder, "due_ts": due_ts, "text": text}


async def reconcile_task_reminders(
    bx: BitrixClient, db: Database, now_ts: int | None = None
) -> int:
    """Сверяет ожидающие пинги отдельных напоминаний с задачами Bitrix24.

    Тот же рисунок, что у reconcile_deal_reminders: по каждой ожидающей
    записи один tasks.task.get, ошибки одной записи не мешают остальным.
    Возвращает счёт сверенных (второй проход в счёт не входит).

    Вторым проходом проверяются недавно отменённые пинги (окно и защита от
    дублей — в db.cancelled_task_reminders): завершённую задачу могли
    переоткрыть в Bitrix24 со сроком в будущем — без воскрешения отмена
    была бы терминальной и переоткрытая задача молчала бы навсегда.
    """
    if now_ts is None:
        now_ts = int(time.time())
    count = 0
    for reminder in await db.pending_task_reminders():
        try:
            await sync_task_reminder(bx, db, reminder, now_ts)
        except Exception:
            log.exception("Сверка пинга id=%s не удалась", reminder["id"])
            continue
        count += 1
    for reminder in await db.cancelled_task_reminders():
        task_id = reminder.get("entity_id")
        try:
            async with asyncio.timeout(SYNC_CRM_DEADLINE):
                task = await get_reminder_task(bx, int(task_id))
        except Exception:
            log.warning(
                "Задача %s не прочитана — воскрешение пинга отложено",
                task_id,
                exc_info=True,
            )
            continue
        if task is None or str(task.get("status")) == TASK_STATUS_COMPLETED:
            continue
        due_ts = dates.bitrix_deadline_epoch(task.get("deadline"))
        if due_ts is None or due_ts < now_ts - SYNC_TOLERANCE_SECONDS:
            # Срока нет или он в прошлом: срабатывание задним числом хуже
            # тишины, с той датой уже разобрались.
            continue
        text = _text_with_deadline(str(reminder["text"]), due_ts)
        if await db.revive_task_reminder(
            reminder["id"], due_ts, text, SYNC_TOLERANCE_SECONDS
        ):
            log.info(
                "Пинг id=%s воскрешён: задача %s снова открыта",
                reminder["id"],
                task_id,
            )
    return count


async def reconcile_deal_reminders(
    bx: BitrixClient, db: Database, now_ts: int | None = None
) -> int:
    """Сверяет ожидающие напоминания сделок с CRM, возвращает счёт сверенных.

    Работает по каждому ожидающему напоминанию, а не по фильтру DATE_MODIFY
    сделок: перенос «назначенной даты» правит ДЕЛО, и DATE_MODIFY самой
    сделки при этом меняться не обязан — такой фильтр правки бы терял.
    Ожидающих записей единицы, поэтому цена сверки — один crm.activity.list
    на сделку. Заодно закрываются правки, сделанные пока бот был выключен
    (первый вызов — сразу при старте reminder_loop).

    Вторым проходом проверяются недавно отменённые напоминания (окно и
    защита от дублей — в db.cancelled_deal_reminders): появившееся у сделки
    дело со сроком в будущем воскрешает запись. Третьим — недавно
    ОТПРАВЛЕННЫЕ (db.sent_deal_reminders): очередь перевооружается следующим
    делом сделки, пинг на само время заявки уходит и если в момент раннего
    напоминания CRM была недоступна. В счёт сверенных эти проходы не входят.
    """
    count = 0
    for reminder in await db.pending_deal_reminders():
        try:
            await sync_deal_reminder(bx, db, reminder, now_ts)
        except Exception:
            log.exception("Сверка напоминания id=%s не удалась", reminder["id"])
            continue
        count += 1
    seen: set[int] = set()
    for reminder in await db.cancelled_deal_reminders():
        deal_id = reminder.get("entity_id")
        if not deal_id or deal_id in seen:
            continue
        seen.add(deal_id)
        try:
            todos = await _read_deal_todos(bx, int(deal_id))
            if todos is None:
                continue
            await revive_from_todos(db, reminder, todos, now_ts)
        except Exception:
            log.exception("Воскрешение напоминания id=%s не удалось", reminder["id"])
    for reminder in await db.sent_deal_reminders():
        deal_id = reminder.get("entity_id")
        if not deal_id or deal_id in seen:
            # Сделку уже вело воскрешение этого прохода: у неё либо появился
            # pending (spawn его гард отсечёт), либо дела прочитаны зря.
            continue
        seen.add(deal_id)
        try:
            todos = await _read_deal_todos(bx, int(deal_id))
            if todos is not None:
                await rearm_deal_reminder(db, reminder, todos, now_ts)
        except Exception:
            log.exception(
                "Перевооружение после напоминания id=%s не удалось", reminder["id"]
            )
        # Отметка ротации ставится после ЛЮБОЙ попытки, включая сбойную:
        # иначе сделка с постоянной ошибкой чтения (или их толпа) навсегда
        # занимала бы голову очереди и исправный хвост не сканировался бы.
        # Повтор сбойной придёт следующим кругом ротации.
        try:
            await db.mark_rearm_checked(reminder["id"])
        except Exception:
            log.exception("Отметка ротации id=%s не записана", reminder["id"])
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
        if bitrix is not None and reminder.get("kind") in ("deal", "task"):
            syncer = (
                sync_deal_reminder
                if reminder.get("kind") == "deal"
                else sync_task_reminder
            )
            try:
                synced = await syncer(bitrix, db, reminder, now_ts)
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
        # Пересверка прямо перед отправкой: записи прохода читаются пачкой,
        # и пока уходили предыдущие, эту могли отменить кнопкой или
        # перенести. Окно гонки сжимается до самой отправки; вместе с CAS
        # в mark_reminder_sent это не трогает принцип «отправить, потом
        # пометить» — упавший процесс по-прежнему шлёт дубль, а не молчит.
        fresh = await db.get_reminder(reminder["id"])
        if (
            fresh is None
            or fresh["status"] != REMINDER_PENDING
            or int(fresh["due_ts"]) != int(reminder["due_ts"])
        ):
            continue
        try:
            await bot.send_message(
                reminder["chat_id"], REMINDER_MESSAGE.format(text=reminder["text"])
            )
        except Exception:
            log.exception("Напоминание id=%s не отправлено", reminder["id"])
            await db.record_reminder_attempt(reminder["id"], REMINDER_MAX_ATTEMPTS)
            continue
        # CAS и по сроку: если параллельная сверка успела перенести запись,
        # отметка промахнётся и напоминание уйдёт по новой дате отдельно.
        marked = await db.mark_reminder_sent(reminder["id"], int(reminder["due_ts"]))
        sent += 1
        if marked and bitrix is not None and reminder.get("kind") == "deal":
            # Перевооружение сразу после пинга: следующее дело сделки (само
            # время заявки) не должно ждать периодическую сверку — зазор
            # между ранним напоминанием и сроком бывает меньше её интервала.
            # Сбой или недоступная CRM не мешают проходу: третий проход
            # reconcile_deal_reminders доберёт эту сделку по sent-строке.
            try:
                todos = await _read_deal_todos(bitrix, int(reminder["entity_id"]))
                if todos is not None:
                    await rearm_deal_reminder(db, reminder, todos, now_ts)
            except Exception:
                log.exception(
                    "Перевооружение после напоминания id=%s не удалось",
                    reminder["id"],
                )
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
                await reconcile_task_reminders(bitrix, db)
            await send_due_reminders(bot, db, bitrix=bitrix)
        except Exception:
            log.exception("Проход планировщика напоминаний не удался")
        tick += 1
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)
