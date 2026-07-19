"""Всё, что не текст и не голосовое: фото, документы, стикеры и т.п."""

from aiogram import Router
from aiogram.types import Message

router = Router(name="fallback")


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Понимаю только текст и голосовые сообщения.")
