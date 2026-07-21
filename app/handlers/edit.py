"""Просмотр и правка НАЙДЕННОЙ заявки: открыть → изменить поля → сохранить.

Из списка поиска заявка открывается кнопкой «Открыть №N» (deal:open:<id>):
показывается карточка сделки из Bitrix24. Кнопка «Изменить» переводит диалог
в режим правки (DealEditFlow): поля выбираются кнопками, новые значения
приходят текстом или кнопками категории/источника, правки копятся в FSM и
записываются ОДНИМ «Сохранить» — crm.deal.update / crm.contact.update по тем
же ID. Обновляется ТА ЖЕ сделка: номер сохраняется, дублей не появляется,
старая заявка не удаляется.

Смена срока переносит и напоминания: Telegram-очередь (reminders) и дело
CRM (crm.activity.todo.update по сохранённому activity_id; если дела ещё
нет — создаётся новое). Сбой сохранения не теряет правки: они остаются в
FSM, «Сохранить» можно нажать повторно (обновления Bitrix идемпотентны).
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import Database
from app.handlers.messages import OrderFlow, category_keyboard, source_keyboard
from app.handlers.start import (
    BTN_FIND,
    BTN_LAST,
    EDIT_IN_PROGRESS,
    LEGACY_BTN_FIND,
    LEGACY_BTN_LAST,
)
from app.schemas import Category, Source
from app.services import dates
from app.services.bitrix import (
    SOURCE_ID_BY_NAME,
    SOURCE_NAME_BY_ID,
    UF_EXPENSE,
    UF_PROFIT,
    UF_SERVICE_CATEGORY,
    BitrixClient,
    create_deal_todo,
    get_contact,
    get_deal,
    list_deal_todos,
    normalize_phone,
    stage_names,
    update_contact,
    update_deal,
    update_deal_todo_deadline,
)
from app.services.tasks import nearest_todo, resync_deal_reminder

log = logging.getLogger("bot.edit")

router = Router(name="edit")

# Общий дедлайн одного похода в CRM (открытие карточки или сохранение).
EDIT_CRM_DEADLINE = 25

NO_CRM_EDIT = "CRM пока не подключена (не задан вебхук Bitrix24), правки недоступны."

DEAL_GONE = "Заявка №{deal_id} не найдена в Bitrix24 — возможно, её удалили."

EDIT_LOAD_FAILED = "Не получилось открыть заявку, попробуйте позже."

EDIT_SAVE_FAILED = (
    "Не получилось сохранить правки, попробуйте нажать «Сохранить» ещё раз."
)

EDIT_SAVED = "Заявка №{deal_id} обновлена (номер прежний, дубль не создан)."

EDIT_NO_CHANGES = "Пока ничего не изменено. Выберите поле кнопкой."

EDIT_CANCELLED = "Правки отменены, заявка осталась прежней."

# EDIT_IN_PROGRESS импортируется из handlers/start: тем же ответом защищаются
# и кнопки «Новая заявка» (включая легаси) с /new, чьи хендлеры живут там.

ACTIVE_INPUT_WARNING = (
    "Сначала завершите текущий ввод заявки или нажмите «Отмена» на вопросе."
)

# Состояния незаконченного ввода заявки: правка сделки не имеет права их
# затирать — вместе с ними пропал бы и захваченный контент-хэш текста.
# Карточка-превью сюда не входит: у неё собственные кнопки и черновик в SQLite.
_ORDER_INPUT_STATES = {
    OrderFlow.ask_phone.state,
    OrderFlow.ask_category.state,
    OrderFlow.form_name.state,
    OrderFlow.form_phone.state,
    OrderFlow.form_category.state,
    OrderFlow.form_source.state,
    OrderFlow.form_problem.state,
    OrderFlow.form_deadline.state,
}

EDIT_STALE = "Правки уже не активны. Найдите заявку заново через «Найти»."

CHOOSE_FIELD = "Что изменить в заявке №{deal_id}? Выберите поле."

NOT_A_NUMBER = "Не похоже на сумму. Пришлите число, например 5000."

NOT_A_PHONE_EDIT = "Не похоже на номер телефона. Пришлите номер, например 89141234567."

NOT_A_DEADLINE = (
    "Не понял срок. Пришлите, например, «24.07.2026 10:00», «завтра в 10:00» "
    "или «через 3 дня»."
)

EMPTY_VALUE = "Пустое значение не подойдёт, пришлите текст."

# Поля правки: callback-код -> (подпись кнопки, подсказка для текстового ввода)
FIELD_PROMPTS = {
    "name": "Пришлите новое имя клиента.",
    "phone": "Пришлите новый телефон клиента.",
    "problem": "Пришлите новое описание работ.",
    "income": "Пришлите новый доход в рублях (число).",
    "expense": "Пришлите новый расход в рублях (число).",
    "deadline": "Пришлите новый срок: «24.07.2026 10:00», «завтра в 10:00», «через 3 дня».",
}

FIELD_TITLES = {
    "name": "Имя клиента",
    "phone": "Телефон",
    "category": "Категория",
    "source": "Источник",
    "problem": "Описание",
    "income": "Доход",
    "expense": "Расход",
    "deadline": "Срок",
}


class DealEditFlow(StatesGroup):
    """Правка найденной заявки: выбор поля и ожидание нового значения."""

    choosing = State()
    typing = State()


EDIT_STATES = StateFilter(DealEditFlow.choosing, DealEditFlow.typing)

_DEADLINE_LINE_RE = re.compile(r"(?m)^Срок: .*$")


# ---------------------------------------------------------------------------
# Карточка заявки и клавиатуры
# ---------------------------------------------------------------------------


def open_deals_keyboard(deal_ids: list[int]) -> InlineKeyboardMarkup | None:
    """Кнопки «Открыть №N» под списком поиска (по 2 в ряд)."""
    if not deal_ids:
        return None
    buttons = [
        InlineKeyboardButton(text=f"Открыть №{deal_id}", callback_data=f"deal:open:{deal_id}")
        for deal_id in deal_ids
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deal_card_keyboard(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"deal:edit:{deal_id}")]
        ]
    )


def edit_fields_keyboard(has_contact: bool) -> InlineKeyboardMarkup:
    """Клавиатура выбора поля; имя и телефон доступны только с контактом."""
    codes = ["category", "source", "problem", "income", "expense", "deadline"]
    if has_contact:
        codes = ["name", "phone"] + codes
    buttons = [
        InlineKeyboardButton(text=FIELD_TITLES[code], callback_data=f"dedit:f:{code}")
        for code in codes
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(text="💾 Сохранить", callback_data="dedit:save"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="dedit:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def split_title(raw_title: object) -> tuple[str | None, str]:
    """TITLE «категория: суть» -> (категория, суть); чужой формат — только суть."""
    title = str(raw_title or "").strip()
    if ": " in title:
        maybe_category, problem = title.split(": ", 1)
        if maybe_category.strip().lower() in {c.value for c in Category}:
            return maybe_category.strip().lower(), problem.strip()
    return None, title


def _num(raw: object) -> float | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value


def _fmt_amount(value: float) -> str:
    """Сумма числом: без хвоста «.0» и без экспоненты («2.5e+06» у миллионов)."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_money(raw: object) -> str | None:
    value = _num(raw)
    if value is None:
        return None
    return f"{_fmt_amount(value)} руб."


def _deadline_from_comments(comments: object) -> str | None:
    match = re.search(r"(?m)^Срок: (.+)$", str(comments or ""))
    return match.group(1).strip() if match else None


def deal_card_text(
    deal: dict[str, Any],
    contact: dict[str, Any] | None,
    stages: dict[str, str],
    todos: list[dict[str, Any]] | None = None,
) -> str:
    """Читаемая карточка сделки для просмотра и правки.

    Срок берётся из ближайшего незавершённого ДЕЛА сделки (todos): именно там
    живёт «назначенная дата», которую заказчик правит в Bitrix24, — карточка
    обязана показывать свежее значение, а не копию из комментария. Строка
    «Срок:» комментария — только запасной вариант, когда дела прочитать не
    удалось или их нет.
    """
    deal_id = deal.get("ID")
    category, problem = split_title(deal.get("TITLE"))
    category = deal.get(UF_SERVICE_CATEGORY) or category
    source_id = str(deal.get("SOURCE_ID") or "")
    stage_id = str(deal.get("STAGE_ID") or "—")
    lines = [f"Заявка №{deal_id}"]
    if contact is not None:
        name_parts = (contact.get("NAME"), contact.get("LAST_NAME"))
        client = " ".join(str(part) for part in name_parts if part)
        lines.append(f"Клиент: {client or 'без имени'}")
        phones = contact.get("PHONE") or []
        phone = phones[0].get("VALUE") if phones and isinstance(phones[0], dict) else None
        lines.append(f"Телефон: {phone or 'не указан'}")
    else:
        lines.append("Клиент: без контакта")
    lines.append(f"Категория: {category or 'не указана'}")
    lines.append(f"Источник: {SOURCE_NAME_BY_ID.get(source_id, source_id or 'не указан')}")
    lines.append(f"Описание: {problem or 'без описания'}")
    lines.append(f"Стадия: {stages.get(stage_id, stage_id)}")
    income = _fmt_money(deal.get("OPPORTUNITY"))
    expense = _fmt_money(deal.get(UF_EXPENSE))
    profit = _fmt_money(deal.get(UF_PROFIT))
    if income:
        lines.append(f"Доход: {income}")
    if expense:
        lines.append(f"Расход: {expense}")
    if profit:
        lines.append(f"Прибыль: {profit}")
    deadline = None
    if todos:
        # «Сейчас» — чтобы просроченный хвост дел не заслонял актуальный
        # срок: показывается ближайшее ненаступившее дело.
        best = nearest_todo(todos, int(dates.now_local().timestamp()))
        if best is not None:
            deadline = dates.format_epoch(best[1])
    if deadline is None:
        deadline = _deadline_from_comments(deal.get("COMMENTS"))
    if deadline:
        lines.append(f"Срок: {deadline}")
    lines.append(f"Создана: {dates.format_bitrix_datetime(deal.get('DATE_CREATE'))}")
    return "\n".join(lines)


def _changes_summary(changes: dict[str, Any]) -> str:
    if not changes:
        return "Изменений пока нет."
    parts = []
    for code, value in changes.items():
        shown = value
        if code == "deadline":
            shown = dates.format_deadline(value)
        elif isinstance(value, float):
            # Суммы разбираются во float: без формата сотрудник видел бы
            # «Доход → 7777.0», а от миллиона — экспоненту «1.5e+06».
            shown = _fmt_amount(value)
        parts.append(f"{FIELD_TITLES[code]} → {shown}")
    return "Изменения: " + "; ".join(parts) + ".\nСохранить?"


async def _load_deal_context(bitrix: BitrixClient, deal_id: int) -> (
    tuple[
        dict[str, Any],
        dict[str, Any] | None,
        dict[str, str],
        list[dict[str, Any]] | None,
    ]
    | None
):
    """Сделка + контакт + имена стадий + дела одним походом; None — сделки нет.

    Дела (срок заявки) — best-effort: их сбой не должен прятать карточку,
    поэтому вместо списка может вернуться None, и срок возьмётся из
    комментария сделки.
    """
    deal = await get_deal(bitrix, deal_id)
    if deal is None:
        return None
    contact = None
    try:
        contact_id = int(deal.get("CONTACT_ID") or 0)
    except (TypeError, ValueError):
        contact_id = 0
    if contact_id:
        contact = await get_contact(bitrix, contact_id)
    stages = await stage_names(bitrix)
    try:
        todos = await list_deal_todos(bitrix, deal_id)
    except Exception:
        log.warning("Дела заявки №%s не прочитаны — срок из комментария", deal_id)
        todos = None
    return deal, contact, stages, todos


# ---------------------------------------------------------------------------
# Открытие карточки из поиска
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("deal:open:"))
async def on_deal_open(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    if bitrix is None:
        await callback.answer()
        await callback.message.answer(NO_CRM_EDIT)
        return
    deal_id = int((callback.data or "").rsplit(":", 1)[-1])
    await callback.answer()
    try:
        async with asyncio.timeout(EDIT_CRM_DEADLINE):
            context = await _load_deal_context(bitrix, deal_id)
    except Exception:
        log.exception("Заявка №%s не открылась", deal_id)
        await callback.message.answer(EDIT_LOAD_FAILED)
        return
    if context is None:
        await callback.message.answer(DEAL_GONE.format(deal_id=deal_id))
        return
    deal, contact, stages, todos = context
    await callback.message.answer(
        deal_card_text(deal, contact, stages, todos),
        reply_markup=deal_card_keyboard(deal_id),
    )
    if todos is not None:
        # Дела заявки только что прочитаны — очередь напоминаний догоняет
        # правки CRM сразу, не дожидаясь периодической сверки. Best-effort:
        # сбой сверки не мешает просмотру.
        try:
            await resync_deal_reminder(db, deal_id, todos)
        except Exception:
            log.exception("Напоминание заявки №%s не сверено с CRM", deal_id)


@router.callback_query(F.data.startswith("deal:edit:"))
async def on_deal_edit(
    callback: CallbackQuery, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    """«Изменить» на карточке: включает режим правки с выбором полей."""
    if bitrix is None:
        await callback.answer()
        await callback.message.answer(NO_CRM_EDIT)
        return
    if await state.get_state() in _ORDER_INPUT_STATES:
        # Незаконченный опросник/уточнение: правка не затирает его данные.
        await callback.answer()
        await callback.message.answer(ACTIVE_INPUT_WARNING)
        return
    deal_id = int((callback.data or "").rsplit(":", 1)[-1])
    await callback.answer()
    try:
        async with asyncio.timeout(EDIT_CRM_DEADLINE):
            deal = await get_deal(bitrix, deal_id)
    except Exception:
        log.exception("Заявка №%s не открылась для правки", deal_id)
        await callback.message.answer(EDIT_LOAD_FAILED)
        return
    if deal is None:
        await callback.message.answer(DEAL_GONE.format(deal_id=deal_id))
        return
    try:
        contact_id = int(deal.get("CONTACT_ID") or 0)
    except (TypeError, ValueError):
        contact_id = 0
    await state.set_state(DealEditFlow.choosing)
    await state.set_data({"deal_id": deal_id, "contact_id": contact_id, "changes": {}})
    await callback.message.answer(
        CHOOSE_FIELD.format(deal_id=deal_id),
        reply_markup=edit_fields_keyboard(bool(contact_id)),
    )


# ---------------------------------------------------------------------------
# Выбор поля и приём значений
# ---------------------------------------------------------------------------


@router.callback_query(EDIT_STATES, F.data.startswith("dedit:f:"))
async def on_field_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    field = (callback.data or "").rsplit(":", 1)[-1]
    if field not in FIELD_TITLES:
        await callback.answer()
        return
    await callback.answer()
    if field == "category":
        await callback.message.answer(
            "Выберите новую категорию:", reply_markup=category_keyboard()
        )
    elif field == "source":
        await callback.message.answer(
            "Выберите новый источник:", reply_markup=source_keyboard()
        )
    else:
        await callback.message.answer(FIELD_PROMPTS[field])
    await state.update_data(field=field)
    await state.set_state(DealEditFlow.typing)


async def _back_to_fields(message: Message, state: FSMContext) -> None:
    """Показывает накопленные правки и снова предлагает поля."""
    data = await state.get_data()
    await state.set_state(DealEditFlow.choosing)
    await message.answer(
        _changes_summary(data.get("changes") or {}),
        reply_markup=edit_fields_keyboard(bool(data.get("contact_id"))),
    )


async def _store_change(
    message: Message, state: FSMContext, field: str, value: Any
) -> None:
    data = await state.get_data()
    changes = dict(data.get("changes") or {})
    changes[field] = value
    await state.update_data(changes=changes, field=None)
    await _back_to_fields(message, state)


@router.callback_query(DealEditFlow.typing, F.data.startswith("cat:"))
async def on_edit_category(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _store_change(
        callback.message, state, "category", callback.data.split(":", 1)[1]
    )


@router.callback_query(DealEditFlow.typing, F.data.startswith("src:"))
async def on_edit_source(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _store_change(callback.message, state, "source", callback.data.split(":", 1)[1])


async def edit_value_step(message: Message, state: FSMContext, text: str) -> None:
    """Новое значение выбранного поля: проверка и запись в накопленные правки."""
    data = await state.get_data()
    field = data.get("field")
    value = (text or "").strip()
    if field in (None, "category"):
        await message.answer("Выберите категорию кнопкой ниже:", reply_markup=category_keyboard())
        return
    if field == "source":
        matched = next(
            (src.value for src in Source if src.value.lower() == value.lower()), None
        )
        if matched is None:
            await message.answer(
                "Выберите источник кнопкой ниже:", reply_markup=source_keyboard()
            )
            return
        await _store_change(message, state, "source", matched)
        return
    if field == "phone":
        phone = normalize_phone(value)
        if phone is None:
            await message.answer(NOT_A_PHONE_EDIT)
            return
        await _store_change(message, state, "phone", phone)
        return
    if field in ("income", "expense"):
        cleaned = value.replace(",", ".").replace(" ", "")
        cleaned = re.sub(r"(руб\w*|р\.?|₽)$", "", cleaned, flags=re.IGNORECASE)
        amount = _num(cleaned)
        if amount is None or amount < 0:
            await message.answer(NOT_A_NUMBER)
            return
        await _store_change(message, state, field, amount)
        return
    if field == "deadline":
        resolved = dates.parse_human_date(value, dates.now_local())
        if resolved is None and _valid_iso(value):
            resolved = value
        if resolved is None:
            await message.answer(NOT_A_DEADLINE)
            return
        await _store_change(message, state, "deadline", resolved)
        return
    if not value:
        await message.answer(EMPTY_VALUE)
        return
    await _store_change(message, state, field, value)


def _valid_iso(raw: str) -> bool:
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        return False
    return True


@router.message(DealEditFlow.typing, F.text, ~F.text.startswith("/"))
async def on_edit_value(message: Message, state: FSMContext) -> None:
    await edit_value_step(message, state, message.text or "")


_MENU_BUTTON_TEXTS = {BTN_FIND, BTN_LAST, LEGACY_BTN_FIND, LEGACY_BTN_LAST}


@router.message(DealEditFlow.choosing, F.text.in_(_MENU_BUTTON_TEXTS))
@router.message(DealEditFlow.typing, F.text.in_(_MENU_BUTTON_TEXTS))
@router.message(EDIT_STATES, Command("find", "last"))
async def protect_edit_flow(message: Message) -> None:
    """Поиск не рвёт незаконченную правку — сначала сохранить или отменить."""
    await message.answer(EDIT_IN_PROGRESS)


@router.message(DealEditFlow.choosing, F.text, ~F.text.startswith("/"))
async def on_choosing_text(message: Message, state: FSMContext) -> None:
    """Свободный текст в режиме выбора поля — подсказка вместо новой заявки."""
    data = await state.get_data()
    await message.answer(
        EDIT_NO_CHANGES if not (data.get("changes") or {}) else _changes_summary(data["changes"]),
        reply_markup=edit_fields_keyboard(bool(data.get("contact_id"))),
    )


# ---------------------------------------------------------------------------
# Сохранение и отмена
# ---------------------------------------------------------------------------


def _build_deal_fields(
    deal: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    """Поля crm.deal.update из накопленных правок и текущей сделки."""
    fields: dict[str, Any] = {}
    if "category" in changes or "problem" in changes:
        current_category, current_problem = split_title(deal.get("TITLE"))
        category = changes.get(
            "category", deal.get(UF_SERVICE_CATEGORY) or current_category or "прочее"
        )
        problem = changes.get("problem", current_problem)
        fields["TITLE"] = f"{category}: {problem}"[:255]
    if "category" in changes:
        fields[UF_SERVICE_CATEGORY] = changes["category"]
    if "source" in changes:
        fields["SOURCE_ID"] = SOURCE_ID_BY_NAME.get(changes["source"], "OTHER")
    if "income" in changes or "expense" in changes:
        income = changes.get("income", _num(deal.get("OPPORTUNITY")))
        expense = changes.get("expense", _num(deal.get(UF_EXPENSE)))
        if "income" in changes:
            fields["OPPORTUNITY"] = changes["income"]
        if "expense" in changes:
            fields[UF_EXPENSE] = changes["expense"]
        if income is not None:
            # Прибыль пересчитывается из итоговых значений: правка одной
            # суммы не должна портить аналитику.
            fields[UF_PROFIT] = income - (expense or 0)
    if "deadline" in changes:
        pretty = dates.format_deadline(changes["deadline"])
        comments = str(deal.get("COMMENTS") or "")
        if _DEADLINE_LINE_RE.search(comments):
            comments = _DEADLINE_LINE_RE.sub(f"Срок: {pretty}", comments, count=1)
        else:
            comments = f"{comments}\nСрок: {pretty}" if comments else f"Срок: {pretty}"
        fields["COMMENTS"] = comments
    return fields


def _contact_phone_fields(contact: dict[str, Any], new_phone: str) -> list[dict[str, Any]]:
    """Замена основного номера: правится существующее значение, не плодится новое."""
    phones = contact.get("PHONE") or []
    if phones and isinstance(phones[0], dict) and phones[0].get("ID"):
        replaced = [{"ID": phones[0]["ID"], "VALUE": new_phone}]
        return replaced
    return [{"VALUE": new_phone, "VALUE_TYPE": "WORK"}]


async def _reschedule_reminders(
    message: Message,
    db: Database,
    bitrix: BitrixClient,
    deal: dict[str, Any],
    deal_id: int,
    new_deadline: str,
) -> None:
    """Переносит Telegram-напоминание и дело CRM на новый срок (best-effort)."""
    pending = await db.pending_deal_reminder(deal_id)
    await db.drop_pending_deal_reminders(deal_id)
    due_ts = dates.reminder_epoch(new_deadline)
    if due_ts is None or due_ts <= int(dates.now_local().timestamp()):
        return
    activity_id = pending["activity_id"] if pending else None
    _, problem = split_title(deal.get("TITLE"))
    try:
        if not activity_id:
            # Дело могло существовать и без записи в очереди (напоминание уже
            # ушло, или его завели вручную в CRM): переносится актуальное
            # существующее, а не плодится второе дело в карточке.
            best = nearest_todo(
                await list_deal_todos(bitrix, deal_id),
                int(dates.now_local().timestamp()),
            )
            if best is not None:
                activity_id = best[0]
        if activity_id:
            await update_deal_todo_deadline(
                bitrix,
                activity_id,
                deal_id,
                dates.epoch_to_iso(due_ts),
                title=f"Заявка №{deal_id}: {problem}",
            )
        else:
            activity_id = await create_deal_todo(
                bitrix,
                deal_id,
                title=f"Заявка №{deal_id}: {problem}",
                deadline_iso=dates.epoch_to_iso(due_ts),
                responsible_id=settings.bitrix_responsible_id,
            )
    except Exception:
        log.exception("Дело-напоминание сделки %s не перенесено", deal_id)
    await db.add_reminder(
        message.chat.id,
        text=(
            f"заявка №{deal_id} — {problem}. "
            f"Срок: {dates.format_deadline(new_deadline)}"
        ),
        due_ts=due_ts,
        kind="deal",
        entity_id=deal_id,
        activity_id=activity_id,
    )


@router.callback_query(EDIT_STATES, F.data == "dedit:save")
async def on_edit_save(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    data = await state.get_data()
    deal_id = data.get("deal_id")
    changes: dict[str, Any] = data.get("changes") or {}
    await callback.answer()
    if not deal_id:
        await state.clear()
        await callback.message.answer(EDIT_STALE)
        return
    if not changes:
        await callback.message.answer(EDIT_NO_CHANGES)
        return
    if bitrix is None:
        await callback.message.answer(NO_CRM_EDIT)
        return
    try:
        async with asyncio.timeout(EDIT_CRM_DEADLINE):
            deal = await get_deal(bitrix, deal_id)
            if deal is None:
                await state.clear()
                await callback.message.answer(DEAL_GONE.format(deal_id=deal_id))
                return
            deal_fields = _build_deal_fields(deal, changes)
            if deal_fields:
                await update_deal(bitrix, deal_id, deal_fields)
            if ("name" in changes or "phone" in changes) and data.get("contact_id"):
                contact = await get_contact(bitrix, data["contact_id"])
                if contact is not None:
                    contact_fields: dict[str, Any] = {}
                    if "name" in changes:
                        contact_fields["NAME"] = changes["name"]
                    if "phone" in changes:
                        contact_fields["PHONE"] = _contact_phone_fields(
                            contact, changes["phone"]
                        )
                    await update_contact(bitrix, data["contact_id"], contact_fields)
    except Exception:
        # Правки остаются в FSM: «Сохранить» можно нажать ещё раз, обновления
        # Bitrix идемпотентны и второй записи-дубля не создадут.
        log.exception("Правки заявки №%s не сохранены", deal_id)
        await callback.message.answer(EDIT_SAVE_FAILED)
        return
    if "deadline" in changes:
        try:
            await _reschedule_reminders(
                callback.message, db, bitrix, deal, deal_id, changes["deadline"]
            )
        except Exception:
            log.exception("Напоминания сделки %s не перенесены", deal_id)
    await state.clear()
    await callback.message.answer(EDIT_SAVED.format(deal_id=deal_id))
    # Свежая карточка после сохранения: сотрудник сразу видит результат.
    try:
        async with asyncio.timeout(EDIT_CRM_DEADLINE):
            context = await _load_deal_context(bitrix, deal_id)
    except Exception:
        log.exception("Карточка №%s после сохранения не показана", deal_id)
        return
    if context is not None:
        deal, contact, stages, todos = context
        await callback.message.answer(
            deal_card_text(deal, contact, stages, todos),
            reply_markup=deal_card_keyboard(deal_id),
        )


@router.callback_query(EDIT_STATES, F.data == "dedit:cancel")
async def on_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer(EDIT_CANCELLED)


@router.callback_query(F.data.startswith("dedit:"))
async def on_edit_stale(callback: CallbackQuery) -> None:
    """Кнопки правки после сброса состояния (рестарт, отмена) не молчат."""
    await callback.answer(EDIT_STALE, show_alert=True)


@router.message(EDIT_STATES, Command("cancel"))
async def on_edit_cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(EDIT_CANCELLED)
