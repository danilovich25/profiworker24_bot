"""Последовательная обработка событий чата без вечного кэша замков."""

from asyncio import Lock
from collections.abc import AsyncGenerator, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiogram.fsm.storage.base import BaseEventIsolation, StorageKey


@dataclass
class _LockEntry:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


class PruningEventIsolation(BaseEventIsolation):
    """Сериализует один StorageKey и удаляет замок после последнего ожидающего."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, _LockEntry] = {}

    @property
    def lock_count(self) -> int:
        """Количество активных ключей; используется в диагностике и тестах."""
        return len(self._locks)

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = _LockEntry()
            self._locks[key] = entry
        # До следующего await выполнение атомарно в рамках event loop: новый
        # ожидающий успевает увеличить счётчик до возможной очистки владельцем.
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(key) is entry:
                del self._locks[key]

    async def close(self) -> None:
        self._locks.clear()
