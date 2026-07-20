"""Голосовые сообщения: скачать ogg, распознать, дальше — по состоянию FSM.

Распознанный текст показывается пользователю («Распознал: …») и идёт ТЕМ ЖЕ
путём, что и текстовое сообщение: голосовой ответ в пошаговом опроснике
отвечает на заданный вопрос (а не начинает новую заявку, стирая собранные
поля), голос на вопросе о телефоне работает как текстовый ответ (голый
номер / «нет» / новая фраза с переразбором без повторного вопроса), голос
в ожидании поискового запроса выполняет поиск. Вне этих состояний текст
начинает обычный поток заявки (handle_order_text).

Состояние FSM и уникальный fence фиксируются В МОМЕНТ получения голосового,
до скачивания и распознавания. Если параллельный текст успел изменить диалог,
поздний результат STT отбрасывается: старое голосовое не может стереть новую
заявку или записать ответ уже в другое поле.

Лимиты SpeechKit (30 секунд / 1 МБ) проверяются ДО скачивания файла:
сначала по метаданным апдейта, затем по размеру из getFile — отсутствующий
в апдейте размер не считается нулём, а само скачивание жёстко обрезается
лимитом. Любой сбой — скачивание, сеть, распознавание, тишина — отвечает
мягкой подсказкой и не роняет апдейт.
"""

import io
import logging
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.db import Database
from app.handlers.edit import EDIT_IN_PROGRESS, DealEditFlow, edit_value_step
from app.handlers.messages import (
    OrderFlow,
    ask_category_text_step,
    ask_phone_step,
    form_category_text_step,
    form_deadline_step,
    form_name_step,
    form_phone_step,
    form_problem_step,
    form_source_text_step,
    handle_order_text,
)
from app.handlers.search import SearchFlow, handle_search_query
from app.services import speech
from app.services.bitrix import BitrixClient

log = logging.getLogger("bot.voice")

router = Router(name="voice")

VOICE_TOO_LONG = (
    "Голосовое длиннее 30 секунд (или больше 1 МБ) распознать не могу. "
    "Пришлите покороче или отправьте заявку текстом."
)

VOICE_NOT_RECOGNIZED = "Не разобрал голосовое, пришлите заявку текстом, пожалуйста."

RECOGNIZED_TEMPLATE = "Распознал: «{text}»"

VOICE_STATE_CHANGED = (
    "Пока распознавал голосовое, диалог уже изменился. Отправьте ответ ещё раз."
)


class VoiceTooBig(Exception):
    """Поток голосового превысил лимит распознавания во время скачивания."""


async def _download_capped(bot: Bot, file_path: str, limit: int) -> bytes:
    """Скачивает файл Telegram, читая не больше limit байт.

    Заявленному размеру не доверяем: и Voice.file_size, и getFile.file_size
    могут отсутствовать (или врать), а bot.download_file читает поток
    целиком. Здесь поток читается кусками и обрывается сразу, как только
    прочитано больше лимита, — гигантский файл не оседает в памяти.
    """
    url = bot.session.api.file_url(bot.token, file_path)
    buffer = io.BytesIO()
    stream = bot.session.stream_content(
        url=url, timeout=30, chunk_size=65536, raise_for_status=True
    )
    async for chunk in stream:
        buffer.write(chunk)
        if buffer.tell() > limit:
            # выход из async for закрывает генератор — хвост не скачивается
            raise VoiceTooBig
    return buffer.getvalue()


async def _route_by_state(
    message: Message,
    state: FSMContext,
    db: Database,
    text: str,
    bitrix: BitrixClient | None,
    dedup_key: str,
    current: str | None,
) -> None:
    """Отправляет распознанный текст в обработчик состояния current.

    current — состояние FSM, зафиксированное при ПОЛУЧЕНИИ голосового. Перед
    вызовом этой функции обработчик уже подтвердил, что state и fence не
    изменились за время скачивания и STT.
    """
    if current == OrderFlow.ask_phone.state:
        await ask_phone_step(
            message, state, db, text=text, bitrix=bitrix, dedup_key=dedup_key
        )
        return
    if current == OrderFlow.ask_category.state:
        await ask_category_text_step(message, state, db, text)
        return
    if current == OrderFlow.form_name.state:
        await form_name_step(message, state, text)
        return
    if current == OrderFlow.form_phone.state:
        await form_phone_step(message, state, text)
        return
    if current == OrderFlow.form_category.state:
        await form_category_text_step(message, state, text)
        return
    if current == OrderFlow.form_source.state:
        await form_source_text_step(message, state, text)
        return
    if current == OrderFlow.form_problem.state:
        await form_problem_step(message, state, text)
        return
    if current == OrderFlow.form_deadline.state:
        await form_deadline_step(message, state, db, text)
        return
    if current == SearchFlow.query.state:
        await handle_search_query(message, state, bitrix, text)
        return
    if current == DealEditFlow.typing.state:
        # Голосом можно продиктовать и новое значение поля при правке.
        await edit_value_step(message, state, text)
        return
    if current == DealEditFlow.choosing.state:
        # Пока правка не сохранена, голос не должен начинать новую заявку.
        await message.answer(EDIT_IN_PROGRESS)
        return
    user_id = message.from_user.id if message.from_user else message.chat.id
    await handle_order_text(
        message, state, db, text=text, user_id=user_id, dedup_key=dedup_key, bitrix=bitrix
    )


@router.message(F.voice)
async def on_voice(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
    dedup_key: str = "",
) -> None:
    # Уникальный fence записывается вместе со снимком состояния. Любое новое
    # голосовое, clear/set_data нового flow или смена состояния делает снимок
    # устаревшим; поздний STT тогда не имеет права менять FSM.
    current_state = await state.get_state()
    voice_fence = uuid4().hex
    await state.update_data(_voice_fence=voice_fence)
    voice = message.voice
    if voice.duration > speech.MAX_DURATION_SECONDS:
        await message.answer(VOICE_TOO_LONG)
        return
    if voice.file_size is not None and voice.file_size > speech.MAX_SIZE_BYTES:
        await message.answer(VOICE_TOO_LONG)
        return

    try:
        file = await message.bot.get_file(voice.file_id)
        # Отсутствующий размер в апдейте не означает «ноль»: настоящий размер
        # сообщает getFile, и он проверяется ДО скачивания файла в память.
        if file.file_size is not None and file.file_size > speech.MAX_SIZE_BYTES:
            await message.answer(VOICE_TOO_LONG)
            return
        # Жёсткий предел на само скачивание: даже если размер не заявлен ни
        # в апдейте, ни в getFile, читается не больше лимита плюс кусок —
        # превышение обрывает поток, а не грузит весь файл ради отказа STT.
        data = await _download_capped(message.bot, file.file_path, speech.MAX_SIZE_BYTES)
        text = await speech.recognize_ogg(data)
    except VoiceTooBig:
        await message.answer(VOICE_TOO_LONG)
        return
    except speech.SpeechUnavailable:
        await message.answer(VOICE_NOT_RECOGNIZED)
        return
    except Exception:
        # Сбой скачивания файла из Telegram — та же мягкая подсказка,
        # апдейт не роняем.
        log.exception("Не удалось скачать голосовое сообщение")
        await message.answer(VOICE_NOT_RECOGNIZED)
        return
    if not text:
        await message.answer(VOICE_NOT_RECOGNIZED)
        return

    latest_data = await state.get_data()
    if (
        await state.get_state() != current_state
        or latest_data.get("_voice_fence") != voice_fence
    ):
        await message.answer(VOICE_STATE_CHANGED)
        return

    # Распознанный текст показывается пользователю: он должен видеть, что
    # именно уходит в заявку, и может поправить текстом.
    await message.answer(RECOGNIZED_TEMPLATE.format(text=text))
    latest_data = await state.get_data()
    if (
        await state.get_state() != current_state
        or latest_data.get("_voice_fence") != voice_fence
    ):
        await message.answer(VOICE_STATE_CHANGED)
        return
    await _route_by_state(message, state, db, text, bitrix, dedup_key, current_state)
