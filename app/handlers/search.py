"""Поиск по заявкам (/find, кнопка «Найти») и последние заявки (/last).

/find принимает запрос сразу («/find 89141234567») или следующим сообщением
(состояние SearchFlow.query). Тип запроса определяется автоматически:
- короткое число (до 9 цифр) — номер заявки, crm.deal.get;
- текст, из которого normalize_phone достаёт номер, — сделки контактов
  с этим телефоном;
- любой другой текст — поиск по названию/комментарию сделки и по
  имени/фамилии контакта; организация ищется также по UF_CRM_ORG контакта.

Вывод — короткий список: № сделки, клиент, название (категория: суть),
стадия, дата создания. Хендлеры кнопок меню («Найти», «Последние»)
регистрируются до обработчика запроса, чтобы кнопка работала и из
состояния ожидания запроса; внутри активной заявки кнопки поиска
перехватываются с предупреждением и не становятся ответом на вопрос.

После входа через «Найти» или /find режим остаётся активным: следующие
текстовые и голосовые сообщения тоже считаются поисковыми запросами. Выход —
«Новая заявка», /new, /start или /help. Вся работа с CRM (поиск + имена
контактов и стадий) ограничена общим дедлайном и завёрнута в мягкую обработку
ошибок: любой сбой отвечает SEARCH_FAILED, а не молчанием.
"""

import asyncio
import contextlib
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.handlers.messages import OrderFlow
from app.handlers.start import BTN_FIND, BTN_LAST
from app.services import dates
from app.services.bitrix import (
    BitrixClient,
    contact_names,
    get_deal,
    normalize_phone,
    recent_deals,
    search_deals_by_phone,
    search_deals_by_text,
    stage_names,
)

log = logging.getLogger("bot.search")

router = Router(name="search")

NO_CRM = "CRM пока не подключена (не задан вебхук Bitrix24), поиск недоступен."

ASK_QUERY = (
    "Что ищем? Пришлите телефон клиента, номер заявки, имя или название "
    "организации."
)

SEARCH_AGAIN_HINT = (
    "Искать ещё? Пришлите телефон, номер или имя. Чтобы завести заявку, "
    "нажмите «Новая заявка»."
)

NOTHING_FOUND = "Ничего не нашёл."

SEARCH_FAILED = "Поиск сейчас не работает, попробуйте позже."

# Совпадений больше, чем позволяет просмотреть лимит чтения CRM: отвечать
# «ничего не нашёл» нельзя — сделка может быть у непросмотренного контакта.
SEARCH_TOO_BROAD = "Слишком много совпадений, все не проверить. Уточните запрос."

ACTIVE_ORDER_WARNING = "Сначала завершите текущую заявку или отмените её карточкой."

QUERY_TOO_SHORT = "Слишком короткий запрос, нужно хотя бы 2 символа. Уточните."

LAST_EMPTY = "Заявок пока нет."

# Сколько строк показывать в списках (/find и /last)
LIST_LIMIT = 10

# Номер заявки короче телефона: до 9 цифр (валидный номер — от 10)
MAX_DEAL_ID_DIGITS = 9

# Минимальная длина текстового запроса: однобуквенный «/find а» совпал бы
# почти со всеми сделками портала и тянул бы тысячи строк ради 10.
MIN_TEXT_QUERY_LEN = 2

# Основа для запасного поиска не должна становиться слишком общей.
MIN_STEM_QUERY_LEN = 3

# Общий дедлайн одного поиска (все запросы к CRM, включая имена контактов
# и стадий): /find и /last не должны зависать на медленном портале.
SEARCH_DEADLINE = 25

# Лимит длины одного sendMessage Telegram (символов).
TG_MESSAGE_LIMIT = 4096

# Слова удаляются только с краёв запроса: внутри названия организации или
# описания они могут быть значимы. Дополнительные формы покрывают обычную
# разговорную речь и исправление «нет, найди ...» после неудачной попытки.
_EDGE_SERVICE_WORDS = frozenset(
    {
        "найди",
        "найти",
        "поиск",
        "искать",
        "ищу",
        "можешь",
        "пожалуйста",
        "покажи",
        "дай",
        "мне",
        "клиент",
        "клиента",
        "клиенту",
        "заявку",
        "заявка",
        "заявки",
        "заказ",
        "заказа",
        "по",
        "номер",
        "номеру",
        "телефон",
        "телефону",
        "организация",
        "организацию",
        "организации",
        "фирма",
        "фирму",
        "компания",
        "компанию",
        "нет",
        "не",
    }
)
_QUERY_TOKEN_RE = re.compile(
    r"[0-9A-Za-zА-Яа-яЁё]+(?:[-'’][0-9A-Za-zА-Яа-яЁё]+)*"
)
_LAST_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+$")
_TWO_LETTER_ENDINGS = (
    "ом",
    "ем",
    "ым",
    "им",
    "ой",
    "ах",
    "ях",
    "ам",
    "ям",
    "ую",
    "юю",
)

_ORDER_NUMBER_TEXT_RE = re.compile(
    r"^\s*(?:статус(?:\s+по)?|что\s+с|покажи(?:те)?|найди(?:те)?)\s+"
    r"заявк[а-яё]*\s*(?:№\s*|номер(?:ом|а|у)?\s*)?(\d{1,9})\s*[?!.]*\s*$",
    re.IGNORECASE,
)


class SearchFlow(StatesGroup):
    """Липкий режим поисковых запросов после «Найти», /find или /last."""

    query = State()


def clean_search_query(raw: str) -> str:
    """Оставляет смысловую часть естественного текстового запроса.

    Служебные слова снимаются только с начала и конца, а исходный фрагмент
    между смысловыми словами сохраняется вместе с ``&``, дефисами и
    кавычками. Телефоны и чистые номера сделок до этой функции не доходят
    и обрабатываются точными поисковыми ветками.
    """
    source = raw or ""
    matches = list(_QUERY_TOKEN_RE.finditer(source))
    first = 0
    last = len(matches) - 1
    while first <= last and matches[first].group(0).casefold() in _EDGE_SERVICE_WORDS:
        first += 1
    while last >= first and matches[last].group(0).casefold() in _EDGE_SERVICE_WORDS:
        last -= 1
    if first > last:
        return ""

    start = matches[first].start()
    end = matches[last].end()
    quote_chars = "\"'«»“”„"

    # Кавычка непосредственно перед названием относится к названию, даже
    # если перед ней было удалено служебное слово: «найди \"Ромашку\"».
    prefix_start = matches[first - 1].end() if first else 0
    prefix = source[prefix_start:start]
    quote_positions = [prefix.rfind(char) for char in quote_chars]
    quote_position = max(quote_positions, default=-1)
    if quote_position >= 0:
        start = prefix_start + quote_position

    # После последнего смыслового слова сохраняются только закрывающие
    # кавычки; запятые, точки и восклицательные знаки поиску не помогают.
    suffix_end = matches[last + 1].start() if last + 1 < len(matches) else len(source)
    suffix = source[end:suffix_end]
    closing_quotes = "".join(char for char in suffix if char in quote_chars)
    return (source[start:end].strip() + closing_quotes).strip()


def extract_order_number_request(text: str) -> str | None:
    """Номер из явного текстового вопроса о существующей заявке."""
    match = _ORDER_NUMBER_TEXT_RE.match(text or "")
    return match.group(1) if match is not None else None


def _stem_search_query(query: str) -> str | None:
    """Возвращает одну более короткую основу последнего слова запроса."""
    match = _LAST_WORD_RE.search(query)
    if match is None:
        return None
    word = match.group(0)
    cut = 2 if word.casefold().endswith(_TWO_LETTER_ENDINGS) else 1
    stem = word[:-cut]
    if len(stem) < MIN_STEM_QUERY_LEN:
        return None
    return query[: match.start()] + stem


# Кнопки меню срабатывают только в свободных состояниях и на карточке-превью
# (её черновик уже сохранён в SQLite и переживёт поиск). Внутри активной
# заявки команды и кнопки поиска перехватываются отдельными обработчиками:
# пользователь получает предупреждение, а текст не становится ответом на
# вопрос и не стирает данные заявки из FSM.
MENU_BUTTON_STATES = StateFilter(
    None,
    SearchFlow.query,
    OrderFlow.preview,
)

ACTIVE_ORDER_STATES = StateFilter(
    OrderFlow.ask_phone,
    OrderFlow.ask_category,
    OrderFlow.form_name,
    OrderFlow.form_phone,
    OrderFlow.form_category,
    OrderFlow.form_problem,
    OrderFlow.form_deadline,
)


def _deal_line(deal: dict, names: dict[int, str], stages: dict[str, str]) -> str:
    """Одна строка списка: №, клиент, название, стадия, дата (дд.мм.гггг чч:мм)."""
    try:
        contact_key = int(deal.get("CONTACT_ID") or 0)
    except (TypeError, ValueError):
        contact_key = 0
    client = names.get(contact_key, "без контакта")
    stage_id = str(deal.get("STAGE_ID") or "—")
    stage = stages.get(stage_id, stage_id)
    title = str(deal.get("TITLE") or "без названия")
    return (
        f"№{deal.get('ID')} · {client} · {title} · {stage} · "
        f"{dates.format_bitrix_datetime(deal.get('DATE_CREATE'))}"
    )


def _clip_reply(text: str, suffix: str = "") -> str:
    """Укладывает ответ в лимит Telegram, честно считая скрытые строки.

    Десяти строк с длинными именами и названиями хватает, чтобы превысить
    4096 символов — такой sendMessage Telegram отклоняет (Bad Request), и
    пользователь не получил бы ничего. Ответ режется ПО СТРОКАМ с хвостом
    «…и ещё N», а финальный срез страхует вырожденный случай сверхдлинной
    одиночной строки.
    """
    suffix_text = f"\n\n{suffix}" if suffix else ""
    available = TG_MESSAGE_LIMIT - len(suffix_text)
    if len(text) <= available:
        return text + suffix_text
    lines = text.splitlines()
    kept: list[str] = []
    length = 0
    for index, line in enumerate(lines):
        tail = f"…и ещё {len(lines) - index}. Уточните запрос."
        if length + len(line) + len(tail) + 2 > available:
            kept.append(tail)
            break
        kept.append(line)
        length += len(line) + 1
    return "\n".join(kept)[:available] + suffix_text


async def _answer_search_reply(
    message: Message, reply: str, *, offer_next_search: bool = False
) -> bool:
    """Отправляет итог поиска: длина в лимите, сбой отправки не роняет апдейт.

    Отправка — тоже часть «мягкого» пути: если Telegram отклонил сообщение,
    пользователю уходит хотя бы короткий SEARCH_FAILED, а не тишина с
    ошибкой в глобальном error-хендлере.
    """
    try:
        suffix = SEARCH_AGAIN_HINT if offer_next_search else ""
        await message.answer(_clip_reply(reply, suffix))
        return True
    except Exception:
        log.exception("Ответ поиска не отправлен")
        with contextlib.suppress(Exception):
            await message.answer(SEARCH_FAILED)
            return True
    return False


async def _format_deals(bitrix: BitrixClient, deals: list[dict], header: str) -> str:
    """Читаемый список сделок (не длиннее LIST_LIMIT строк).

    Дополнительно ходит в CRM за именами контактов и стадий, поэтому
    вызывается только внутри мягкой обработки ошибок поиска.
    """
    shown = deals[:LIST_LIMIT]
    ids = [d.get("CONTACT_ID") for d in shown if d.get("CONTACT_ID")]
    names = await contact_names(bitrix, ids)
    stages = await stage_names(bitrix)
    lines = [header] if header else []
    lines += [_deal_line(deal, names, stages) for deal in shown]
    if len(deals) > LIST_LIMIT:
        lines.append(f"…и ещё {len(deals) - LIST_LIMIT}. Уточните запрос.")
    return "\n".join(lines)


async def _run_search(message: Message, bitrix: BitrixClient, query: str) -> bool:
    """Определяет тип запроса, ищет и отвечает списком.

    Весь поход в CRM — и сам поиск, и обогащение именами контактов/стадий —
    под общим дедлайном и в try: сбой любого запроса отвечает SEARCH_FAILED,
    а не роняет апдейт в глобальный error-хендлер без ответа пользователю.
    """
    raw_query = query.strip()
    if not raw_query:
        await message.answer(ASK_QUERY)
        return True
    offer_next_search = False
    try:
        async with asyncio.timeout(SEARCH_DEADLINE):
            phone = normalize_phone(raw_query)
            truncated = False
            if raw_query.isdigit() and len(raw_query) <= MAX_DEAL_ID_DIGITS:
                deal = await get_deal(bitrix, int(raw_query))
                deals = [deal] if deal else []
            elif phone is not None:
                found = await search_deals_by_phone(bitrix, phone)
                deals = found.deals
                truncated = found.truncated
            else:
                query = clean_search_query(raw_query)
                if not query:
                    await message.answer(ASK_QUERY)
                    return True
                # Фраза «найди по номеру 154» после очистки снова становится
                # точным идентификатором и не проходит через поиск по основе.
                cleaned_phone = normalize_phone(query)
                if query.isdigit() and len(query) <= MAX_DEAL_ID_DIGITS:
                    deal = await get_deal(bitrix, int(query))
                    deals = [deal] if deal else []
                elif cleaned_phone is not None:
                    found = await search_deals_by_phone(bitrix, cleaned_phone)
                    deals = found.deals
                    truncated = found.truncated
                elif len(query) < MIN_TEXT_QUERY_LEN:
                    await message.answer(QUERY_TOO_SHORT)
                    return True
                else:
                    candidates = [raw_query, query]
                    stem_query = _stem_search_query(query)
                    if stem_query is not None:
                        candidates.append(stem_query)
                    candidates = list(dict.fromkeys(candidates))

                    for candidate in candidates:
                        found = await search_deals_by_text(bitrix, candidate)
                        if found.deals or found.truncated:
                            break
                    deals = found.deals
                    truncated = found.truncated
            if truncated:
                # Любой усечённый результат неполон, даже если несколько
                # старых сделок уже нашлись: показывать их как все совпадения
                # было бы вводящим в заблуждение.
                reply = SEARCH_TOO_BROAD
            elif deals:
                reply = await _format_deals(bitrix, deals, header=f"Нашёл заявок: {len(deals)}")
                offer_next_search = True
            else:
                reply = NOTHING_FOUND
                offer_next_search = True
    except Exception:
        log.exception("Поиск по запросу не удался")
        reply = SEARCH_FAILED
    return await _answer_search_reply(
        message, reply, offer_next_search=offer_next_search
    )


@router.message(ACTIVE_ORDER_STATES, Command("find"))
@router.message(ACTIVE_ORDER_STATES, Command("last"))
async def protect_active_order_command(message: Message) -> None:
    """Поисковые команды не меняют незавершённую заявку в FSM."""
    await message.answer(ACTIVE_ORDER_WARNING)


@router.message(ACTIVE_ORDER_STATES, F.text.in_({BTN_FIND, BTN_LAST}))
async def protect_active_order_button(message: Message) -> None:
    """Reply-кнопки меню не становятся ответом на вопрос заявки."""
    await message.answer(ACTIVE_ORDER_WARNING)


@router.message(ACTIVE_ORDER_STATES, F.text.regexp(_ORDER_NUMBER_TEXT_RE))
async def protect_active_order_text_search(message: Message) -> None:
    """Текстовый поиск, как и /find, не перебивает активное уточнение."""
    await message.answer(ACTIVE_ORDER_WARNING)


@router.message(MENU_BUTTON_STATES, F.text.regexp(_ORDER_NUMBER_TEXT_RE))
async def on_text_order_number(
    message: Message,
    state: FSMContext,
    bitrix: BitrixClient | None = None,
) -> None:
    """Разовый точный поиск по номеру из разговорного вопроса."""
    if bitrix is None:
        await message.answer(NO_CRM)
        return
    number = extract_order_number_request(message.text or "")
    if number is None:
        return
    await _run_search(message, bitrix, number)


@router.message(Command("find"))
async def on_find(
    message: Message,
    state: FSMContext,
    bitrix: BitrixClient | None = None,
    command: CommandObject | None = None,
) -> None:
    if bitrix is None:
        await message.answer(NO_CRM)
        return
    args = (command.args or "").strip() if command else ""
    if args:
        if await _run_search(message, bitrix, args):
            await state.set_state(SearchFlow.query)
        return
    await message.answer(ASK_QUERY)
    await state.set_state(SearchFlow.query)


@router.message(Command("last"))
async def on_last(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    if bitrix is None:
        await message.answer(NO_CRM)
        return
    try:
        async with asyncio.timeout(SEARCH_DEADLINE):
            deals = await recent_deals(bitrix, LIST_LIMIT)
            if deals:
                reply = await _format_deals(bitrix, deals, header="Последние заявки:")
            else:
                reply = LAST_EMPTY
    except Exception:
        log.exception("Не удалось получить последние заявки")
        reply = SEARCH_FAILED
    if await _answer_search_reply(message, reply, offer_next_search=True):
        await state.set_state(SearchFlow.query)


@router.message(MENU_BUTTON_STATES, F.text == BTN_FIND)
async def on_find_button(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    """Кнопка меню «Найти» — то же, что /find без аргументов."""
    await on_find(message, state, bitrix)


@router.message(MENU_BUTTON_STATES, F.text == BTN_LAST)
async def on_last_button(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    """Кнопка меню «Последние» — то же, что /last."""
    await on_last(message, state, bitrix)


async def handle_search_query(
    message: Message, state: FSMContext, bitrix: BitrixClient | None, query: str
) -> None:
    """Поисковый запрос из состояния SearchFlow.query (текстом или голосом)."""
    if bitrix is None:
        await message.answer(NO_CRM)
        await state.clear()
        return
    await _run_search(message, bitrix, query)


@router.message(SearchFlow.query, F.text)
async def on_search_query(
    message: Message, state: FSMContext, bitrix: BitrixClient | None = None
) -> None:
    await handle_search_query(message, state, bitrix, message.text or "")
