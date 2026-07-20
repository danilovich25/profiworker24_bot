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
"""

import asyncio
import logging
import re
import time
from typing import Any

from app.config import settings
from app.db import Database
from app.services.bitrix import (
    BitrixClient,
    MalformedBitrixResponse,
    call_once,
    list_all_checked,
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


async def send_due_reminders(bot: Any, db: Database, now_ts: int | None = None) -> int:
    """Один проход планировщика: шлёт наступившие напоминания, возвращает счёт.

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


async def reminder_loop(bot: Any, db: Database) -> None:
    """Фоновый цикл Telegram-напоминаний.

    Очередь читается из SQLite на каждом проходе, поэтому рестарт контейнера
    ничего не теряет: неотправленные напоминания уйдут после подъёма. Ошибка
    одного прохода (недоступная база, сеть) не роняет цикл.
    """
    while True:
        try:
            await send_due_reminders(bot, db)
        except Exception:
            log.exception("Проход планировщика напоминаний не удался")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)
