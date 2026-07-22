"""Команды /start, /help, /new и главное меню (reply-клавиатура).

Кнопки меню приходят обычным текстом, поэтому этот роутер подключается
раньше разбора свободного текста: «Новая заявка» отвечает подсказкой, а не
уходит в модель. Кнопки «Найти» и «Последние» обрабатывает handlers/search.
"""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

router = Router(name="start")

BTN_NEW = "Новая заявка"
BTN_FIND = "Найти"
BTN_LAST = "Последние"
BTN_REMIND = "Напоминание"
BTN_MY_REMINDERS = "Мои напоминания"

# Кнопки клавиатуры СТАРОГО бота (ветка legacy/telebot-mvp). Reply-клавиатура
# живёт в чате, пока её не заменят: у сотрудника, не нажимавшего /start после
# обновления, кнопки прежние. Их тексты обязаны работать как новые кнопки —
# иначе «🔎 Найти заявку» уходит в разбор заявки и бот отвечает подсказкой
# «нажмите „Найти“» вместо поиска.
LEGACY_BTN_NEW = "🆕 Новая заявка"
LEGACY_BTN_FIND = "🔎 Найти заявку"
LEGACY_BTN_LAST = "📋 Последние заявки"

WELCOME = (
    "Бот приёма заявок на связи.\n\n"
    "Пришлите заявку обычным текстом или голосовым сообщением, например:\n"
    "«Иван, 89141234567, сантехника, срочно завтра, замена крана»\n\n"
    "Я разберу сообщение и заведу клиента и сделку в Bitrix24.\n\n"
    "Кнопки меню: «Новая заявка» — подсказка по формату, «Найти» — поиск "
    "по телефону, номеру заявки или названию, «Последние» — последние 10 заявок, "
    "«Напоминание» — отдельное напоминание на любую дату и с любым текстом, "
    "«Мои напоминания» — список ожидающих и отмена."
)

NEW_ORDER_HINT = (
    "Пришлите заявку текстом или голосовым сообщением: имя клиента, телефон, "
    "что нужно сделать и срок. Например: «Иван, 89141234567, сантехника, "
    "замена крана, завтра»."
)

# Общий ответ на попытку начать новое действие посреди незаконченной правки
# заявки. Живёт здесь, а не в handlers/edit: edit сам импортирует кнопки из
# этого модуля, обратный импорт замкнул бы цикл.
EDIT_IN_PROGRESS = "Сначала сохраните или отмените правки заявки (кнопки выше)."


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура главного меню."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_FIND), KeyboardButton(text=BTN_LAST)],
            [KeyboardButton(text=BTN_REMIND), KeyboardButton(text=BTN_MY_REMINDERS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def _close_search_flow(state: FSMContext) -> None:
    """Закрывает ожидание поискового запроса или напоминания при /start и /help.

    После приветствия пользователь следует подсказке и шлёт заявку — она не
    должна поглощаться как поисковый запрос забытого /find или как текст
    напоминания. Незаконченный опросник заявки командами помощи НЕ
    сбрасывается (полный сброс — /new). Состояние сверяется по имени группы,
    а не импортом SearchFlow/ReminderFlow: handlers/search и
    handlers/reminders сами импортируют кнопки из этого модуля.
    """
    current = await state.get_state()
    if current is not None and (
        current.startswith("SearchFlow") or current.startswith("ReminderFlow")
    ):
        await state.clear()


async def _edit_changes_in_progress(state: FSMContext) -> bool:
    """Правка заявки с несохранёнными изменениями сейчас активна.

    «Новая заявка» (включая легаси-кнопку старого бота) и /new не имеют
    права молча стереть накопленные правки: сотрудник вводил их руками —
    сначала «Сохранить» или «Отмена». Правка БЕЗ изменений сбрасывается как
    обычно: терять в ней нечего. Состояние сверяется по имени группы, а не
    импортом DealEditFlow: handlers/edit сам импортирует этот модуль.
    """
    current = await state.get_state()
    if current is None or not current.startswith("DealEditFlow"):
        return False
    data = await state.get_data()
    return bool(data.get("changes"))


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await _close_search_flow(state)
    await message.answer(WELCOME, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def on_help(message: Message, state: FSMContext) -> None:
    await _close_search_flow(state)
    await message.answer(WELCOME, reply_markup=main_menu_keyboard())


@router.message(Command("new"))
async def on_new(message: Message, state: FSMContext) -> None:
    # «Новая заявка» начинает с чистого листа: хвосты незаконченных
    # опросников и поисковых запросов не должны мешать новому тексту.
    # Исключение — правка с несохранёнными изменениями: её не стираем молча.
    if await _edit_changes_in_progress(state):
        await message.answer(EDIT_IN_PROGRESS)
        return
    await state.clear()
    await message.answer(NEW_ORDER_HINT)


@router.message(F.text == BTN_NEW)
async def on_new_button(message: Message, state: FSMContext) -> None:
    await on_new(message, state)


@router.message(F.text == LEGACY_BTN_NEW)
async def on_legacy_new_button(message: Message, state: FSMContext) -> None:
    """Кнопка старого бота: тот же сброс, плюс замена устаревшей клавиатуры."""
    if await _edit_changes_in_progress(state):
        await message.answer(EDIT_IN_PROGRESS)
        return
    await state.clear()
    await message.answer(NEW_ORDER_HINT, reply_markup=main_menu_keyboard())
