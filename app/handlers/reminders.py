"""Отдельные напоминания: кнопка «Напоминание», список, привязка, отмена.

Сотрудник пишет или проговаривает любую дату и время плюс любой текст
(«напомни 23 июля в 8 позвонить заказчику»). Запись зеркалится задачей
Bitrix24 — тем же идемпотентным путём, что и intent=reminder в свободном
тексте (_create_reminder), — а Telegram-пинг встаёт в очередь бота. Перенос
срока задачи в портале переносит пинг (sync_task_reminder), отмена через
бота завершает и задачу.

Напоминание можно привязать к заявке: после текста с датой бот спрашивает,
к какой (номер, название/организация, телефон, «последняя») или это обычное
напоминание. Привязку можно назвать и сразу в тексте («к последней заявке
завтра в 8 позвонить») — разбор детерминированный (services/binding).
Привязанная задача связывается со сделкой (UF_CRM_TASK), пинг и список
«Мои напоминания» называют заявку.

Кнопка открывает ForceReply-ввод (состояние ReminderFlow.query): дата и
текст разбираются моделью, срок нормализуется детерминированным парсером
(resolve_deadline) — относительные обороты и месяцы словами считает код.
Без модели работает запасной путь: срок из parse_human_date, текст как есть.
"""

import asyncio
import logging
import re
import time
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
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
from app.handlers.messages import (
    REMIND_SCHEDULED_DEAL,
    _create_reminder,
    deal_binding_label,
    find_ref_deals,
)
from app.handlers.search import (
    ACTIVE_ORDER_STATES,
    ACTIVE_ORDER_WARNING,
    LIST_LIMIT,
    MENU_BUTTON_STATES,
    SEARCH_DEADLINE,
    clean_search_query,
    on_find,
    on_last,
    stem_search_query,
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
from app.services import binding, dates, llm
from app.services.binding import BindingRef
from app.services.bitrix import BitrixClient, search_deals_by_text
from app.services.tasks import complete_reminder_task

log = logging.getLogger("bot.reminders")

router = Router(name="reminders")


class ReminderFlow(StatesGroup):
    """Шаги постановки напоминания: текст с датой, затем привязка к заявке."""

    query = State()
    binding = State()


REMIND_PROMPT = (
    "Что и когда напомнить? Напишите или продиктуйте одним сообщением, "
    "например: завтра в 8:00 позвонить заказчику. Можно сразу назвать "
    "заявку: к последней заявке, к заявке 154, к заявке по телефону 8914…"
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

BIND_BTN_LAST = "К последней заявке"

BIND_BTN_NONE = "Без привязки"

BIND_PROMPT = (
    "К какой заявке привязать напоминание? Напишите номер заявки, название, "
    "организацию или телефон, либо выберите кнопкой."
)

BIND_NOT_FOUND = (
    "Заявку не нашёл. Напишите номер, название или телефон ещё раз "
    "или нажмите «Без привязки»."
)

BIND_MANY = "Нашёл несколько заявок, выберите:"

BIND_TOO_BROAD = (
    "Совпадений слишком много. Уточните номер заявки или телефон, "
    "или нажмите «Без привязки»."
)

BIND_FAILED = (
    "Поиск заявки сейчас не работает. Попробуйте ещё раз или нажмите "
    "«Без привязки»."
)

BIND_QUERY_TOO_SHORT = (
    "Слишком короткий запрос. Назовите номер заявки, название, организацию "
    "или телефон, или нажмите «Без привязки»."
)

BIND_CONFIRM = "Похоже, это заявка (проверьте и подтвердите кнопкой):"

BIND_LAST_EMPTY = "Заявок пока нет, ставлю обычное напоминание."

BIND_LOST = (
    "Не нашёл, что напомнить: начните заново кнопкой «Напоминание»."
)

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

# Окно гонки с отправкой: пинг, чей срок наступил только что, уже может
# уходить (планировщик шлёт до отметки), отменять его поздно. Сильно
# просроченный pending — наоборот, застрял (планировщик стоял или отправка
# падает), и кнопка отмены обязана работать.
CANCEL_RACE_WINDOW_SECONDS = 60

CANCELLED_TEXT = "Напоминание отменено: {label}"

# Хвост «Срок: …» в тексте пинга дублировал бы дату в списке — срезается
# ПОСЛЕДНЕЕ вхождение: «Срок:» внутри самого текста напоминания
# («Проверить поле Срок: оплаты») — это текст, а не служебный хвост.
_TAIL_RE = re.compile(r"\.?\s*Срок:(?:(?!Срок:).)*$", re.S)


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
    # Новый ввод убивает ожидающий шаг привязки прошлого напоминания:
    # иначе его старая кнопка оставалась бы живой до записи нового pending
    # и могла бы создать уже брошенное напоминание (ревью R2).
    await state.set_state(ReminderFlow.query)
    await state.update_data(rem_pending=None)
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
    # Команда в режиме постановки напоминания закрывает режим, как и кнопка:
    # иначе следующий текст с датой молча становился бы напоминанием.
    if await state.get_state() in {ReminderFlow.query.state, ReminderFlow.binding.state}:
        await state.clear()
    await _show_reminders(message, db)


@router.message(MENU_BUTTON_STATES, F.text == BTN_MY_REMINDERS)
async def on_reminders_button(message: Message, db: Database) -> None:
    """Кнопка меню «Мои напоминания» — то же, что /reminders."""
    await _show_reminders(message, db)


def _binding_keyboard(
    nonce: str, deals: list[dict] | None = None
) -> InlineKeyboardMarkup:
    """Кнопки шага привязки: выбор из найденных заявок и постоянные действия.

    nonce вшивается в callback_data: кнопка действует только на ТО
    напоминание, к которому был задан вопрос. Старые кнопки от прошлых
    вопросов отвечают «Уже неактуально» и ничего не создают.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for deal in deals or []:
        title = str(deal.get("TITLE") or "без названия")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"№{deal['ID']} · {title}"[:60],
                    callback_data=f"rem:bind:{nonce}:{deal['ID']}",
                )
            ]
        )
    last_row = [
        InlineKeyboardButton(text=BIND_BTN_NONE, callback_data=f"rem:bind:{nonce}:none")
    ]
    if not deals:
        last_row.insert(
            0,
            InlineKeyboardButton(
                text=BIND_BTN_LAST, callback_data=f"rem:bind:{nonce}:last"
            ),
        )
    rows.append(last_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _peek_pending(state: FSMContext, nonce: str) -> dict | None:
    """Ожидающее напоминание с этим nonce, если шаг привязки ещё жив."""
    data = await state.get_data()
    pending = data.get("rem_pending")
    if isinstance(pending, dict) and pending.get("nonce") == nonce:
        return pending
    return None


# Взаимное исключение потребителей pending по FSM-ключу (чат+пользователь):
# у хранилищ aiogram нет условной записи, и чтение+затирание двумя
# конкурентными потребителями иначе прошло бы у обоих (ревью R2). Локи
# процесс-локальные — как и MemoryStorage самого бота; словарь растёт не
# больше числа сотрудников, объект лока копеечный.
_consume_locks: dict[str, asyncio.Lock] = {}


def _consume_lock(state: FSMContext) -> asyncio.Lock:
    key = f"{state.key.chat_id}:{state.key.user_id}"
    lock = _consume_locks.get(key)
    if lock is None:
        lock = _consume_locks[key] = asyncio.Lock()
    return lock


async def _consume_pending(state: FSMContext, nonce: str) -> dict | None:
    """Атомарно забирает ожидающее напоминание перед созданием.

    Проверка nonce и затирание — под локом: /cancel, второй параллельный
    выбор или новый вопрос уже сняли или заменили pending — тогда None, и
    создавать ничего нельзя. Потребление идёт непосредственно перед
    _finalize_reminder, чтобы решение, принятое во время долгого похода в
    CRM, не пережило отмену.
    """
    async with _consume_lock(state):
        pending = await _peek_pending(state, nonce)
        if pending is None:
            return None
        await state.update_data(rem_pending=None)
        return pending


async def handle_reminder_query(
    message: Message,
    state: FSMContext,
    db: Database,
    text: str,
    bitrix: BitrixClient | None,
    dedup_key: str,
) -> None:
    """Разбирает «что и когда напомнить» и ведёт к постановке напоминания.

    Контекст явный (сотрудник сам нажал «Напоминание»), поэтому intent
    модели не важен: из разбора берутся только текст (problem) и срок.
    Срок нормализуется детерминированно (resolve_deadline); без даты или с
    датой в прошлом — переспрос, состояние не сбрасывается.

    Привязка к заявке: явная фраза в тексте («к последней заявке», «к заявке
    154», «обычное напоминание») решает сразу; без неё бот спрашивает
    отдельным шагом (ReminderFlow.binding).
    """
    raw = (text or "").strip()
    clean, ref = binding.extract_inline_binding(raw)
    source = clean or raw
    problem = source
    llm_deadline = None
    try:
        orders = await llm.parse_orders(source)
    except llm.LLMUnavailable:
        orders = []
    if orders:
        parsed = orders[0]
        if (parsed.problem or "").strip():
            problem = parsed.problem.strip()
        llm_deadline = parsed.deadline
    if len(orders) > 1:
        # Несколько напоминаний в сообщении: фраза «к заявке …» могла
        # относиться к любому из них — инлайн-привязка не угадывается,
        # шаг привязки спросит явно (ревью R3).
        ref = None
    if ref is not None and ref.kind == "conflict":
        # Самоисправление («к заявке 154, нет, к 155»): не угадываем,
        # выбор задаёт явный вопрос (ревью ULTRA).
        ref = None
    now = dates.now_local()
    deadline = dates.resolve_deadline(llm_deadline, source, now)
    due_ts = dates.reminder_epoch(deadline)
    if due_ts is None:
        await message.answer(REMIND_NO_DATE, reply_markup=_prompt_markup())
        return
    if due_ts <= int(now.timestamp()):
        await message.answer(REMIND_PAST_DATE, reply_markup=_prompt_markup())
        return
    pending = {
        "problem": problem,
        "deadline": deadline,
        "key": dedup_key or f"rem:{uuid4().hex}",
        "chat_id": message.chat.id,
        "user_id": message.from_user.id if message.from_user else message.chat.id,
        "nonce": uuid4().hex[:12],
    }
    if bitrix is None:
        # CRM не подключена: вопрос о привязке бессмысленен, честный отказ
        # ответит _create_reminder (REMINDER_NO_CRM).
        await _finalize_reminder(message, state, db, bitrix, pending, None)
        return
    if ref is not None and ref.kind == "none":
        await _finalize_reminder(message, state, db, bitrix, pending, None)
        return
    if ref is not None:
        # Инлайн-ссылка: pending кладётся в FSM ДО похода в CRM, чтобы
        # /cancel и кнопки меню честно снимали его даже посреди поиска.
        await state.set_state(ReminderFlow.binding)
        await state.update_data(rem_pending=pending)
        await _resolve_binding(message, state, db, bitrix, pending["nonce"], ref)
        return
    # Вопрос отправляется ДО записи состояния: если он не ушёл, чат не
    # заперт в невидимом шаге привязки — повтор текста снова разберётся
    # как напоминание, а не как название заявки (ревью ULTRA; тот же
    # инвариант, что у опросника заявки).
    await message.answer(BIND_PROMPT, reply_markup=_binding_keyboard(pending["nonce"]))
    await state.set_state(ReminderFlow.binding)
    await state.update_data(rem_pending=pending)


async def _finalize_reminder(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None,
    pending: dict,
    deal: dict | None,
) -> None:
    """Создаёт напоминание по собранным данным (с привязкой или без)."""
    due_ts = dates.reminder_epoch(pending["deadline"])
    if due_ts is None or due_ts <= int(dates.now_local().timestamp()):
        # Пока выбирали заявку, срок успел пройти: честный переспрос даты.
        await state.set_state(ReminderFlow.query)
        await message.answer(REMIND_PAST_DATE, reply_markup=_prompt_markup())
        return
    await state.clear()
    order = ParsedOrder(
        problem=pending["problem"], deadline=pending["deadline"], intent=Intent.reminder
    )
    deal_id = None
    deal_label = None
    if deal is not None:
        deal_id = int(deal["ID"])
        deal_label = await deal_binding_label(bitrix, deal)
    created, final_label, _label_known = await _create_reminder(
        message,
        db,
        bitrix,
        order,
        key=pending["key"],
        deal_id=deal_id,
        deal_label=deal_label,
    )
    if created:
        when = dates.format_epoch(due_ts)
        reply = (
            REMIND_SCHEDULED_DEAL.format(when=when, deal=final_label)
            if final_label
            else REMIND_SCHEDULED.format(when=when)
        )
        await message.answer(reply, reply_markup=main_menu_keyboard())


async def _consume_and_finalize(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None,
    nonce: str,
    deal: dict | None,
) -> None:
    """Забирает pending по nonce и создаёт напоминание; иначе — неактуально."""
    pending = await _consume_pending(state, nonce)
    if pending is None:
        await message.answer(CANCEL_STALE)
        return
    await _finalize_reminder(message, state, db, bitrix, pending, deal)


async def _reask_binding(
    message: Message,
    state: FSMContext,
    nonce: str,
    reply: str,
    deals: list[dict] | None = None,
) -> None:
    """Переспрашивает, если шаг привязки ещё жив; после отмены — молчит.

    Живость проверяется по nonce: /cancel или новый вопрос уже сняли
    pending, и переспрос воскресил бы отменённый сотрудником шаг.
    """
    if await _peek_pending(state, nonce) is None:
        await message.answer(CANCEL_STALE)
        return
    await state.set_state(ReminderFlow.binding)
    await message.answer(reply, reply_markup=_binding_keyboard(nonce, deals))


async def _find_deals(
    bitrix: BitrixClient, ref: BindingRef
) -> tuple[list[dict], bool, bool]:
    """Заявки по ссылке: (совпадения, обрезана ли выборка, неточный матч).

    Неточный матч (fuzzy) — совпадение нашлось только основой последнего
    слова («Ромашке» → «Ромашк»): такое НЕ привязывается молча, а требует
    подтверждения кнопкой (ревью ULTRA).
    """
    if ref.kind == "text":
        # Точный ярус — ТОЛЬКО полный ответ как есть: любая очистка может
        # выбросить смысловое слово («Телефон доверия» → «доверия»), и её
        # совпадения не имеют права привязываться молча (ревью ULTRA-3).
        raw_query = (ref.value or "").strip()
        found = await search_deals_by_text(bitrix, raw_query)
        if found.deals or found.truncated:
            return found.deals, found.truncated, False
        # Мягкие кандидаты: без служебных слов краёв/префикса и основа
        # последнего слова. Их совпадения подтверждаются кнопкой.
        cleaned = clean_search_query(raw_query)
        core = binding.core_text_query(raw_query)
        soft = [
            candidate
            for candidate in dict.fromkeys(
                [cleaned, core, clean_search_query(core) if core else ""]
            )
            if candidate and candidate != raw_query
        ]
        stem_source = clean_search_query(core) or cleaned
        stem = stem_search_query(stem_source) if stem_source else None
        if stem is not None and stem != raw_query and stem not in soft:
            soft.append(stem)
        for candidate in soft:
            found = await search_deals_by_text(bitrix, candidate)
            if found.deals or found.truncated:
                return found.deals, found.truncated, True
        return [], False, False
    deals, truncated = await find_ref_deals(bitrix, ref)
    return deals, truncated, False


async def _resolve_binding(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient,
    nonce: str,
    ref: BindingRef,
) -> None:
    """Ищет заявку и создаёт напоминание либо уточняет выбор."""
    if ref.kind == "text" and binding.is_vague_query(ref.value or ""):
        # Односимвольный или чисто служебный ответ: широкий поиск по нему
        # мог бы молча привязать к случайному совпадению (ревью R4).
        await _reask_binding(message, state, nonce, BIND_QUERY_TOO_SHORT)
        return
    try:
        async with asyncio.timeout(SEARCH_DEADLINE):
            deals, truncated, fuzzy = await _find_deals(bitrix, ref)
    except Exception:
        log.exception("Поиск заявки для привязки напоминания не удался")
        await _reask_binding(message, state, nonce, BIND_FAILED)
        return
    if truncated:
        await _reask_binding(message, state, nonce, BIND_TOO_BROAD)
        return
    if len(deals) == 1 and not fuzzy:
        await _consume_and_finalize(message, state, db, bitrix, nonce, deals[0])
        return
    if len(deals) == 1:
        # Совпадение только по основе слова: показываем и ждём кнопку.
        line = f"№{deals[0]['ID']} · {str(deals[0].get('TITLE') or 'без названия')}"
        await _reask_binding(
            message, state, nonce, "\n".join([BIND_CONFIRM, line]), deals
        )
        return
    if not deals:
        if ref.kind == "last":
            # Заявок в CRM вообще нет: привязывать не к чему, ставим обычное.
            pending = await _consume_pending(state, nonce)
            if pending is None:
                await message.answer(CANCEL_STALE)
                return
            await message.answer(BIND_LAST_EMPTY)
            await _finalize_reminder(message, state, db, bitrix, pending, None)
            return
        await _reask_binding(message, state, nonce, BIND_NOT_FOUND)
        return
    shown = deals[:LIST_LIMIT]
    lines = [BIND_MANY] + [
        f"№{deal['ID']} · {str(deal.get('TITLE') or 'без названия')}" for deal in shown
    ]
    await _reask_binding(message, state, nonce, "\n".join(lines), shown)


async def handle_binding_answer(
    message: Message,
    state: FSMContext,
    db: Database,
    text: str,
    bitrix: BitrixClient | None,
) -> None:
    """Разбирает ответ на вопрос «к какой заявке привязать?»."""
    data = await state.get_data()
    pending = data.get("rem_pending")
    if not isinstance(pending, dict) or not pending.get("nonce"):
        # Данные шага потеряны (например, рестарт со сменой хранилища FSM).
        await state.clear()
        await message.answer(BIND_LOST, reply_markup=main_menu_keyboard())
        return
    nonce = str(pending["nonce"])
    ref = binding.parse_binding_answer(text)
    if ref.kind == "none" or bitrix is None:
        await _consume_and_finalize(message, state, db, bitrix, nonce, None)
        return
    await _resolve_binding(message, state, db, bitrix, nonce, ref)


# Оба шага постановки напоминания: ввод текста и выбор привязки.
REMINDER_FLOW_STATES = StateFilter(ReminderFlow.query, ReminderFlow.binding)


@router.message(REMINDER_FLOW_STATES, Command("cancel"))
async def on_reminder_flow_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(REMIND_CANCELLED_FLOW, reply_markup=main_menu_keyboard())


@router.message(REMINDER_FLOW_STATES, F.text.in_({BTN_FIND, LEGACY_BTN_FIND}))
async def on_find_during_reminder(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    """Кнопка «Найти» уводит в поиск, а не становится текстом напоминания."""
    await state.clear()
    await on_find(message, state, bitrix)


@router.message(REMINDER_FLOW_STATES, F.text.in_({BTN_LAST, LEGACY_BTN_LAST}))
async def on_last_during_reminder(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    await state.clear()
    await on_last(message, state, bitrix)


@router.message(REMINDER_FLOW_STATES, F.text == BTN_MY_REMINDERS)
async def on_reminders_during_reminder(
    message: Message, state: FSMContext, db: Database
) -> None:
    await state.clear()
    await _show_reminders(message, db)


@router.message(REMINDER_FLOW_STATES, F.text == BTN_REMIND)
async def on_remind_again(message: Message, state: FSMContext) -> None:
    """Кнопка «Напоминание» в открытом режиме начинает ввод заново."""
    await on_remind(message, state)


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


@router.message(ReminderFlow.binding, F.text, ~F.text.startswith("/"))
async def on_binding_answer(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    await handle_binding_answer(message, state, db, message.text or "", bitrix)


@router.callback_query(F.data.startswith("rem:bind:"))
async def on_bind_choice(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    """Кнопки шага привязки: конкретная заявка, «последняя», «без привязки».

    Формат callback_data: rem:bind:<nonce>:<действие>. Кнопка живёт, пока в
    FSM лежит ожидающее напоминание с ТЕМ ЖЕ nonce, того же сотрудника и
    чата: старая кнопка от прошлого вопроса, чужое нажатие или кнопка без
    nonce (до обновления) отвечают «Уже неактуально» и ничего не создают.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer(CANCEL_STALE)
        return
    _, _, nonce, action = parts
    if await state.get_state() != ReminderFlow.binding.state:
        # Шаг привязки уже покинут (рестарт ввода, /find, /cancel): кнопка
        # мертва, даже если pending ещё не перезаписан (ревью R2).
        await callback.answer(CANCEL_STALE)
        return
    data = await state.get_data()
    pending = data.get("rem_pending")
    message = callback.message
    if (
        not isinstance(pending, dict)
        or message is None
        or pending.get("nonce") != nonce
        or pending.get("chat_id") != message.chat.id
        or (callback.from_user and pending.get("user_id") != callback.from_user.id)
    ):
        await callback.answer(CANCEL_STALE)
        return
    if action == "none" or bitrix is None:
        await callback.answer()
        await _consume_and_finalize(message, state, db, bitrix, nonce, None)
        return
    if action == "last":
        await callback.answer()
        await _resolve_binding(message, state, db, bitrix, nonce, BindingRef("last"))
        return
    if action.isdecimal():
        await callback.answer()
        await _resolve_binding(
            message, state, db, bitrix, nonce, BindingRef("deal_id", action)
        )
        return
    await callback.answer(CANCEL_STALE)


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
    now = int(time.time())
    # Только что наступивший срок не отменяется: планировщик шлёт до
    # отметки, и «отмена» в этот момент рапортовала бы успех, завершала
    # задачу Bitrix, а сообщение всё равно приходило. Застрявший сильно
    # просроченный pending (планировщик стоял, отправка падает) отменяется.
    racing = (
        row is not None
        and row["due_ts"] <= now < row["due_ts"] + CANCEL_RACE_WINDOW_SECONDS
    )
    if (
        row is None
        or row["kind"] != "task"
        or row["chat_id"] != chat_id
        or row["status"] != REMINDER_PENDING
        or racing
    ):
        await callback.answer(CANCEL_STALE)
        return
    if not await db.dismiss_reminder(row["id"]):
        # Гонка: пинг успел отправиться или его уже отменили.
        await callback.answer(CANCEL_STALE)
        return
    if bitrix is not None and row.get("entity_id"):
        await complete_reminder_task(bitrix, int(row["entity_id"]))
    await callback.answer(CANCEL_DONE)
    if callback.message is not None:
        await callback.message.answer(CANCELLED_TEXT.format(label=_reminder_label(row)))
