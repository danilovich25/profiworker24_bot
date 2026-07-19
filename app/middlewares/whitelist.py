"""Ограничение доступа по списку разрешённых Telegram ID (ALLOWED_TG_IDS).

Пустой список — доступ ЗАКРЫТ для всех (fail-closed): бот на проде по
умолчанию никого не пускает, пока не заполнен ALLOWED_TG_IDS. Открыть бота
всем можно только явным флагом ALLOW_ALL_USERS=true (staging/отладка),
см. docs/deploy.md. Постороннему пользователю бот отвечает один раз в сутки,
остальные его сообщения молча игнорирует и не передаёт дальше по цепочке
(в том числе не тратит запросы к нейросети).

Отметка "уже отвечали сегодня" хранится в той же таблице processed
(ключ whitelist_deny:<user_id>:<день>), так что ограничение переживает
перезапуск бота.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings
from app.db import Database

log = logging.getLogger("bot.whitelist")

DENY_TEXT = (
    "Это личный рабочий бот компании. "
    "Доступа нет — обратитесь к администратору."
)


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает только пользователей из списка разрешённых.

    allowed_ids=None / allow_all=None означают "брать актуальные значения
    из настроек" (перечитываются на каждом событии, чтобы тесты и возможная
    смена настроек не требовали пересборки диспетчера).
    """

    def __init__(
        self,
        db: Database,
        allowed_ids: set[int] | None = None,
        allow_all: bool | None = None,
    ) -> None:
        self.db = db
        self._allowed_ids = allowed_ids
        self._allow_all = allow_all

    @property
    def allowed_ids(self) -> set[int]:
        if self._allowed_ids is not None:
            return self._allowed_ids
        return settings.allowed_ids

    @property
    def allow_all(self) -> bool:
        if self._allow_all is not None:
            return self._allow_all
        return settings.allow_all_users

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        allowed = self.allowed_ids
        user = event.from_user
        if user is not None and user.id in allowed:
            return await handler(event, data)
        # Пустой список сам по себе никого не пускает (fail-closed);
        # открытый режим включается только явным флагом ALLOW_ALL_USERS=true.
        if not allowed and self.allow_all:
            return await handler(event, data)

        user_id = user.id if user else 0
        day = datetime.now(ZoneInfo(settings.tz)).date().isoformat()
        first_today = await self.db.try_mark_processed(f"whitelist_deny:{user_id}:{day}")
        log.info("Доступ запрещён: user=%s, ответ сегодня уже был: %s", user_id, not first_today)
        if first_today:
            if isinstance(event, Message):
                await event.answer(DENY_TEXT)
            else:
                await event.answer(DENY_TEXT, show_alert=True)
        return None
