"""Ограничение частоты сообщений: 10 сообщений за 60 секунд на пользователя.

Скользящее окно. При превышении пользователь получает одно предупреждение,
дальнейшие сообщения в том же окне молча игнорируются. Нажатия inline-кнопок
(callback) под лимит не попадают — мидлварь вешается только на Message.
"""

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

DEFAULT_LIMIT = 10
DEFAULT_WINDOW = 60.0


class SlidingWindowLimiter:
    """Счётчик событий в скользящем окне по ключу (Telegram ID)."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window: float = DEFAULT_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int, now: float | None = None) -> bool:
        """Регистрирует событие. Возвращает True, если лимит не превышен."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit: int = DEFAULT_LIMIT, window: float = DEFAULT_WINDOW) -> None:
        self.limiter = SlidingWindowLimiter(limit, window)
        self._warned: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        user_id = event.from_user.id
        if self.limiter.hit(user_id):
            self._warned.discard(user_id)
            return await handler(event, data)

        if user_id not in self._warned:
            self._warned.add(user_id)
            await event.answer("Слишком часто, подождите минуту.")
        return None
