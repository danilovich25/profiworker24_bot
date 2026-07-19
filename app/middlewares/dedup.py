"""Защита от повторной обработки одного и того же сообщения.

Ключи дедупликации:
- fwd:<chat>:<id> — для пересланных сообщений с открытым источником
  (у forward новый message_id, поэтому обычный ключ не ловит повтор);
- fwd:u<user>:<время>:<хэш текста> — forward от пользователя: время origin
  имеет точность до секунды, поэтому в ключ добавлен контент-хэш, иначе два
  разных сообщения, отправленных в одну секунду, склеились бы в один ключ;
- fwd:hidden:<хэш текста>:<время> — forward со скрытым источником
  (MessageOriginHiddenUser / анонимный канал): исходных id нет вообще,
  ключ строится по нормализованному тексту и времени исходного сообщения;
- msg:<chat_id>:<message_id> — для обычных сообщений (ловит повторную
  доставку апдейта Telegram).

Ключи выше отсекают точный повтор доставки (первый уровень, жёсткий).
Перепечатанный заново текст приходит с новым message_id и этим слоем не
ловится: его перехватывает второй, мягкий уровень — контент-хэш текста в
окне 24 часов с кнопкой «Создать всё равно» (см. app/handlers/messages.py).
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.db import Database


def content_hash(text: str) -> str:
    """Хэш нормализованного текста для эвристики "тот же текст ещё раз"."""
    normalized = " ".join((text or "").lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def dedup_key(message: Message) -> str:
    """Строит ключ идемпотентности для сообщения."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        origin_msg_id = getattr(origin, "message_id", None)
        if chat is not None and origin_msg_id is not None:
            return f"fwd:{chat.id}:{origin_msg_id}"
        text = message.text or getattr(message, "caption", None) or ""
        date = getattr(origin, "date", None)
        stamp = int(date.timestamp()) if date is not None else 0
        user = getattr(origin, "sender_user", None)
        if user is not None:
            return f"fwd:u{user.id}:{stamp}:{content_hash(text)[:16]}"
        return f"fwd:hidden:{content_hash(text)}:{stamp}"
    return f"msg:{message.chat.id}:{message.message_id}"


class DedupMiddleware(BaseMiddleware):
    """Отсекает повторную доставку сообщения ДО хендлеров.

    Дубль ловится для любого сообщения, включая ответы на шаги FSM-опросника:
    повторно доставленный ответ «Иван» не должен второй раз двигать опрос
    вперёд. На дубль пользователь получает короткое уведомление (с номером
    заявки, если по исходному сообщению она уже создана), хендлеры не
    вызываются. Новые сообщения проходят дальше с dedup_key в data.

    Ключ захватывается со статусом «идёт обработка» и токеном владельца
    (claim_processing) и переводится в «завершено» после успешного хендлера.
    Если хендлер упал или его отменили (шатдаун), ключ освобождается —
    неудачная обработка не блокирует повторную отправку того же сообщения.
    Ключ, застрявший в «идёт обработка» без deal_id (процесс убит посреди
    обработки), по прошествии DEDUP_STALE_SECONDS перехватывается с новым
    токеном; завершение и освобождение работают только со своим токеном,
    поэтому перехваченный обработчик не трогает запись нового владельца.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        key = dedup_key(event)
        proc_token = await self.db.claim_processing(key)
        if proc_token is None:
            deal_id = await self.db.get_deal_id(key)
            if deal_id:
                await event.answer(
                    "Похоже на дубль: по такому сообщению уже "
                    f"создана заявка №{deal_id}."
                )
            else:
                await event.answer("Похоже, это сообщение я уже обрабатывал.")
            return None
        data["dedup_key"] = key
        try:
            result = await handler(event, data)
        except asyncio.CancelledError:
            # CancelledError не наследует Exception, поэтому ей нужна своя
            # ветка: без неё отмена задачи (шатдаун, перезапуск) оставляла бы
            # ключ «в обработке» на DEDUP_STALE_SECONDS. Освобождаем по своему
            # токену и обязательно пробрасываем отмену дальше.
            await self.db.unmark_processed(key, proc_token)
            raise
        except Exception:
            # Сбой обработки не должен «отравлять» заявку: освобождаем ключ,
            # чтобы то же сообщение можно было переслать снова, и пробрасываем
            # исключение дальше (его поймает глобальный @dp.error).
            await self.db.unmark_processed(key, proc_token)
            raise
        await self.db.mark_done(key, proc_token)
        return result
