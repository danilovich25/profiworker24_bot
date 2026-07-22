"""Отдельные напоминания: кнопка «Напоминание», список и отмена.

Напоминание не привязано к заявке: сотрудник пишет или проговаривает любую
дату и время плюс любой текст («напомни 23 июля в 8 позвонить заказчику»).
Запись зеркалится задачей Bitrix24 — тем же идемпотентным путём, что и
intent=reminder в свободном тексте (_create_reminder), — а Telegram-пинг
встаёт в очередь бота. Перенос срока задачи в портале переносит пинг
(sync_task_reminder), отмена через бота завершает и задачу.

Кнопка открывает ForceReply-ввод (состояние ReminderFlow.query): дата и
текст разбираются моделью, срок нормализуется детерминированным парсером
(resolve_deadline) — относительные обороты и месяцы словами считает код.
Без модели работает запасной путь: срок из parse_human_date, текст как есть.
"""

import logging
import re
import time
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.db import REMINDER_PENDING, Database
from app.handlers.messages import _create_reminder
from app.handlers.search import (
    ACTIVE_ORDER_STATES,
    ACTIVE_ORDER_WARNING,
    MENU_BUTTON_STATES,
    on_find,
    on_last,
)
from app.handlers.start import (
    BTN_FIND,
    BTN_LAST,
    BTN_MY_REMINDERS,
    BTN_REMIND,
    LEGACY_BTN_FIND,
    LEGACY_BTN_LAST,
    main_menu_keyboard,
)
from app.schemas import Intent, ParsedOrder
from app.services import dates, llm
from app.services.bitrix import BitrixClient
from app.services.tasks import complete_reminder_task

log = logging.getLogger("bot.reminders")

router = Router(name="reminders")


class ReminderFlow(StatesGroup):
    """Ожидание текста «что и когда напомнить» после кнопки/команды."""

    query = State()


REMIND_PROMPT = (
    "Что и когда напомнить? Напишите или продиктуйте одним сообщением, "
    "например: завтра в 8:00 позвонить заказчику."
)

REMIND_PLACEHOLDER = "Например: завтра в 8:00 позвонить заказчику"

REMIND_NO_DATE = (
    "Не понял, когда напомнить. Назовите дату и время, например: "
    "завтра в 8:00, 23 июля в 10:00, через 2 часа. И что напомнить."
)

REMIND_PAST_DATE = (
    "Это время уже прошло. Назовите дату и время в будущем, например: "
    "завтра в 8:00 позвонить заказчику."
)

REMIND_SCHEDULED = (
    "Пришлю напоминание в телеграм {when}. Посмотреть или отменить: "
    "кнопка «Мои напоминания»."
)

REMIND_CANCELLED_FLOW = "Хорошо, без напоминания."

MY_REMINDERS_TITLE = "Ваши напоминания:"

MY_REMINDERS_EMPTY = (
    "Ожидающих напоминаний нет. Чтобы поставить, нажмите «Напоминание»."
)

DEAL_REMINDERS_HINT = (
    "Напоминаниями по заявкам управляет дело в карточке сделки Битрикс24: "
    "перенесите или завершите дело там, бот подхватит."
)

CANCEL_STALE = "Уже неактуально"

CANCEL_DONE = "Отменено"

CANCELLED_TEXT = "Напоминание отменено: {label}"

# Хвост «Срок: …» в тексте пинга дублировал бы дату в списке — срезается.
_TAIL_RE = re.compile(r"\.?\s*Срок: .*$", re.S)


def _prompt_markup() -> ForceReply:
    """ForceReply: поле ввода открывается сразу, без тапа по клавиатуре."""
    return ForceReply(force_reply=True, input_field_placeholder=REMIND_PLACEHOLDER)


def _reminder_label(row: dict) -> str:
    base = _TAIL_RE.sub("", str(row["text"])).strip() or str(row["text"])
    return f"{dates.format_epoch(row['due_ts'])} — {base}"


@router.message(ACTIVE_ORDER_STATES, Command("remind", "reminders"))
@router.message(ACTIVE_ORDER_STATES, F.text.in_({BTN_REMIND, BTN_MY_REMINDERS}))
async def protect_active_order(message: Message) -> None:
    """Напоминания не рвут незавершённую заявку: сначала ответить на вопрос."""
    await message.answer(ACTIVE_ORDER_WARNING)


@router.message(Command("remind"))
async def on_remind(message: Message, state: FSMContext) -> None:
    await state.set_state(ReminderFlow.query)
    await message.answer(REMIND_PROMPT, reply_markup=_prompt_markup())


@router.message(MENU_BUTTON_STATES, F.text == BTN_REMIND)
async def on_remind_button(message: Message, state: FSMContext) -> None:
    """Кнопка меню «Напоминание» — то же, что /remind."""
    await on_remind(message, state)


async def _show_reminders(message: Message, db: Database) -> None:
    rows = await db.pending_chat_reminders(message.chat.id)
    if not rows:
        await message.answer(MY_REMINDERS_EMPTY)
        return
    lines = [MY_REMINDERS_TITLE]
    buttons: list[InlineKeyboardButton] = []
    has_deal = False
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {_reminder_label(row)}")
        if row["kind"] == "deal":
            has_deal = True
            continue
        buttons.append(
            InlineKeyboardButton(
                text=f"Отменить {index}", callback_data=f"rem:cancel:{row['id']}"
            )
        )
    if has_deal:
        lines.append(DEAL_REMINDERS_HINT)
    markup = None
    if buttons:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        )
    await message.answer("\n".join(lines), reply_markup=markup)


@router.message(Command("reminders"))
async def on_reminders(message: Message, state: FSMContext, db: Database) -> None:
    # Команда в режиме ввода напоминания закрывает режим, как и кнопка:
    # иначе следующий текст с датой молча становился бы напоминанием.
    if await state.get_state() == ReminderFlow.query.state:
        await state.clear()
    await _show_reminders(message, db)


@router.message(MENU_BUTTON_STATES, F.text == BTN_MY_REMINDERS)
async def on_reminders_button(message: Message, db: Database) -> None:
    """Кнопка меню «Мои напоминания» — то же, что /reminders."""
    await _show_reminders(message, db)


async def handle_reminder_query(
    message: Message,
    state: FSMContext,
    db: Database,
    text: str,
    bitrix: BitrixClient | None,
    dedup_key: str,
) -> None:
    """Разбирает «что и когда напомнить» и ставит напоминание.

    Контекст явный (сотрудник сам нажал «Напоминание»), поэтому intent
    модели не важен: из разбора берутся только текст (problem) и срок.
    Срок нормализуется детерминированно (resolve_deadline); без даты или с
    датой в прошлом — переспрос, состояние не сбрасывается.
    """
    raw = (text or "").strip()
    problem = raw
    llm_deadline = None
    try:
        orders = await llm.parse_orders(raw)
    except llm.LLMUnavailable:
        orders = []
    if orders:
        parsed = orders[0]
        if (parsed.problem or "").strip():
            problem = parsed.problem.strip()
        llm_deadline = parsed.deadline
    now = dates.now_local()
    deadline = dates.resolve_deadline(llm_deadline, raw, now)
    due_ts = dates.reminder_epoch(deadline)
    if due_ts is None:
        await message.answer(REMIND_NO_DATE, reply_markup=_prompt_markup())
        return
    if due_ts <= int(now.timestamp()):
        await message.answer(REMIND_PAST_DATE, reply_markup=_prompt_markup())
        return
    await state.clear()
    order = ParsedOrder(problem=problem, deadline=deadline, intent=Intent.reminder)
    key = dedup_key or f"rem:{uuid4().hex}"
    created = await _create_reminder(message, db, bitrix, order, key=key)
    if created:
        await message.answer(
            REMIND_SCHEDULED.format(when=dates.format_epoch(due_ts)),
            reply_markup=main_menu_keyboard(),
        )


@router.message(ReminderFlow.query, Command("cancel"))
async def on_reminder_flow_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(REMIND_CANCELLED_FLOW, reply_markup=main_menu_keyboard())


@router.message(ReminderFlow.query, F.text.in_({BTN_FIND, LEGACY_BTN_FIND}))
async def on_find_during_reminder(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    """Кнопка «Найти» уводит в поиск, а не становится текстом напоминания."""
    await state.clear()
    await on_find(message, state, bitrix)


@router.message(ReminderFlow.query, F.text.in_({BTN_LAST, LEGACY_BTN_LAST}))
async def on_last_during_reminder(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    await state.clear()
    await on_last(message, state, bitrix)


@router.message(ReminderFlow.query, F.text == BTN_MY_REMINDERS)
async def on_reminders_during_reminder(
    message: Message, state: FSMContext, db: Database
) -> None:
    await state.clear()
    await _show_reminders(message, db)


@router.message(ReminderFlow.query, F.text == BTN_REMIND)
async def on_remind_again(message: Message) -> None:
    """Повторное нажатие кнопки — просто напомнить формат, режим уже открыт."""
    await message.answer(REMIND_PROMPT, reply_markup=_prompt_markup())


@router.message(ReminderFlow.query, F.text, ~F.text.startswith("/"))
async def on_reminder_query(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
    dedup_key: str = "",
) -> None:
    await handle_reminder_query(
        message, state, db, message.text or "", bitrix, dedup_key
    )


@router.callback_query(F.data.startswith("rem:cancel:"))
async def on_cancel_reminder(
    callback: CallbackQuery, db: Database, bitrix: BitrixClient | None = None
) -> None:
    """Отмена отдельного напоминания кнопкой из списка.

    Снимается Telegram-пинг (CAS по pending), затем best-effort завершается
    задача Bitrix24 — иначе портал слал бы свой колокольчик по отменённому.
    Пинги сделок здесь не отменяются: ими управляет дело в CRM, и отмена
    воскресла бы первой же сверкой (revive_from_todos).
    """
    raw = (callback.data or "").rsplit(":", 1)[-1]
    row = await db.get_reminder(int(raw)) if raw.isdecimal() else None
    chat_id = callback.message.chat.id if callback.message else None
    if (
        row is None
        or row["kind"] != "task"
        or row["chat_id"] != chat_id
        or row["status"] != REMINDER_PENDING
        # Наступивший срок не отменяется: планировщик шлёт до отметки, и
        # «отмена» в этот момент рапортовала бы успех, завершала задачу
        # Bitrix, а сообщение всё равно приходило.
        or row["due_ts"] <= int(time.time())
    ):
        await callback.answer(CANCEL_STALE)
        return
    if not await db.cancel_reminder(row["id"]):
        # Гонка: пинг успел отправиться или его уже отменили.
        await callback.answer(CANCEL_STALE)
        return
    if bitrix is not None and row.get("entity_id"):
        await complete_reminder_task(bitrix, int(row["entity_id"]))
    await callback.answer(CANCEL_DONE)
    if callback.message is not None:
        await callback.message.answer(CANCELLED_TEXT.format(label=_reminder_label(row)))
