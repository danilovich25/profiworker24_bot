"""Структурное логирование входящих сообщений без персональных данных.

Телефоны в тексте маскируются даже на уровне DEBUG: в логах не должно
оставаться номеров клиентов.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

log = logging.getLogger("bot.updates")

_PHONE_RE = re.compile(r"\+?\d[\d\-() ]{8,}\d")


def mask_phones(text: str) -> str:
    """Заменяет похожие на телефон последовательности на маску +7912***67."""

    def _mask(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group())
        return match.group()[0] + digits[:3] + "***" + digits[-2:]

    return _PHONE_RE.sub(_mask, text)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            content = "voice" if event.voice else ("text" if event.text else "other")
            log.info(
                "message: chat=%s user=%s type=%s",
                event.chat.id,
                event.from_user.id if event.from_user else "-",
                content,
            )
            if event.text:
                log.debug("text: %s", mask_phones(event.text))
        return await handler(event, data)
