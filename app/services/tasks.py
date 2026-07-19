"""Задачи-напоминания в Bitrix24 (tasks.task.add).

Напоминание (intent=reminder) — не сделка: вместо карточки и записи в CRM
создаётся задача с заголовком из текста и дедлайном из распознанного срока.

Идемпотентность — по образцу сделок: ключ сообщения хранится ТЕГОМ задачи
(у задач нет пользовательского поля «из коробки», а по тегу tasks.task.list
умеет фильтровать). Перед созданием задача ищется по тегу; после
неоднозначного сбоя task.add обработчик сверяется той же find_reminder_task —
повтор не создаёт вторую задачу.
"""

import logging
import re
from typing import Any

from app.services.bitrix import (
    BitrixClient,
    MalformedBitrixResponse,
    call_once,
    list_all_checked,
    require_positive_id,
)

log = logging.getLogger(__name__)

# Ответственный по задаче: пользователь, от имени которого выдан входящий
# вебхук (на портале заказчика это пользователь id=1).
REMINDER_RESPONSIBLE_ID = 1


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
    responsible_id: int = REMINDER_RESPONSIBLE_ID,
    deal_id: int | None = None,
    key: str | None = None,
) -> int:
    """Создаёт задачу-напоминание, при необходимости привязывает к сделке."""
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
