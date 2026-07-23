"""Текстовый поток заявки.

Текст -> разбор моделью -> карточка-превью с кнопками "Создать / Изменить /
Отмена" -> запись контакта и сделки в Bitrix24 -> ответ с номером заявки.

Повтор того же текста в одном чате за последние сутки (в том числе
перепечатанный заново — у него новый message_id, и точный ключ дедупа его
не ловит) перехватывается контент-хэшем ДО обращения к модели: вместо
карточки бот предупреждает о вероятном дубле (с номером заявки, если она
уже создана) и предлагает кнопку «Создать всё равно». Нажатие продолжает
штатную обработку сохранённого текста — похожий текст может оказаться и
другой реальной заявкой, поэтому решение остаётся за сотрудником. Точный
повтор той же доставки или того же forward по-прежнему жёстко отсекает
DedupMiddleware до хендлеров.

Если в тексте нет телефона или категории, бот задаёт уточняющий вопрос.
Вопрос о телефоне не запирает диалог: если вместо номера приходит новая
осмысленная фраза, она переразбирается заново (тот же путь, что on_text),
а телефон спрашивается максимум один раз — при повторном отсутствии номера
карточка показывается без него.
Если модель недоступна (LLMUnavailable), заявка собирается пошаговым
опросником из 6 вопросов (имя, телефон, категория, источник, описание,
срок). Промежуточный черновик живёт в FSM (in-memory),
а каждая показанная карточка-превью сохраняется в SQLite (таблица drafts)
и её кнопки несут draft_id: нажатие применяется именно к той карточке,
на которой нажали, даже если после неё показаны новые. Просроченная
карточка (TTL 30 минут) отвечает "Карточка устарела". Кнопки слушаются
только автора карточки в её чате, а "Создать" атомарно захватывает
черновик: двойное нажатие не создаёт вторую сделку.

У черновика явная машина состояний (см. app/db.py): open — обычная работа,
creation_unknown — неоднозначный исход deal.add: таймаут, обрыв связи или
отмена задачи после отправки запроса (повторное "Создать" только сверяется
с CRM, второй deal.add невозможен), done — терминальный tombstone
созданной сделки (повторное нажатие отвечает её номером).

Заявки-напоминания (intent=reminder) сделками не становятся: по ним
создаётся задача в Bitrix24 (tasks.task.add) с дедлайном из распознанного
срока, пользователь получает номер задачи.

Прочие сообщения (intent=other) получают короткую подсказку о возможностях
бота и не создают сущностей в Bitrix24.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import DRAFT_DONE, DRAFT_UNKNOWN, Database
from app.middlewares.dedup import content_hash
from app.schemas import Category, Intent, ParsedOrder, Source
from app.services import binding, dates, llm
from app.services.binding import BindingRef
from app.services.bitrix import (
    DEFAULT_SOURCE_ID,
    SOURCE_ID_BY_NAME,
    UF_EXPENSE,
    UF_PROFIT,
    UF_SERVICE_CATEGORY,
    BitrixClient,
    add_contact_timeline_comment,
    contact_names,
    create_deal,
    create_deal_todo,
    extract_bare_phone,
    find_contact_by_draft_id,
    find_deal_by_key,
    get_deal,
    is_server_refusal,
    normalize_phone,
    recent_deals,
    resolve_contact,
    search_deals_by_phone,
)
from app.services.tasks import create_reminder_task, find_reminder_task

log = logging.getLogger("bot.messages")

router = Router(name="messages")

SKIP_WORDS = {"нет", "нет телефона", "не знаю", "не надо", "пропустить", "skip"}
KEEP_WORD = "-"

NOT_A_PHONE = (
    "Не похоже на номер телефона. Пришлите номер, например 89141234567, "
    "или напишите «нет», если телефона нет."
)

STALE_CARD = "Карточка устарела, отправьте заявку ещё раз."

FOREIGN_CARD = "Это чужая карточка"

CLAIM_IN_PROGRESS = "Обрабатываю"

DRAFT_BUSY = "Заявка сейчас обрабатывается, подождите."

# Неоднозначный исход deal.add (таймаут, обрыв связи, отмена): запрос мог
# пройти без ответа, черновик заморожен в creation_unknown, повторные
# нажатия «Создать» только сверяются с CRM.
CRM_UNKNOWN_TEXT = (
    "Не уверен, что заявка записалась, проверяю. "
    "Если не появится — отправьте заявку заново."
)

# Сбой строго ДО отправки deal.add (контакт, предпроверка дубля): сделки в
# CRM точно нет, повтор по той же кнопке безопасен.
CRM_RETRY_TEXT = (
    "Не получилось записать заявку в CRM. Попробуйте нажать "
    "«Создать» ещё раз через минуту."
)

CRM_STILL_UNKNOWN_TEXT = (
    "Всё ещё проверяю, записалась ли заявка. "
    "Если сделка не появится, отправьте заявку заново."
)

DEAL_ALREADY_CREATED = "Заявка №{deal_id} уже создана."

# Мягкий контент-дедуп: тот же текст уже присылали в последние сутки.
# Похожий текст может быть и другой реальной заявкой, поэтому вместо отказа —
# предупреждение и кнопка «Создать всё равно».
DUP_WITH_DEAL = "Похоже на дубль заявки №{deal_id}. Создать всё равно?"
DUP_NO_DEAL = "Похоже, такую заявку вы уже отправляли. Создать всё равно?"
FORCE_CREATE_BUTTON = "Создать всё равно"

REMINDER_CREATED = "Напоминание записано: задача №{task_id} в Bitrix24."

# Подтверждение напоминания, привязанного к заявке. Живёт здесь (а не в
# handlers/reminders): свободнотекстовый путь intent=reminder тоже привязывает,
# а reminders импортирует этот модуль — обратный импорт дал бы цикл.
REMIND_SCHEDULED_DEAL = (
    "Пришлю напоминание в телеграм {when} по заявке {deal}. Посмотреть "
    "или отменить: кнопка «Мои напоминания»."
)

# Заявка из фразы «к заявке …» в свободном тексте не нашлась однозначно:
# напоминание честно ставится обычным, а не привязывается наугад.
BIND_INLINE_MISS = (
    "Заявку из текста не нашёл, поставил обычное напоминание. Привязать к "
    "заявке можно через кнопку «Напоминание»."
)

MULTIPLE_ORDERS_TEXT = (
    "В сообщении несколько заявок, я взял первую (клиент {client}). "
    "Остальные пришлите по одной, так каждая точно попадёт в CRM."
)

OTHER_MESSAGE_TEXT = (
    "Я принимаю заявки: пришлите описание текстом или голосом. "
    "Для поиска заявки нажмите «Найти» в меню."
)

ORDER_CHANGES_TEXT = (
    "Изменить, перенести или отменить существующую заявку можно в Bitrix24. "
    "Бот только заводит новые заявки."
)

REMINDER_NO_CRM = (
    "Понял, это напоминание, но CRM пока не подключена (не задан вебхук "
    "Bitrix24) — задачу не записал. Попробуйте позже."
)

REMINDER_FAILED = (
    "Не получилось записать напоминание в Bitrix24, попробуйте ещё раз через минуту."
)

# Неоднозначный исход tasks.task.add: запрос мог пройти без ответа, и сверка
# по ключу-тегу задачу пока не увидела. Повтор того же текста предупредит о
# вероятном дубле, вторую задачу создаст только явное подтверждение.
REMINDER_UNKNOWN_TEXT = (
    "Не уверен, что напоминание записалось в Bitrix24. Проверьте задачи; "
    "если её нет — отправьте напоминание ещё раз."
)


# Общий дедлайн записи контакта и сделки в CRM. Должен быть меньше
# CLAIM_TIMEOUT_SECONDS (120с в app/db.py): воркер либо успевает, либо
# отпускает аренду до того, как её можно перехватить.
CRM_DEADLINE = 40

# Дедлайн ВСЕЙ обработки нажатия «Создать всё равно» (после захвата аренды
# отложенного текста). Строго меньше CLAIM_TIMEOUT_SECONDS (120с в app/db.py)
# с запасом: обработчик гарантированно завершается или отменяется с
# освобождением аренды РАНЬШЕ, чем аренду вообще можно перехватить, поэтому
# «зомби»-обработчик, переживший свою аренду, структурно невозможен — это
# инвариант, на котором держится единственность черновика во всех ветках
# force-flow (карточка, уточнения, опросник). Внутри дедлайна умещаются
# разбор модели (у неё свой таймаут) и ответы Telegram.
FORCE_FLOW_DEADLINE = 90

# Ответ на дедлайн force-flow: аренда уже снята, повторное нажатие сработает.
FORCE_TIMEOUT_TEXT = "Не успел обработать заявку, нажмите «Создать всё равно» ещё раз."

FORCE_ROLLBACK_TEXT = (
    "Не удалось завершить обработку. Не отвечайте на последний вопрос — "
    "предыдущий диалог восстановлен."
)

EDIT_ROLLBACK_TEXT = (
    "Редактирование не началось. Предыдущий диалог остаётся активным — "
    "ответьте на его последний вопрос."
)

# Интервал продления аренды. Держится меньше CLAIM_TIMEOUT_SECONDS / 3,
# чтобы даже пара пропущенных ударов не отдала живую аренду другому воркеру.
HEARTBEAT_INTERVAL = 15

# Сверка после таймаута CRM: сколько раз и с какой паузой искать сделку,
# которую сервер мог успеть создать, не успев ответить до дедлайна.
# CRM_DEADLINE + RECONCILE_DEADLINE остаются меньше CLAIM_TIMEOUT_SECONDS:
# аренда переживает и запись, и сверку.
RECONCILE_ATTEMPTS = 3
RECONCILE_DELAY = 2.0
RECONCILE_DEADLINE = 15


async def _reconcile(finder: Callable[[], Awaitable[int | None]]) -> int | None:
    """Несколько попыток найти сущность, которую CRM могла создать без ответа.

    Общий механизм сверки после неоднозначного сбоя *.add (таймаут, обрыв):
    запрос мог быть принят сервером, хотя ответ не пришёл. Разрешить
    немедленный retry с новым add значило бы создать дубль — сущность
    ищется по идемпотентному ключу несколько раз с паузой.
    """
    try:
        async with asyncio.timeout(RECONCILE_DEADLINE):
            for attempt in range(RECONCILE_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(RECONCILE_DELAY)
                try:
                    found = await finder()
                except Exception:
                    log.exception("Сверка после сбоя не удалась (попытка %d)", attempt + 1)
                    continue
                if found is not None:
                    return found
    except TimeoutError:
        log.warning("Сверка по ключу не уложилась в %s секунд", RECONCILE_DEADLINE)
    return None


async def _reconcile_deal(bitrix: BitrixClient, key: str) -> int | None:
    """Ищет сделку, которую CRM могла создать до таймаута ответа deal.add."""
    return await _reconcile(lambda: find_deal_by_key(bitrix, key))


# Дедлайн best-effort шагов ПОСЛЕ созданной сделки (дело CRM + запись
# Telegram-напоминания): сделка уже зафиксирована, эти шаги не должны
# блокировать ответ и не имеют права уронить обработчик.
POST_DEAL_DEADLINE = 15


async def _schedule_deal_reminder(
    message: Message,
    db: Database,
    bitrix: BitrixClient | None,
    order: ParsedOrder,
    deal_id: int,
) -> None:
    """Напоминания о сроке созданной заявки: дело CRM + Telegram.

    Вызывается после фиксации сделки, поэтому любой сбой здесь только
    логируется. Дело (crm.activity.todo) даёт родной mobile-push Bitrix;
    Telegram-напоминание — гарантированный канал, он не зависит от дела.
    Срок без времени напоминает утром (dates.DEFAULT_REMINDER_HOUR).
    """
    due_ts = dates.reminder_epoch(order.deadline)
    if due_ts is None or due_ts <= int(dates.now_local().timestamp()):
        # Срока нет или он уже в прошлом: напоминать не о чем.
        return
    try:
        if await db.pending_deal_reminder(deal_id) is not None:
            # Напоминание уже стоит: сделку зафиксировал параллельный путь.
            return
    except Exception:
        log.exception("Проверка существующего напоминания сделки %s не удалась", deal_id)
    activity_id = None
    if bitrix is not None:
        try:
            async with asyncio.timeout(POST_DEAL_DEADLINE):
                activity_id = await create_deal_todo(
                    bitrix,
                    deal_id,
                    title=f"Заявка №{deal_id}: {order.problem}",
                    deadline_iso=dates.epoch_to_iso(due_ts),
                    responsible_id=settings.bitrix_responsible_id,
                )
        except Exception:
            log.exception("Дело-напоминание для сделки %s не создано", deal_id)
    try:
        await db.add_reminder(
            message.chat.id,
            text=(
                f"заявка №{deal_id} — {order.problem}. "
                f"Срок: {dates.format_deadline(order.deadline)}"
            ),
            due_ts=due_ts,
            kind="deal",
            entity_id=deal_id,
            activity_id=activity_id,
        )
    except Exception:
        log.exception("Telegram-напоминание для сделки %s не записано", deal_id)


async def _schedule_task_reminder(
    message: Message,
    db: Database,
    order: ParsedOrder,
    task_id: int,
    deal_label: str | None = None,
) -> None:
    """Telegram-напоминание к задаче-напоминанию (intent=reminder).

    Идемпотентно по задаче (db.spawn_task_reminder): вызывается на ЛЮБОМ
    пути, где задача существует, включая «ответ task.add потерялся, сверка
    нашла id» — раньше этот путь оставлял очередь пустой, хотя пользователю
    обещан пинг. Повторная доставка сообщения дубля не создаёт, отменённый
    пользователем пинг не воскрешает.

    deal_label — подпись заявки для привязанного напоминания: пинг и список
    «Мои напоминания» называют заявку, чтобы было ясно, о чём речь.
    """
    due_ts = dates.reminder_epoch(order.deadline)
    if due_ts is None or due_ts <= int(dates.now_local().timestamp()):
        return
    body = order.problem
    if deal_label:
        body = f"{body} (заявка {deal_label})"
    try:
        await db.spawn_task_reminder(
            task_id,
            message.chat.id,
            text=f"{body}. Срок: {dates.format_deadline(order.deadline)}",
            due_ts=due_ts,
        )
    except Exception:
        log.exception("Telegram-напоминание для задачи %s не записано", task_id)


async def _freeze_draft_unknown(db: Database, draft_id: str, token: str, key: str) -> bool:
    """Переводит черновик в creation_unknown под asyncio.shield.

    Вызывается, когда deal.add отправлен, а его исход неизвестен. shield
    гарантирует, что запись состояния завершится, даже если задачу
    обработчика отменили: иначе finally освободил бы черновик под новый
    deal.add и повтор мог бы создать дубль. False — заморозка не случилась:
    либо аренда потеряна (черновиком владеет другой воркер), либо черновик
    уже зафиксирован как done (complete_draft под shield успел раньше) —
    понижать терминальное состояние нельзя.
    """
    frozen, cancelled = await _complete_despite_cancellation(
        db.mark_draft_unknown(draft_id, token, key)
    )
    if not frozen:
        log.warning(
            "Черновик %s не заморожен: аренда потеряна или статус уже терминальный", draft_id
        )
    if cancelled:
        raise asyncio.CancelledError
    return frozen


async def _heartbeat(
    db: Database,
    draft_id: str,
    token: str,
    stop_event: asyncio.Event,
    lease_lost: asyncio.Event,
    interval: float = HEARTBEAT_INTERVAL,
) -> None:
    """Продлевает аренду черновика, пока идёт запись в CRM.

    Если продлить не удалось, аренда потеряна (её перехватил другой воркер
    после нашего зависания): ставится lease_lost и heartbeat завершается —
    писать в CRM по чужой аренде нельзя.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            if not await db.refresh_claim(draft_id, token):
                log.warning("Аренда черновика %s потеряна, прекращаю heartbeat", draft_id)
                lease_lost.set()
                return


# Межчерновиковые замки чата: сериализация записи в CRM по кнопке «Создать».
# Aiogram 3.29 по умолчанию НЕ изолирует события (DisabledEventIsolation):
# два быстрых callback одного чата бегут параллельно, и две карточки одного
# сообщения (двойной cat:*-клик) могли конкурентно пройти предпроверку по
# UF_CRM_TG_MSG_ID до первого deal.add — получались две сделки с одним
# ключом. Замок держится только на критической секции CRM-записи (внутри
# CRM_DEADLINE — ожидание замка тоже им ограничено). Однопроцессного замка
# достаточно: бот развёрнут в одном инстансе (Railway), а межпроцессные
# гонки закрывают предпроверка и сверка по ключу.
@dataclass
class _ChatLockEntry:
    lock: asyncio.Lock
    users: int = 0


_chat_locks: dict[int, _ChatLockEntry] = {}


@asynccontextmanager
async def _chat_lock(chat_id: int):
    """Замок чата для критической секции «предпроверка дубля + deal.add»."""
    entry = _chat_locks.get(chat_id)
    if entry is None:
        entry = _chat_locks[chat_id] = _ChatLockEntry(asyncio.Lock())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _chat_locks.get(chat_id) is entry:
            del _chat_locks[chat_id]


class OrderFlow(StatesGroup):
    """Состояния потока заявки: превью, уточнения, пошаговый опросник."""

    preview = State()
    ask_phone = State()
    ask_category = State()
    form_name = State()
    form_phone = State()
    form_category = State()
    form_source = State()
    form_problem = State()
    form_deadline = State()


# ---------------------------------------------------------------------------
# Клавиатуры и карточка
# ---------------------------------------------------------------------------


def preview_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    """Кнопки карточки. draft_id в callback_data привязывает их к черновику."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Создать", callback_data=f"order:create:{draft_id}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"order:edit:{draft_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"order:cancel:{draft_id}"),
            ]
        ]
    )


def force_create_keyboard(token: str) -> InlineKeyboardMarkup:
    """Кнопка «Создать всё равно» под предупреждением о вероятном дубле."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=FORCE_CREATE_BUTTON, callback_data=f"dup:force:{token}")]
        ]
    )


def category_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=cat.value, callback_data=f"cat:{cat.value}")]
        for cat in Category
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_keyboard() -> InlineKeyboardMarkup:
    """4 кнопки источника заявки (Авито, Форпост, Сарафанное радио, Прочее)."""
    rows = [
        [InlineKeyboardButton(text=src.value, callback_data=f"src:{src.value}")]
        for src in Source
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


CANCEL_INPUT_CB = "flow:cancel"

CANCELLED_TEXT = "Ввод отменён. Пришлите новую заявку текстом или голосом."

NOTHING_TO_CANCEL = "Сейчас нечего отменять."


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка «Отмена»: оборвать уточнения или опросник на любом шаге."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_INPUT_CB)]
        ]
    )


def with_cancel(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Дописывает кнопку «Отмена» последней строкой существующей клавиатуры."""
    rows = list(markup.inline_keyboard) + [
        [InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_INPUT_CB)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_text(order: ParsedOrder) -> str:
    lines = [
        "Проверьте заявку:",
        f"Клиент: {order.client_name or 'не указан'}",
        f"Телефон: {order.phone or 'не указан'}",
    ]
    if order.org:
        lines.append(f"Организация: {order.org}")
    if order.address:
        lines.append(f"Адрес: {order.address}")
    lines.append(f"Категория: {order.category.value if order.category else 'не указана'}")
    # Источник показывается всегда: если он не назван, при записи в CRM
    # подставится «Прочее» — карточка честно предупреждает об этом.
    lines.append(f"Источник: {order.source.value if order.source else Source.other.value}")
    lines.append(f"Описание: {order.problem}")
    if order.deadline:
        lines.append(f"Срок: {dates.format_deadline(order.deadline)}")
    if order.income_rub is not None:
        lines.append(f"Доход: {order.income_rub:g} руб.")
    if order.expense_rub is not None:
        lines.append(f"Расход: {order.expense_rub:g} руб.")
    if order.profit_rub is not None:
        lines.append(f"Прибыль: {order.profit_rub:g} руб.")
    if order.urgency:
        lines.append(f"Срочность: {order.urgency}")
    if order.comment:
        lines.append(f"Комментарий: {order.comment}")
    return "\n".join(lines)


def build_deal_fields(order: ParsedOrder) -> dict[str, Any]:
    """Поля сделки Bitrix24 из разобранной заявки."""
    category = order.category.value if order.category else "прочее"
    fields: dict[str, Any] = {"TITLE": f"{category}: {order.problem}"[:255]}
    if order.category:
        # Категория дублируется в отдельное поле сделки: по нему в CRM
        # работают фильтры и отчёты, а TITLE остаётся человекочитаемым.
        fields[UF_SERVICE_CATEGORY] = order.category.value
    # Источник — родное поле SOURCE_ID (отдельная колонка в CRM). Не назван —
    # «Прочее»: значения справочника готовит ensure_sources при старте.
    fields["SOURCE_ID"] = (
        SOURCE_ID_BY_NAME.get(order.source.value, DEFAULT_SOURCE_ID)
        if order.source
        else DEFAULT_SOURCE_ID
    )
    if order.income_rub is not None:
        fields["OPPORTUNITY"] = order.income_rub
    if order.expense_rub is not None:
        fields[UF_EXPENSE] = order.expense_rub
    if order.profit_rub is not None:
        fields[UF_PROFIT] = order.profit_rub
    comments = []
    if order.org:
        # Организация дублируется в комментарий сделки: по нему работает
        # текстовый поиск «/find <организация>» (%COMMENTS) — у контакта
        # штатного поля организации нет (она в UF-поле контакта).
        comments.append(f"Организация: {order.org}")
    if order.address:
        comments.append(f"Адрес: {order.address}")
    if order.deadline:
        # В комментарии сделки срок хранится в читаемом виде дд.мм.гггг чч:мм.
        comments.append(f"Срок: {dates.format_deadline(order.deadline)}")
    if order.urgency:
        comments.append(f"Срочность: {order.urgency}")
    if order.comment:
        comments.append(order.comment)
    if comments:
        fields["COMMENTS"] = "\n".join(comments)
    return fields


def _draft_id_from(callback_data: str) -> str | None:
    """Достаёт draft_id из callback_data вида order:<действие>:<draft_id>."""
    parts = callback_data.split(":", 2)
    if len(parts) == 3 and parts[2]:
        return parts[2]
    return None


async def _load_draft(callback: CallbackQuery, db: Database) -> dict[str, Any] | None:
    """Черновик по кнопке или None (тогда пользователю уже отвечено).

    Кнопки работают только для автора карточки в её чате: в группе чужое
    нажатие не создаёт, не меняет и не отменяет чужую заявку.
    """
    draft_id = _draft_id_from(callback.data or "")
    draft = await db.get_draft(draft_id) if draft_id else None
    if draft is None:
        await callback.answer()
        await callback.message.answer(STALE_CARD)
        return None
    if callback.from_user.id != draft["user_id"] or callback.message.chat.id != draft["chat_id"]:
        await callback.answer(FOREIGN_CARD, show_alert=True)
        return None
    return draft


# ---------------------------------------------------------------------------
# Вход: свободный текст заявки
# ---------------------------------------------------------------------------


@router.message(StateFilter(None, OrderFlow.preview), F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
    dedup_key: str = "",
) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    await handle_order_text(
        message,
        state,
        db,
        text=message.text or "",
        user_id=user_id,
        dedup_key=dedup_key,
        bitrix=bitrix,
    )


async def handle_order_text(
    message: Message,
    state: FSMContext,
    db: Database,
    *,
    text: str,
    user_id: int,
    dedup_key: str,
    bitrix: BitrixClient | None = None,
    phone_asked: bool = False,
) -> None:
    """Общий вход текста заявки: контент-дедуп, затем штатная обработка.

    Сюда приходят свободный текст (on_text), свежая фраза вместо ответа на
    вопрос о телефоне (ask_phone_step) и распознанные голосовые
    (handlers/voice).

    Второй уровень дедупа — по содержимому. Точный ключ (msg:/fwd:) ловит
    только повтор той же доставки; перепечатанный заново текст приходит с
    новым message_id и первым уровнем не отсекается. Хэш проверяется и
    занимается ОДНОЙ транзакцией до обращения к модели: два одинаковых
    текста, пришедших почти одновременно, не проскочат оба за время
    парсинга, и токены на вероятный дубль не тратятся. Отказ мягкий:
    похожий текст может оказаться другой реальной заявкой, поэтому вместо
    блокировки — предупреждение и кнопка «Создать всё равно».
    """
    dup = await db.claim_content(message.chat.id, content_hash(text), dedup_key)
    if dup is not None:
        token = uuid4().hex
        # phone_asked сохраняется вместе с текстом: обработка по кнопке
        # «Создать всё равно» не должна спрашивать телефон второй раз.
        await db.save_pending_text(
            token, message.chat.id, user_id, text, dedup_key, phone_asked=phone_asked
        )
        if dup["deal_id"]:
            warning = DUP_WITH_DEAL.format(deal_id=dup["deal_id"])
        else:
            warning = DUP_NO_DEAL
        await message.answer(warning, reply_markup=force_create_keyboard(token))
        return

    await _process_order_text(
        message,
        state,
        db,
        text=text,
        user_id=user_id,
        dedup_key=dedup_key,
        content_claimed=True,
        bitrix=bitrix,
        phone_asked=phone_asked,
    )


# Замена db.save_draft с тем же набором полей, возвращающая признак успеха:
# путь «Создать всё равно» передаёт так fencing-переход из отложенного текста
# в черновик (см. _continue_flow и on_force_create).
DraftSaver = Callable[..., Awaitable[bool]]


async def _process_order_text(
    message: Message,
    state: FSMContext,
    db: Database,
    *,
    text: str,
    user_id: int,
    dedup_key: str,
    content_claimed: bool,
    bitrix: BitrixClient | None = None,
    phone_asked: bool = False,
    draft_saver: DraftSaver | None = None,
    owner_check: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Штатная обработка текста заявки: модель, уточнения, карточка.

    phone_asked=True означает, что вопрос о телефоне уже задавался (переразбор
    свежей фразы из ask_phone_step): второй раз он не задаётся ни в одной
    ветке — ни перед карточкой, ни в пошаговом опроснике; карточка
    показывается и без номера — телефон запрашивается максимум один раз.

    Вызывается из handle_order_text (контент-хэш уже занят,
    content_claimed=True) и из кнопки «Создать всё равно» (обход
    контент-проверки, захвата нет). Захват хэша остаётся, только если текст
    реально начал заявку — показана карточка, задан уточняющий вопрос,
    запущен опросник или создана задача-напоминание. «Не заявка»,
    несостоявшееся напоминание (нет CRM, сбой записи) и любой сбой обработки
    освобождают захват: повтор такого текста не должен считаться дублем.

    Возвращает признак «обработка завершена» для force-flow: False — только
    когда напоминание не записалось по исправимой причине и повторное
    нажатие кнопки должно запустить обработку заново (запись pending
    сохраняется). Все остальные исходы, включая «не заявка», возвращают True.

    owner_check — проверка владения арендой отложенного текста (путь кнопки
    «Создать всё равно»). Выполняется сразу ПОСЛЕ разбора модели и ДО любого
    видимого пользователю действия или записи FSM: обработчик, потерявший
    аренду за время разбора, выходит тихо ЛЮБОЙ веткой — без карточки, без
    уточняющего вопроса и без запуска опросника.
    """
    order_started = False

    def _mark_order_started() -> None:
        # Точка невозврата: черновик записан в SQLite или задача-напоминание
        # существует в CRM. С этого момента захват контент-хэша держится,
        # даже если последующая отправка ответа упала или задачу отменили:
        # Telegram мог принять сообщение без ответа клиенту, и освобождённый
        # захват позволил бы повтору текста молча создать дубль.
        nonlocal order_started
        order_started = True

    try:
        try:
            orders = await llm.parse_orders(text)
        except llm.LLMUnavailable:
            if owner_check is not None and not await owner_check():
                # Аренда потеряна за время разбора: ни вопроса, ни записи
                # FSM — текстом уже занимается новый владелец.
                return True
            # Вступление отправляется ДО записи состояния: если оно не ушло,
            # чат не заперт в невидимом опроснике (следующий текст не станет
            # молча «именем клиента»), а захват хэша снимется в finally.
            await message.answer(
                "Не получилось разобрать сообщение автоматически, соберу заявку "
                "по шагам.\nВопрос 1 из 6. Как зовут клиента?",
                reply_markup=cancel_keyboard(),
            )
            await state.set_state(OrderFlow.form_name)
            data: dict[str, Any] = {"order": {}, "dedup_key": dedup_key, "user_id": user_id}
            if content_claimed:
                # Для кнопки «Отмена»: занятый контент-хэш освобождается, и
                # тот же текст после отмены не считается дублем.
                data["content_hash"] = content_hash(text)
                data["content_claim_key"] = dedup_key
            if phone_asked:
                # Телефон уже спрашивали до падения в опросник: шаг с
                # вопросом о номере будет пропущен (см. form_name_step).
                data["phone_skipped"] = True
            await state.set_data(data)
            order_started = True
            return True

        if owner_check is not None and not await owner_check():
            # Аренда потеряна за время разбора: выходим до любого ответа
            # и до записи состояния — включая ветки уточнений и карточку.
            return True

        if not orders:
            await message.answer(
                "Пришлите текст заявки, например: "
                "«Иван, 89141234567, сантехника, замена крана, завтра, 5000 руб»."
            )
            return True

        # Срок нормализуется детерминированно: относительные обороты в самом
        # сообщении пересчитываются кодом от текущего времени (settings.tz),
        # арифметика модели им не доверяется. Для нескольких заявок в одном
        # сообщении общий текст не используется: у каждой свой срок от модели.
        now = dates.now_local()
        deadline_text = text if len(orders) == 1 else ""
        orders = [
            parsed.model_copy(
                update={
                    "deadline": dates.resolve_deadline(parsed.deadline, deadline_text, now)
                }
            )
            for parsed in orders
        ]

        key = dedup_key or f"msg:{uuid4().hex}"
        order = orders[0]
        if len(orders) > 1:
            await message.answer(
                MULTIPLE_ORDERS_TEXT.format(client=order.client_name or "не указан")
            )

        if order.intent is Intent.other:
            reply = ORDER_CHANGES_TEXT if order.existing_order_change else OTHER_MESSAGE_TEXT
            await message.answer(reply)
            return True

        if order.intent is Intent.reminder:
            # Напоминание — не сделка: карточка и черновик не нужны, вместо
            # них создаётся задача в Bitrix24 (tasks.task.add).
            #
            # Привязка к заявке здесь — только по однозначной фразе в самом
            # тексте («к последней заявке», «к заявке 154», по телефону):
            # вопрос-уточнение живёт в явном флоу кнопки «Напоминание», а
            # свободный текст остаётся одношаговым — отложенное создание
            # ломало бы контент-дедуп (см. _mark_order_started). Не нашлась
            # заявка однозначно — честное обычное напоминание с пояснением.
            await state.clear()
            ref = None
            if len(orders) == 1:
                # Инлайн-привязка только для одиночного напоминания: при
                # нескольких фраза «к заявке …» могла относиться к любому
                # из них, а создаётся здесь только первое (ревью R3).
                _, ref = binding.extract_inline_binding(text)
                cleaned_problem, problem_ref = binding.extract_inline_binding(
                    order.problem or ""
                )
                if problem_ref is not None and cleaned_problem:
                    # Фраза привязки — служебная, в заголовок задачи не идёт.
                    order = order.model_copy(update={"problem": cleaned_problem})
            deal_id = None
            deal_label = None
            inline_miss = False
            if bitrix is not None and ref is not None and ref.kind != "none":
                deal = None
                try:
                    async with asyncio.timeout(POST_DEAL_DEADLINE):
                        found, truncated = await find_ref_deals(bitrix, ref)
                    if len(found) == 1 and not truncated:
                        deal = found[0]
                except Exception:
                    log.exception("Поиск заявки для привязки из текста не удался")
                if deal is not None:
                    deal_id = int(deal["ID"])
                    deal_label = await deal_binding_label(bitrix, deal)
                else:
                    inline_miss = True
            created = await _create_reminder(
                message,
                db,
                bitrix,
                order,
                key=key,
                deal_id=deal_id,
                deal_label=deal_label,
                on_task_settled=_mark_order_started,
            )
            if created and deal_label:
                due_ts = dates.reminder_epoch(order.deadline)
                if due_ts is not None:
                    await message.answer(
                        REMIND_SCHEDULED_DEAL.format(
                            when=dates.format_epoch(due_ts), deal=deal_label
                        )
                    )
            elif created and inline_miss:
                await message.answer(BIND_INLINE_MISS)
            return created

        if order.phone:
            # модель возвращает телефон как в тексте; мусор превращается в None
            order = order.model_copy(update={"phone": normalize_phone(order.phone)})
        data: dict[str, Any] = {
            "order": order.model_dump(mode="json"),
            "dedup_key": key,
            "user_id": user_id,
        }
        if content_claimed:
            # Ключ и хэш занятого контент-дедупа: кнопка «Отмена» на
            # уточняющих вопросах освобождает захват (см. _cancel_flow).
            data["content_hash"] = content_hash(text)
            data["content_claim_key"] = dedup_key
        if phone_asked:
            data["phone_skipped"] = True
        await state.set_data(data)
        await _continue_flow(
            message, state, db, draft_saver, on_order_started=_mark_order_started
        )
        order_started = True
        return True
    finally:
        if content_claimed and not order_started:
            # Заявка не началась: захват хэша снимается, чтобы повторная
            # отправка того же текста не считалась дублем. Выполняется и при
            # исключении (включая отмену задачи) — упавший вход не блокирует
            # текст на окно дедупа.
            await db.release_content(message.chat.id, content_hash(text), dedup_key)


async def _complete_despite_cancellation(awaitable: Awaitable[Any]) -> tuple[Any, bool]:
    """Доводит восстановимую операцию до конца и сообщает об отмене снаружи."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled
        except BaseException as exc:
            if cancelled:
                raise asyncio.CancelledError from exc
            raise


async def _await_cancellation_safe(awaitable: Awaitable[Any]) -> Any:
    """Возвращает результат только после завершения операции, затем отменяется."""
    result, cancelled = await _complete_despite_cancellation(awaitable)
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _restore_fsm(
    state: FSMContext, snapshot: tuple[str | None, dict[str, Any]]
) -> None:
    """Возвращает состояние и данные диалога к согласованному снимку."""
    previous_state, previous_data = snapshot
    await state.set_state(previous_state)
    await state.set_data(deepcopy(previous_data))


async def _send_edit_rollback(message: Message) -> None:
    """Компенсирует уже доставленный edit-вопрос после отката FSM."""
    await message.answer(EDIT_ROLLBACK_TEXT)


async def _revert_force_transition(
    db: Database,
    state: FSMContext,
    snapshot: tuple[str | None, dict[str, Any]],
    token: str,
    owner: str,
    pending: dict[str, Any],
    draft_id: str | None,
) -> None:
    """Возвращает отложенный текст под кнопку после сбоя force-flow.

    Если fencing-переход уже превратил текст в черновик, а карточка не
    дошла до пользователя, переход откатывается: черновик (ещё open и не
    захваченный) удаляется, запись с текстом восстанавливается под тем же
    токеном — повторный клик по кнопке снова покажет карточку, а не
    «Карточка устарела» при orphan-черновике в базе. Захваченный черновик
    не трогается: карточка всё же дошла, и ею уже занимается «Создать».
    Без перехода просто снимается аренда. FSM восстанавливается только после
    доказанного успеха DB-компенсации: иначе он противоречил бы оставшемуся
    черновику или захваченному pending-тексту.
    """
    if draft_id is None:
        restored = await db.release_pending_text(token, owner)
    else:
        restored = await db.rollback_pending_draft(
            token,
            owner,
            draft_id,
            pending["chat_id"],
            pending["user_id"],
            pending["text"],
            pending["dedup_key"],
            pending["phone_asked"],
        )
    if not restored:
        raise RuntimeError(f"Force-компенсация {token} не зафиксирована")
    if draft_id is not None:
        log.info("Force-переход откатен: текст %s снова ждёт кнопку", token)
    await _restore_fsm(state, snapshot)


async def _delete_pending_terminal(db: Database, token: str, owner: str) -> bool:
    """Удаляет terminal pending с коротким повтором безопасной локальной операции."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            return await db.delete_pending_text(token, owner)
        except Exception:  # noqa: BLE001 - delete идемпотентен, повтор безопасен
            if attempt == attempts:
                raise
            log.warning(
                "Terminal cleanup текста %s не удался, повтор %s/%s",
                token,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(0)
    return False


async def _rollback_force_safely(
    db: Database,
    state: FSMContext,
    snapshot: tuple[str | None, dict[str, Any]],
    token: str,
    owner: str,
    pending: dict[str, Any],
    draft_id: str | None,
) -> None:
    """Откатывает DB и FSM до проброса любой пришедшей отмены."""
    _, cancelled = await _complete_despite_cancellation(
        _revert_force_transition(
            db, state, snapshot, token, owner, pending, draft_id
        )
    )
    if cancelled:
        raise asyncio.CancelledError


def _force_flow_timeout():
    """Контекст общего дедлайна force-flow; вынесен для детерминированного теста."""
    return asyncio.timeout(FORCE_FLOW_DEADLINE)


async def find_ref_deals(
    bx: BitrixClient, ref: BindingRef
) -> tuple[list[dict[str, Any]], bool]:
    """Заявки по однозначной ссылке (последняя/номер/телефон) для привязки.

    Свободный текстовый поиск (kind="text") здесь не поддерживается — его
    делает шаг привязки в handlers/reminders поверх поискового ядра.
    """
    if ref.kind == "last":
        return await recent_deals(bx, 1), False
    if ref.kind == "deal_id":
        deal = await get_deal(bx, int(ref.value))
        return ([deal] if deal else []), False
    if ref.kind == "phone":
        found = await search_deals_by_phone(bx, ref.value)
        return found.deals, found.truncated
    return [], False


async def deal_binding_label(bitrix: BitrixClient | None, deal: dict) -> str:
    """Подпись заявки для пинга и подтверждения: №, клиент, название.

    Имя клиента — best-effort: сбой чтения контакта не должен ронять
    постановку напоминания, подпись просто короче.
    """
    client = ""
    try:
        contact_id = int(deal.get("CONTACT_ID") or 0)
    except (TypeError, ValueError):
        contact_id = 0
    if bitrix is not None and contact_id:
        try:
            async with asyncio.timeout(POST_DEAL_DEADLINE):
                names = await contact_names(bitrix, [contact_id])
            client = names.get(contact_id, "")
        except Exception:
            log.warning("Имя контакта заявки %s не прочитано", deal.get("ID"))
    title = str(deal.get("TITLE") or "").strip()
    parts = [f"№{deal.get('ID')}"] + [part for part in (client, title) if part]
    return " · ".join(parts)


async def _create_reminder(
    message: Message,
    db: Database,
    bitrix: BitrixClient | None,
    order: ParsedOrder,
    *,
    key: str,
    deal_id: int | None = None,
    deal_label: str | None = None,
    on_task_settled: Callable[[], None] | None = None,
) -> bool:
    """Идемпотентно создаёт задачу-напоминание в Bitrix24.

    deal_id/deal_label — привязка к заявке: задача связывается со сделкой
    (UF_CRM_TASK), а Telegram-пинг называет заявку по подписи.

    Тот же рисунок, что у сделок: ключ сообщения хранится в самой задаче
    (тегом, см. services/tasks.py), перед созданием выполняется предпроверка
    по ключу, а сбой ПОСЛЕ отправки task.add считается неоднозначным —
    задача могла записаться без ответа. Тогда идёт сверка по ключу; если
    задача так и не нашлась, пользователю честно сообщается об этом, а
    захват контент-хэша СОХРАНЯЕТСЯ: повтор того же текста предупредит о
    вероятном дубле, и вторую задачу создаст только явное «Создать всё
    равно» — молчаливого дубля не бывает.

    on_task_settled вызывается в точке, где задача существует или могла
    записаться, ДО отправки ответов пользователю: вызывающий фиксирует
    захват контент-хэша, и ни сбой, ни отмена (CancelledError) на
    подтверждении уже не освободят его — иначе повтор текста с новым
    message_id получил бы новый тег и молча создал бы вторую задачу.

    Возвращает признак «обработка завершена»: True — задача записана либо
    исход неоднозначен (захват остаётся, pending-текст force-flow удаляется);
    False — задачи в CRM точно нет (не подключена, сбой ДО отправки add,
    явный отказ сервера): захват снимается, pending-текст остаётся под
    повторное нажатие.
    """
    if bitrix is None:
        await message.answer(REMINDER_NO_CRM)
        return False
    title = (order.problem or "").strip()
    # Первая буква — заглавная: заголовок задачи читается как фраза.
    title = title[:1].upper() + title[1:] if title else "Напоминание"
    add_sent = False
    try:
        async with asyncio.timeout(CRM_DEADLINE):
            fence = await db.get_or_create_task_fence(key)
            if fence["status"] == "done":
                task_id = int(fence["task_id"])
            elif fence["status"] == "sent":
                # Предыдущий task.add мог пройти без ответа. Постоянный fence
                # запрещает новую отправку даже после рестарта процесса.
                add_sent = True
                if on_task_settled is not None:
                    on_task_settled()
                task_id = await _reconcile(lambda: find_reminder_task(bitrix, key))
                if task_id is None:
                    await message.answer(REMINDER_UNKNOWN_TEXT)
                    return True
            else:
            # Предпроверка идемпотентности: задача с этим ключом могла быть
            # создана раньше (повторная доставка после сбоя подтверждения).
                task_id = await find_reminder_task(bitrix, key)
                if task_id is None:
                    add_sent = True
                    if not await db.mark_task_fence_sent(key):
                        task_id = await _reconcile(
                            lambda: find_reminder_task(bitrix, key)
                        )
                        if task_id is None:
                            if on_task_settled is not None:
                                on_task_settled()
                            await message.answer(REMINDER_UNKNOWN_TEXT)
                            return True
                    else:
                        try:
                            task_id = await create_reminder_task(
                                bitrix,
                                title,
                                deadline=order.deadline,
                                deal_id=deal_id,
                                key=key,
                            )
                        except Exception as exc:
                            if is_server_refusal(exc):
                                # Сбрасывается только граница именно этого
                                # отклонённого task.add, но не ошибок list/reconcile.
                                await _await_cancellation_safe(
                                    db.reset_task_fence(key)
                                )
                                add_sent = False
                            raise
                else:
                    log.info("Задача с ключом уже есть: id=%s, дубль не создаю", task_id)
    except asyncio.CancelledError:
        # Отмена (шатдаун) после отправки task.add: задача могла записаться
        # без ответа. Захват фиксируется ДО проброса отмены — повтор того же
        # текста предупредит о вероятном дубле, а не создаст вторую задачу.
        # Отмена до отправки add захват не трогает: задачи точно нет.
        if add_sent and on_task_settled is not None:
            on_task_settled()
        raise
    except Exception as exc:
        if not add_sent:
            # Сбой строго ДО отправки task.add (предпроверка, дедлайн на
            # ней) либо явный отказ сервера: задачи в CRM точно нет,
            # немедленный повтор безопасен.
            log.exception("Не удалось создать задачу-напоминание")
            await message.answer(REMINDER_FAILED)
            return False
        # task.add отправлен, ответа нет (таймаут, обрыв): задача могла
        # записаться. Сверяемся по ключу-тегу, как сделки по UF-полю.
        log.warning(
            "Исход task.add неизвестен (%s), сверяю по ключу", type(exc).__name__
        )
        # С этого момента повтор запрещён постоянным fence. Фиксируем захват
        # до любого await сверки: отмена во время sleep/list его не откроет.
        if on_task_settled is not None:
            on_task_settled()
        task_id = await _reconcile(lambda: find_reminder_task(bitrix, key))
        if task_id is None:
            # Задача МОГЛА записаться: захват фиксируется до ответа.
            await message.answer(REMINDER_UNKNOWN_TEXT)
            return True
        log.info("Задача id=%s нашлась при сверке после сбоя task.add", task_id)
    # Задача существует: захват фиксируется ДО отправки подтверждения, чтобы
    # ни сбой локального fence, ни отмена на нём/answer не освободили его.
    if on_task_settled is not None:
        on_task_settled()
    await asyncio.shield(db.complete_task_fence(key, task_id))
    # Гарантированный канал: бот сам напишет в Telegram в момент срока.
    # Ставится на любом пути существования задачи (в т.ч. найденной сверкой
    # после потерянного ответа add) — вставка идемпотентна по задаче.
    await _schedule_task_reminder(message, db, order, task_id, deal_label=deal_label)
    try:
        await message.answer(REMINDER_CREATED.format(task_id=task_id))
    except Exception:
        # Задача уже создана: сбой подтверждения не должен снимать захват
        # контент-хэша, иначе повтор текста молча создал бы вторую задачу.
        # Повтор того же текста ответит предупреждением о вероятном дубле.
        log.exception("Ответ о созданном напоминании не отправлен")
    return True


@router.callback_query(F.data.startswith("dup:force:"))
async def on_force_create(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    """«Создать всё равно»: продолжает обработку текста, отложенного дедупом.

    Кнопка слушается только автора в его чате (как кнопки карточки).
    Текст берётся в аренду по образцу черновиков (claim_token, CAS):
    двойное нажатие запускает обработку один раз, а удаляется запись только
    ПОСЛЕ успешной обработки — сбой Telegram или модели освобождает аренду,
    и повторное нажатие срабатывает, текст не теряется. Контент-проверка
    здесь не повторяется — сотрудник уже подтвердил, что это не дубль.

    Захват идёт первым действием: его транзакция заодно физически вычищает
    просроченные записи, поэтому и поздний клик по устаревшей кнопке не
    оставляет текст с именами и телефонами лежать в базе. Проверка автора —
    после захвата (данные о владельце лежат в самой записи); чужое нажатие
    возвращает аренду, не трогая запись.

    Инвариант «пока аренда активна, работает ровно один обработчик» держится
    структурно, а не таймингами: вся работа после захвата ограничена
    FORCE_FLOW_DEADLINE (строго меньше таймаута аренды) — перехват возможен
    только когда предыдущий обработчик гарантированно завершён или отменён;
    плюс проверка владения сразу после разбора модели (owner_check) отсекает
    потерявшего аренду до любого ответа и записи FSM. Поэтому из force-flow
    любой веткой (карточка, уточнение телефона/категории, опросник) возникает
    максимум один черновик и максимум одна сделка.

    Известное поведение (принятый остаток): если дедлайн прервал обработку
    уже после записи FSM (например, на самой отправке уточняющего вопроса),
    повторное нажатие может задать вопрос ещё раз — дубля сделки это не
    создаёт (fencing-переход, аренда drafts и предпроверка по ключу).
    """
    token = (callback.data or "").split(":", 2)[2]
    pending, busy = await db.claim_pending_text(token)
    if pending is None:
        if busy:
            # Текст уже обрабатывается параллельным нажатием той же кнопки.
            await callback.answer(CLAIM_IN_PROGRESS)
            return
        await callback.answer()
        await callback.message.answer(STALE_CARD)
        return
    owner = pending["claim_token"]
    if (
        callback.from_user.id != pending["user_id"]
        or callback.message.chat.id != pending["chat_id"]
    ):
        # Чужое нажатие: аренда возвращается, запись остаётся её автору.
        await db.release_pending_text(token, owner)
        await callback.answer(FOREIGN_CARD, show_alert=True)
        return
    fsm_snapshot = (
        await state.get_state(),
        deepcopy(await state.get_data()),
    )
    # Запоминает состоявшийся fencing-переход: при сбое доставки карточки
    # он откатывается (_revert_force_transition), а не оставляет orphan.
    transition: dict[str, str | None] = {"draft_id": None}

    async def fenced_draft(draft_id: str, **fields: Any) -> bool:
        # Черновик из отложенного текста создаётся ТОЛЬКО атомарным
        # fencing-переходом: CAS по владельцу аренды, удаление pending-строки
        # и вставка черновика — одна транзакция. Обработчик, чью аренду
        # перехватили после зависания, перехода не пройдёт и карточку не
        # покажет — два клика не могут породить два черновика и две сделки.
        # ID известен до DB-вызова: даже отмена после коммита сможет атомарно
        # найти и откатить созданный черновик.
        transition["draft_id"] = draft_id
        moved = await db.finalize_pending_to_draft(token, owner, draft_id, **fields)
        if not moved:
            transition["draft_id"] = None
        return moved

    async def lease_alive() -> bool:
        # Продление аренды и проверка владения одним CAS: потерянная аренда
        # означает, что текстом уже занимается другой клик — обработчик
        # выйдет тихо любой веткой (см. owner_check в _process_order_text).
        return await db.refresh_pending_claim(token, owner)

    try:
        # Дедлайн всей обработки строго меньше таймаута аренды (см. комментарий
        # к FORCE_FLOW_DEADLINE): к моменту, когда аренду можно перехватить,
        # этот обработчик гарантированно завершён или отменён.
        async with _force_flow_timeout():
            await callback.answer()
            finished = await _process_order_text(
                callback.message,
                state,
                db,
                text=pending["text"],
                user_id=pending["user_id"],
                dedup_key=pending["dedup_key"],
                content_claimed=False,
                bitrix=bitrix,
                phone_asked=pending["phone_asked"],
                draft_saver=fenced_draft,
                owner_check=lease_alive,
            )
    except TimeoutError:
        # Работа не уложилась в дедлайн: аренда снимается (а состоявшийся
        # fencing-переход откатывается), пока их никто не мог перехватить,
        # и пользователь получает понятный ответ — повторное нажатие
        # запустит обработку заново.
        await _rollback_force_safely(
            db, state, fsm_snapshot, token, owner, pending, transition["draft_id"]
        )
        await callback.message.answer(FORCE_TIMEOUT_TEXT)
        return
    except asyncio.CancelledError:
        # Отмена задачи (шатдаун) не должна терять текст: аренда снимается
        # (или откатывается fencing-переход), кнопка сработает после
        # перезапуска. shield доводит откат до конца при повторной отмене;
        # отмена пробрасывается дальше в любом случае.
        await _rollback_force_safely(
            db, state, fsm_snapshot, token, owner, pending, transition["draft_id"]
        )
        raise
    except Exception:
        # Сбой обработки (Telegram, модель, база) освобождает аренду и
        # откатывает состоявшийся fencing-переход: текст цел, повторное
        # нажатие запускает обработку заново, orphan-черновик без видимой
        # карточки не остаётся. Исключение уходит в глобальный @dp.error.
        await _rollback_force_safely(
            db, state, fsm_snapshot, token, owner, pending, transition["draft_id"]
        )
        raise
    if not finished:
        # Напоминание не записалось по исправимой причине (нет CRM, сбой до
        # отправки task.add): запись сохраняется, аренда снимается — кнопка
        # сработает повторно, а не ответит «карточка устарела».
        await _rollback_force_safely(
            db, state, fsm_snapshot, token, owner, pending, transition["draft_id"]
        )
        return
    # Устойчивый переход состоялся. Если текст дошёл до карточки, строка уже
    # удалена fencing-переходом вместе с созданием черновика (CAS ниже станет
    # no-op); для остальных исходов (уточняющий вопрос, опросник, «не заявка»,
    # напоминание) запись удаляется здесь — по владельцу аренды.
    try:
        deleted, cancelled = await _complete_despite_cancellation(
            _delete_pending_terminal(db, token, owner)
        )
    except BaseException:
        await _rollback_force_safely(
            db, state, fsm_snapshot, token, owner, pending, transition["draft_id"]
        )
        await callback.message.answer(FORCE_ROLLBACK_TEXT)
        raise
    if transition["draft_id"] is None and not deleted:
        raise RuntimeError("Terminal pending-текст потерял владельца до удаления")
    if cancelled:
        raise asyncio.CancelledError


async def _continue_flow(
    message: Message,
    state: FSMContext,
    db: Database,
    draft_saver: DraftSaver | None = None,
    on_order_started: Callable[[], None] | None = None,
) -> None:
    """Спрашивает недостающее (телефон, категорию) или показывает карточку.

    draft_saver — необязательная замена db.save_draft с теми же полями,
    возвращающая признак успеха: путь «Создать всё равно» передаёт сюда
    атомарный fencing-переход finalize_pending_to_draft. False означает,
    что аренду отложенного текста перехватил другой клик: черновик и
    карточку создаёт победитель, текущий обработчик выходит тихо.

    on_order_started вызывается сразу после ЗАПИСИ черновика, до отправки
    карточки: с этого момента заявка считается начатой, и сбой доставки
    карточки (Telegram мог принять её, не ответив) не освобождает захват
    контент-хэша — повтор того же текста предупредит о вероятном дубле,
    а не создаст второй черновик и вторую сделку.
    """
    data = await state.get_data()
    order_data = data["order"]
    # Уточняющие вопросы отправляются ДО перевода FSM: если вопрос не ушёл,
    # состояние не сдвинулось, и повтор текста начинает заново, а не отвечает
    # на вопрос, которого пользователь не видел.
    if not order_data.get("phone") and not data.get("phone_skipped"):
        await message.answer(
            "Не указан телефон клиента. Добавить сейчас? "
            "Пришлите номер или напишите «нет».",
            reply_markup=cancel_keyboard(),
        )
        await state.set_state(OrderFlow.ask_phone)
        return
    if not order_data.get("category"):
        await message.answer(
            "Не понял категорию услуги. Выберите:",
            reply_markup=with_cancel(category_keyboard()),
        )
        await state.set_state(OrderFlow.ask_category)
        return
    order = ParsedOrder.model_validate(order_data)
    draft_id = uuid4().hex
    if draft_saver is None:
        await db.save_draft(
            draft_id,
            chat_id=message.chat.id,
            user_id=data.get("user_id") or message.chat.id,
            parsed_json=order.model_dump_json(),
            dedup_key=data.get("dedup_key") or "",
        )
    elif not await draft_saver(
        draft_id,
        chat_id=message.chat.id,
        user_id=data.get("user_id") or message.chat.id,
        parsed_json=order.model_dump_json(),
        dedup_key=data.get("dedup_key") or "",
    ):
        # Fencing не пройден: аренду перехватил параллельный клик, черновик
        # уже создаёт (или создал) он. Ни карточки, ни записи в CRM отсюда.
        return
    if on_order_started is not None:
        # Черновик записан: заявка начата, захват контент-хэша с этого
        # момента держится даже при сбое отправки карточки ниже.
        on_order_started()
    await message.answer(preview_text(order), reply_markup=preview_keyboard(draft_id))
    await state.set_state(OrderFlow.preview)
    await state.update_data(draft_id=draft_id)


# ---------------------------------------------------------------------------
# Отмена ввода: кнопка «Отмена» на каждом шаге и команда /cancel
# ---------------------------------------------------------------------------

CANCELABLE_STATES = StateFilter(
    OrderFlow.preview,
    OrderFlow.ask_phone,
    OrderFlow.ask_category,
    OrderFlow.form_name,
    OrderFlow.form_phone,
    OrderFlow.form_category,
    OrderFlow.form_source,
    OrderFlow.form_problem,
    OrderFlow.form_deadline,
)


async def _cancel_flow(chat_id: int, state: FSMContext, db: Database) -> None:
    """Сбрасывает текущий ввод; недособранная заявка перестаёт считаться дублем.

    Контент-хэш освобождается, только пока карточки нет (черновик ещё не
    записан): тот же текст после отмены можно продиктовать заново. Показанную
    карточку отмена ввода не трогает — у карточки собственные кнопки.
    """
    data = await state.get_data()
    await state.clear()
    if not data.get("draft_id") and data.get("content_hash"):
        await db.release_content(
            chat_id, data["content_hash"], data.get("content_claim_key") or ""
        )


@router.callback_query(CANCELABLE_STATES, F.data == CANCEL_INPUT_CB)
async def on_cancel_input(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    await _cancel_flow(callback.message.chat.id, state, db)
    await callback.answer()
    await callback.message.answer(CANCELLED_TEXT)


@router.callback_query(F.data == CANCEL_INPUT_CB)
async def on_cancel_input_idle(callback: CallbackQuery) -> None:
    """Поздний клик по кнопке отмены: диалог уже свободен, ломать нечего."""
    await callback.answer(NOTHING_TO_CANCEL)


@router.message(CANCELABLE_STATES, Command("cancel"))
async def on_cancel_command(message: Message, state: FSMContext, db: Database) -> None:
    await _cancel_flow(message.chat.id, state, db)
    await message.answer(CANCELLED_TEXT)


@router.message(Command("cancel"))
async def on_cancel_command_idle(message: Message) -> None:
    await message.answer(NOTHING_TO_CANCEL)


# ---------------------------------------------------------------------------
# Уточняющие вопросы (телефон, категория)
# ---------------------------------------------------------------------------


async def ask_phone_step(
    message: Message,
    state: FSMContext,
    db: Database,
    *,
    text: str,
    bitrix: BitrixClient | None = None,
    dedup_key: str = "",
) -> None:
    """Ответ на вопрос о телефоне: номер, слово-пропуск или новая фраза.

    Ответом-номером считается ТОЛЬКО «голый» номер (extract_bare_phone):
    полная фраза с номером внутри («Мария, 89141234567, электрика, заменить
    розетку») — это исправленная/новая заявка, она переразбирается целиком,
    иначе номер подставился бы в старую заявку, а клиент и суть новой
    потерялись бы. Телефон при переразборе второй раз не спрашивается
    (phone_asked): карточка покажется и без номера.
    """
    text = (text or "").strip()
    data = await state.get_data()
    order_data = data["order"]
    if text.lower() in SKIP_WORDS or text == KEEP_WORD:
        order_data["phone"] = None
        await state.update_data(order=order_data, phone_skipped=True)
    else:
        phone = extract_bare_phone(text)
        if phone is None:
            # Не голый номер и не слово-пропуск: человек продолжил писать
            # обычным текстом. Вопрос не повторяется — фраза переразбирается
            # тем же путём, что и свободный текст.
            user_id = message.from_user.id if message.from_user else message.chat.id
            await handle_order_text(
                message,
                state,
                db,
                text=text,
                user_id=user_id,
                dedup_key=dedup_key,
                bitrix=bitrix,
                phone_asked=True,
            )
            return
        order_data["phone"] = phone
        await state.update_data(order=order_data)
    await _continue_flow(message, state, db)


@router.message(OrderFlow.ask_phone, F.text)
async def on_ask_phone(
    message: Message,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
    dedup_key: str = "",
) -> None:
    await ask_phone_step(
        message, state, db, text=message.text or "", bitrix=bitrix, dedup_key=dedup_key
    )


@router.callback_query(OrderFlow.ask_category, F.data.startswith("cat:"))
async def on_ask_category(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    order_data = data["order"]
    order_data["category"] = callback.data.split(":", 1)[1]
    await state.update_data(order=order_data)
    await callback.answer()
    await _continue_flow(callback.message, state, db)


# ---------------------------------------------------------------------------
# Кнопки карточки-превью (работают по draft_id из callback_data)
# ---------------------------------------------------------------------------


async def _resolve_unknown_draft(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient,
    draft: dict[str, Any],
) -> None:
    """«Создать» по черновику в creation_unknown: только сверка, никогда add.

    Сделка могла быть создана при таймауте, поэтому новый deal.add по такой
    карточке не выполняется никогда. Нашлась в CRM — черновик закрывается
    как успех (tombstone done + номер в processed одной транзакцией); не
    нашлась — честно говорим, что проверка продолжается.
    """
    await callback.answer()
    draft_id = draft["draft_id"]
    # Ключ, с которым мог пройти deal.add: mark_draft_unknown сохранил его
    # в dedup_key, поэтому сверка идёт ровно по нему.
    key = draft["dedup_key"]
    try:
        deal_id = await find_deal_by_key(bitrix, key)
    except Exception:
        log.exception("Сверка черновика %s по ключу не удалась", draft_id)
        deal_id = None
    if deal_id is None:
        await callback.message.answer(CRM_STILL_UNKNOWN_TEXT)
        return
    log.info("Сделка id=%s нашлась при повторной сверке черновика %s", deal_id, draft_id)
    if not await db.complete_draft(draft_id, key, deal_id):
        # Черновик уже закрыт параллельной сверкой: подтверждение отправит
        # тот путь, который успел зафиксировать сделку первым.
        log.info("Черновик %s уже закрыт другим путём", draft_id)
        return
    data = await state.get_data()
    if data.get("draft_id") == draft_id:
        await state.clear()
    order = ParsedOrder.model_validate_json(draft["parsed_json"])
    name = order.client_name or "Клиент"
    await callback.message.answer(f"Заявка №{deal_id} создана, клиент {name}.")
    await _schedule_deal_reminder(callback.message, db, bitrix, order, deal_id)


@router.callback_query(F.data.startswith("order:create"))
async def on_create(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bitrix: BitrixClient | None = None,
) -> None:
    draft = await _load_draft(callback, db)
    if draft is None:
        return

    if draft["status"] == DRAFT_DONE:
        # Терминальный tombstone: сделка уже создана, повторное нажатие
        # просто напоминает её номер, в CRM ничего не пишется.
        await callback.answer()
        await callback.message.answer(DEAL_ALREADY_CREATED.format(deal_id=draft["deal_id"]))
        return

    if bitrix is None:
        await callback.answer()
        await callback.message.answer(
            "CRM пока не подключена (не задан вебхук Bitrix24), заявку не записал."
        )
        return

    if draft["status"] == DRAFT_UNKNOWN:
        # Неоднозначный таймаут в прошлом: только сверка, второй add запрещён.
        await _resolve_unknown_draft(callback, state, db, bitrix, draft)
        return

    # Атомарный захват: параллельное нажатие "Создать" (даблклик, повторная
    # доставка апдейта) захват не получит и молча выйдет — сделка одна.
    draft = await db.claim_draft(draft["draft_id"])
    if draft is None:
        await callback.answer(CLAIM_IN_PROGRESS, show_alert=False)
        return

    draft_id = draft["draft_id"]
    token = draft["claim_token"]

    # Полностью атомарной защиты от двойной сделки без серверной уникальности
    # (UNIQUE-ограничения на стороне Bitrix) не построить: между проверкой
    # аренды и crm.deal.add всегда остаётся зазор. При одном инстансе бота
    # (Railway) владелец-токен + heartbeat закрывают реалистичную гонку
    # зависшего воркера, предпроверка по UF_CRM_TG_MSG_ID (find_deal_by_key)
    # и сверка после сбоя (_reconcile_deal) — бэкстоп от дублей. Сделка,
    # принятая сервером, но не видимая в crm.deal.list дольше окна сверки,
    # замораживает черновик в creation_unknown: повторные нажатия сверяются
    # с CRM, но второй deal.add не выполняют.
    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(db, draft_id, token, stop, lease_lost))
    deal_id: int | None = None  # заполняется только при созданной сделке
    # False = черновик больше не наш: аренду перехватил другой воркер, либо
    # терминальный переход (заморозка/фиксация) уже сделан или начат.
    release_lease = True
    # True — complete_draft закоммитил терминальный done: страховка в finally
    # ничего не замораживает, черновик уже в конечном состоянии.
    draft_done = False
    key = draft["dedup_key"] or f"cb:{callback.id}"
    fence_owned = False
    # Клиент уже был в CRM до этой заявки: сделка помечается повторной
    # (IS_RETURN_CUSTOMER) для аналитики Bitrix24. REST сам флаг не ставит —
    # подтверждено живым порталом.
    returning_client = False
    # Каждая неоднозначная contact/comment-запись имеет собственную фазу.
    # Флаг снимается только после постоянного settle этой же операции.
    contact_unknown = False
    # Выставляется непосредственно перед отправкой crm.deal.add. Фазы важны:
    # сбой ДО отправки (контакт, предпроверка дубля) однозначен — сделки нет,
    # retry безопасен; сбой ПОСЛЕ отправки (таймаут, обрыв, отмена задачи)
    # неоднозначен — сделка могла записаться, черновик обязан замёрзнуть.
    add_sent = False
    try:
        # Подтверждение нажатия — уже внутри try: если callback.answer()
        # упадёт (сетевой сбой), finally снимет аренду, а не оставит черновик
        # захваченным до конца таймаута. Запись в CRM может занять десятки
        # секунд, поэтому подтверждаем до неё, итог уходит сообщением.
        await callback.answer()
        order = ParsedOrder.model_validate_json(draft["parsed_json"])
        name = order.client_name or "Клиент"
        try:
            # Общий дедлайн на контакт + сделку: CRM_DEADLINE < CLAIM_TIMEOUT,
            # зависший запрос не переживает собственную аренду. Замок чата —
            # ВНУТРИ дедлайна: ожидание чужой записи тоже ограничено, а два
            # конкурентных «Создать» по разным карточкам одного сообщения
            # выполняют предпроверку дубля строго по очереди.
            async with asyncio.timeout(CRM_DEADLINE), _chat_lock(draft["chat_id"]):
                fence = await db.claim_deal_fence(key, draft_id)
                fence_owned = fence["owned"]
                if not fence["owned"]:
                    # Другой черновик уже занял тот же общий ключ. Ни контакт,
                    # ни сделку здесь не создаём; fence не истекает и переживает
                    # рестарт, поэтому запаздывающая видимость CRM безопасна.
                    if fence["status"] == "done" and fence["deal_id"] is not None:
                        deal_id = int(fence["deal_id"])
                    else:
                        await callback.message.answer(CRM_UNKNOWN_TEXT)
                        return
                elif fence["status"] == "done" and fence["deal_id"] is not None:
                    deal_id = int(fence["deal_id"])
                elif fence["status"] == "sent":
                    # Предыдущая попытка дошла до границы deal.add. Новый add
                    # запрещён навсегда; разрешена только сверка по UF-ключу.
                    add_sent = True
                    release_lease = False
                    if not await _freeze_draft_unknown(db, draft_id, token, key):
                        return
                    deal_id = await _reconcile_deal(bitrix, key)
                    if deal_id is None:
                        await callback.message.answer(CRM_UNKNOWN_TEXT)
                        return
                elif fence["status"] == "contact_sent":
                    # Прошлый contact.add мог пройти без ответа. Повтор add
                    # запрещён; ниже разрешена только сверка по UF-ключу.
                    contact_unknown = True
                elif fence["status"] == "comment_sent":
                    # UF-ключ доказывает контакт, но не доставку комментария.
                    # Автоматически повторить или сверить timeline нельзя.
                    contact_unknown = True

                # Проверка сделки идёт ДО контакта: повтор другого черновика
                # не оставит контакт-сироту, если сделка с ключом уже есть.
                if lease_lost.is_set() or not await db.refresh_claim(draft_id, token):
                    log.warning("Аренда черновика %s потеряна, сделку не создаю", draft_id)
                    release_lease = False
                    return
                if deal_id is None:
                    deal_id = await find_deal_by_key(bitrix, key)
                if deal_id is not None:
                    log.info("Сделка с ключом уже есть: id=%s, дубль не создаю", deal_id)
                else:
                    phase = fence["status"]
                    if phase == "comment_sent":
                        release_lease = False
                        if not await _freeze_draft_unknown(db, draft_id, token, key):
                            return
                        await callback.message.answer(CRM_UNKNOWN_TEXT)
                        return
                    contact_id = draft["contact_id"]
                    if contact_id is None:
                        if phase in ("contact_sent", "contact_ready", "comment_done"):
                            contact_id = await find_contact_by_draft_id(bitrix, draft_id)
                            if contact_id is None:
                                if phase == "contact_sent":
                                    release_lease = False
                                    if not await _freeze_draft_unknown(
                                        db, draft_id, token, key
                                    ):
                                        return
                                    await callback.message.answer(CRM_UNKNOWN_TEXT)
                                else:
                                    await callback.message.answer(CRM_RETRY_TEXT)
                                return
                            if phase == "contact_sent":
                                if not await db.settle_deal_fence_contact(
                                    key, draft_id, token, contact_id, True
                                ):
                                    release_lease = False
                                    if not await _freeze_draft_unknown(
                                        db, draft_id, token, key
                                    ):
                                        return
                                    await callback.message.answer(CRM_UNKNOWN_TEXT)
                                    return
                                contact_unknown = False
                                phase = "comment_done"
                            elif not await db.set_draft_contact(
                                draft_id, contact_id, token
                            ):
                                release_lease = False
                                return
                        else:

                            async def mark_contact_boundary() -> None:
                                nonlocal contact_unknown
                                if not await db.mark_deal_fence_contact_sent(
                                    key, draft_id
                                ):
                                    raise RuntimeError("Fence контакта потерян")
                                contact_unknown = True

                            try:
                                contact_id, created = await resolve_contact(
                                    bitrix,
                                    name=name,
                                    phone=order.phone,
                                    org=order.org,
                                    comment=order.problem,
                                    draft_id=draft_id,
                                    before_contact_add=mark_contact_boundary,
                                )
                            except Exception as exc:
                                if contact_unknown and is_server_refusal(exc):
                                    reset, cancelled = await _complete_despite_cancellation(
                                        db.reset_deal_fence(
                                            key, draft_id, "contact_sent"
                                        )
                                    )
                                    if reset:
                                        contact_unknown = False
                                    if cancelled:
                                        raise asyncio.CancelledError
                                    if not reset:
                                        raise RuntimeError(
                                            "Fence контакта не сброшен после отказа"
                                        ) from exc
                                raise
                            settled, settle_cancelled = await _complete_despite_cancellation(
                                db.settle_deal_fence_contact(
                                    key, draft_id, token, contact_id, created
                                )
                            )
                            if not settled:
                                if created:
                                    contact_unknown = True
                                else:
                                    contact_unknown = False
                                release_lease = False
                                if contact_unknown:
                                    if not await _freeze_draft_unknown(
                                        db, draft_id, token, key
                                    ):
                                        return
                                    await callback.message.answer(CRM_UNKNOWN_TEXT)
                                return
                            contact_unknown = False
                            returning_client = not created
                            phase = "comment_done" if created else "contact_ready"
                            if settle_cancelled:
                                raise asyncio.CancelledError
                    if phase == "contact_ready":
                        if order.problem:
                            if not await db.mark_deal_fence_comment_sent(key, draft_id):
                                contact_unknown = True
                                release_lease = False
                                if not await _freeze_draft_unknown(
                                    db, draft_id, token, key
                                ):
                                    return
                                await callback.message.answer(CRM_UNKNOWN_TEXT)
                                return
                            contact_unknown = True
                            try:
                                await add_contact_timeline_comment(
                                    bitrix, contact_id, order.problem
                                )
                            except Exception as exc:
                                if is_server_refusal(exc):
                                    reset, cancelled = await _complete_despite_cancellation(
                                        db.reset_deal_fence(
                                            key, draft_id, "comment_sent"
                                        )
                                    )
                                    if reset:
                                        contact_unknown = False
                                    if cancelled:
                                        raise asyncio.CancelledError
                                    if not reset:
                                        raise RuntimeError(
                                            "Fence комментария не сброшен после отказа"
                                        ) from exc
                                raise
                            settled, settle_cancelled = await _complete_despite_cancellation(
                                db.settle_deal_fence_comment(key, draft_id)
                            )
                            if not settled:
                                release_lease = False
                                if not await _freeze_draft_unknown(
                                    db, draft_id, token, key
                                ):
                                    return
                                await callback.message.answer(CRM_UNKNOWN_TEXT)
                                return
                            contact_unknown = False
                            phase = "comment_done"
                            if settle_cancelled:
                                raise asyncio.CancelledError
                        elif not await db.skip_deal_fence_comment(key, draft_id):
                            release_lease = False
                            if not await _freeze_draft_unknown(
                                db, draft_id, token, key
                            ):
                                return
                            await callback.message.answer(CRM_UNKNOWN_TEXT)
                            return
                        contact_unknown = False
                        phase = "comment_done"
                    if lease_lost.is_set() or not await db.refresh_claim(draft_id, token):
                        log.warning("Аренда черновика %s потеряна, сделку не создаю", draft_id)
                        release_lease = False
                        return
                    # Fence переводится в sent ДО HTTP-запроса. Даже отмена
                    # между коммитом и отправкой приведёт лишь к консервативной
                    # сверке, но никогда ко второму deal.add.
                    add_sent = True
                    if not await db.mark_deal_fence_sent(key, draft_id):
                        release_lease = False
                        if not await _freeze_draft_unknown(db, draft_id, token, key):
                            return
                        deal_id = await _reconcile_deal(bitrix, key)
                        if deal_id is None:
                            await callback.message.answer(CRM_UNKNOWN_TEXT)
                            return
                    else:
                        deal_fields = build_deal_fields(order)
                        if returning_client:
                            deal_fields["IS_RETURN_CUSTOMER"] = "Y"
                        try:
                            deal_id = await create_deal(
                                bitrix, contact_id, deal_fields, key
                            )
                        except Exception as exc:
                            if is_server_refusal(exc):
                                reset, cancelled = await _complete_despite_cancellation(
                                    db.reset_deal_fence(key, draft_id, "sent")
                                )
                                if reset:
                                    add_sent = False
                                if cancelled:
                                    raise asyncio.CancelledError
                                if not reset:
                                    raise RuntimeError(
                                        "Fence сделки не сброшен после отказа"
                                    ) from exc
                            raise
        except Exception as exc:
            # CancelledError сюда не попадает (не наследует Exception):
            # отмена задачи после отправки add обрабатывается страховкой
            # в finally — заморозка под shield, затем отмена идёт дальше.
            unsafe_unknown = add_sent or contact_unknown
            if not unsafe_unknown:
                # Сбой строго до отправки deal.add (контакт, предпроверка,
                # дедлайн CRM_DEADLINE на этих шагах) либо явный отказ
                # сервера: сделки в CRM точно нет. finally снимет захват,
                # retry той же кнопкой безопасен.
                log.exception("Не удалось записать заявку в Bitrix24")
                await callback.message.answer(CRM_RETRY_TEXT)
                return
            # deal.add отправлен, ответа нет (таймаут, обрыв соединения):
            # сделка могла записаться. Черновик сразу замораживается — с этого
            # момента ни отмена, ни сбой сверки не отдадут его под новый add.
            # Осознанный размен: если deal.add на самом деле не прошёл,
            # карточка «залипнет» в creation_unknown (заявку придётся
            # отправить заново новым сообщением), зато повторное нажатие
            # никогда не создаст дубль сделки, невидимой в crm.deal.list.
            log.warning(
                "Исход unsafe-записи по черновику %s неизвестен (%s)",
                draft_id,
                type(exc).__name__,
            )
            release_lease = False
            if not await _freeze_draft_unknown(db, draft_id, token, key):
                return
            if add_sent:
                deal_id = await _reconcile_deal(bitrix, key)
                if deal_id is not None:
                    log.info(
                        "Сделка id=%s нашлась при сверке после сбоя deal.add",
                        deal_id,
                    )
            if deal_id is None:
                await callback.message.answer(CRM_UNKNOWN_TEXT)
                return

        # Терминальный факт одной транзакцией: номер сделки в processed
        # (дедуп сообщений) и tombstone-черновик done. Подтверждение шлётся
        # только после коммита: если отправка упадёт, повторное нажатие
        # ответит «Заявка №N уже создана», а не создаст вторую сделку.
        # shield: отмена задачи не обрывает фиксацию — транзакция дойдёт до
        # коммита в фоне, а страховка в finally заморозит черновик, если
        # фиксация до неё не успела (гонку решает БД, исход терминален).
        completed, completion_cancelled = await _complete_despite_cancellation(
            db.complete_draft(draft_id, key, deal_id, token)
        )
        if not completed:
            # Аренда потеряна между записью в CRM и фиксацией: черновиком
            # владеет другой воркер, подтверждать сделку и морозить черновик
            # не нам — тихий выход, как при любой потере аренды.
            release_lease = False
            log.warning("Черновик %s не зафиксирован: аренда потеряна", draft_id)
            return
        draft_done = True
        if completion_cancelled:
            raise asyncio.CancelledError
        data = await state.get_data()
        if data.get("draft_id") == draft_id:
            await state.clear()
        await callback.message.answer(f"Заявка №{deal_id} создана, клиент {name}.")
        # Напоминания о сроке — после фиксации и подтверждения: их сбой уже
        # не может повлиять на саму заявку.
        await _schedule_deal_reminder(callback.message, db, bitrix, order, deal_id)
    finally:
        stop.set()
        # Страховка инварианта: после отправленного deal.add черновик не
        # имеет права остаться open. Любой выход без зафиксированного done —
        # отмена во время create_deal или complete_draft, неожиданное
        # исключение — замораживает его в creation_unknown под shield.
        # release_lease=False означает, что терминальный переход уже сделан
        # выше (или аренда не наша) — второй заморозки не будет; уже
        # закоммиченный done заморозка не понижает (guard в mark_draft_unknown).
        unsafe_unknown = add_sent or contact_unknown
        if unsafe_unknown and not draft_done and release_lease:
            release_lease = False
            await _freeze_draft_unknown(db, draft_id, token, key)
        try:
            # Отдельное ожидание heartbeat: его исключение не должно
            # пропустить снятие аренды ниже и не должно заслонить исходную
            # ошибку обработчика. Ожидание ограничено по времени: после
            # stop.set() heartbeat завершается сразу.
            await asyncio.wait_for(heartbeat, timeout=HEARTBEAT_INTERVAL)
        except Exception:
            log.exception("Heartbeat черновика %s завершился с ошибкой", draft_id)
        finally:
            if deal_id is None and release_lease:
                # Любой выход без созданной сделки и без заморозки (ошибка
                # CRM, упавший callback.answer, отмена задачи) снимает нашу
                # аренду: черновик не должен залипать до конца таймаута.
                # shield: повторная отмена задачи не оборвёт снятие на полпути.
                async def release_safe_state() -> None:
                    if fence_owned:
                        await db.release_deal_fence(key, draft_id)
                    await db.release_draft(draft_id, token)

                await _await_cancellation_safe(release_safe_state())


@router.callback_query(F.data.startswith("order:edit"))
async def on_edit(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    draft = await _load_draft(callback, db)
    if draft is None:
        return
    if draft["status"] == DRAFT_DONE:
        # Сделка уже создана: менять черновик поздно, напоминаем номер.
        await callback.answer(
            DEAL_ALREADY_CREATED.format(deal_id=draft["deal_id"]), show_alert=True
        )
        return
    if draft["status"] == DRAFT_UNKNOWN:
        # Судьба записи ещё выясняется: правки разошлись бы с возможной сделкой.
        await callback.answer(CRM_STILL_UNKNOWN_TEXT, show_alert=True)
        return
    # Сначала резервируем черновик тем же CAS-захватом, что и «Создать».
    # Это не удаляет данные до успешной доставки первого вопроса.
    draft = await db.claim_draft(draft["draft_id"])
    if draft is None:
        # Черновик успели захватить «Создать» или изъять параллельной правкой.
        await callback.answer(DRAFT_BUSY, show_alert=True)
        return
    order_data = ParsedOrder.model_validate_json(draft["parsed_json"]).model_dump(mode="json")
    current = order_data.get("client_name") or "не указано"
    token = draft["claim_token"]
    fsm_snapshot = (
        await state.get_state(),
        deepcopy(await state.get_data()),
    )
    prompt_sent = False
    edit_committed = False
    rollback_notified = False
    try:
        await callback.answer()
        # Telegram мог принять сообщение, но не вернуть HTTP-ответ: с этой
        # точки вопрос считается возможно доставленным и требует компенсации.
        prompt_sent = True
        await callback.message.answer(
            f"Вопрос 1 из 6. Имя клиента (сейчас: {current}). "
            "Пришлите новое или «-», чтобы оставить.",
            reply_markup=cancel_keyboard(),
        )

        async def commit_edit() -> bool:
            """Меняет FSM и удаляет карточку с компенсацией при любом сбое."""
            await state.set_data(
                {
                    "order": order_data,
                    "dedup_key": draft["dedup_key"],
                    "user_id": draft["user_id"],
                }
            )
            await state.set_state(OrderFlow.form_name)
            try:
                deleted = await db.delete_draft(draft["draft_id"], token)
            except BaseException:
                try:
                    await _restore_fsm(state, fsm_snapshot)
                finally:
                    await db.release_draft(draft["draft_id"], token)
                raise
            if not deleted:
                try:
                    await _restore_fsm(state, fsm_snapshot)
                finally:
                    await db.release_draft(draft["draft_id"], token)
            return deleted

        deleted, cancelled = await _complete_despite_cancellation(commit_edit())
        if not deleted:
            _, notify_cancelled = await _complete_despite_cancellation(
                _send_edit_rollback(callback.message)
            )
            rollback_notified = True
            if cancelled or notify_cancelled:
                raise asyncio.CancelledError
            return
        edit_committed = True
        if cancelled:
            raise asyncio.CancelledError
    except BaseException:
        # До начала commit_edit старая карточка ещё существует и захвачена.
        # После его сбоя компенсация уже восстановила FSM, повторный release
        # безопасен благодаря CAS по token.
        _, cancelled = await _complete_despite_cancellation(
            db.release_draft(draft["draft_id"], token)
        )
        notify_cancelled = False
        if prompt_sent and not edit_committed and not rollback_notified:
            _, notify_cancelled = await _complete_despite_cancellation(
                _send_edit_rollback(callback.message)
            )
        if cancelled or notify_cancelled:
            raise asyncio.CancelledError
        raise


@router.callback_query(F.data.startswith("order:cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    draft = await _load_draft(callback, db)
    if draft is None:
        return
    if draft["status"] == DRAFT_DONE:
        # Отменять нечего: сделка уже создана, напоминаем номер.
        await callback.answer(
            DEAL_ALREADY_CREATED.format(deal_id=draft["deal_id"]), show_alert=True
        )
        return
    if draft["status"] == DRAFT_UNKNOWN:
        # Сделка могла записаться при таймауте: «Отменено» соврало бы.
        await callback.answer(CRM_STILL_UNKNOWN_TEXT, show_alert=True)
        return
    # Атомарное удаление «только если open и не захвачен»: если «Создать»
    # уже пишет этот черновик в CRM, отмена отклоняется — иначе бот ответил
    # бы «Отменено», а сделка всё равно создалась бы.
    if not await db.delete_draft(draft["draft_id"]):
        await callback.answer(DRAFT_BUSY, show_alert=True)
        return
    data = await state.get_data()
    if data.get("draft_id") == draft["draft_id"]:
        await state.clear()
    await callback.answer()
    await callback.message.answer("Отменено.")


# ---------------------------------------------------------------------------
# Пошаговый опросник: fallback без модели и режим "Изменить"
#
# Логика каждого шага вынесена в *_step-функции с явным текстом ответа:
# хендлеры текста передают message.text, а голосовой хендлер (handlers/voice)
# — распознанную речь, поэтому голос в опроснике отвечает на текущий вопрос,
# а не начинает новую заявку.
# ---------------------------------------------------------------------------


async def _set_field(state: FSMContext, field: str, raw: str) -> None:
    """Пишет значение в черновик; «-» и пустой ответ оставляют как было."""
    value = (raw or "").strip()
    data = await state.get_data()
    order_data = data["order"]
    if value and value != KEEP_WORD:
        order_data[field] = value
    await state.update_data(order=order_data)


ASK_CATEGORY_TEXT = "Вопрос 3 из 6. Категория услуги:"

ASK_SOURCE_TEXT = (
    "Вопрос 4 из 6. Источник заявки? Выберите кнопкой или пришлите «-», "
    "чтобы пропустить (запишется «Прочее»)."
)

ASK_PROBLEM_TEXT = "Вопрос 5 из 6. Опишите, что нужно сделать."

# Во всех шагах опросника следующий вопрос отправляется ДО перевода FSM:
# если отправка упала, состояние осталось на текущем шаге, и повторённый
# ответ запишется в то же поле, а не молча уедет в следующее (например,
# описание работ — в срок выполнения).


async def form_name_step(message: Message, state: FSMContext, text: str) -> None:
    await _set_field(state, "client_name", text)
    data = await state.get_data()
    if data.get("phone_skipped") and not data["order"].get("phone"):
        # Телефон уже спрашивали до входа в опросник (переразбор из
        # ask_phone упал в LLMUnavailable): вопрос о номере не повторяется —
        # телефон запрашивается максимум один раз за заявку.
        await message.answer(
            ASK_CATEGORY_TEXT, reply_markup=with_cancel(category_keyboard())
        )
        await state.set_state(OrderFlow.form_category)
        return
    await message.answer(
        "Вопрос 2 из 6. Телефон клиента? Пришлите номер, «нет» если "
        "телефона нет, или «-», чтобы оставить как есть.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(OrderFlow.form_phone)


@router.message(OrderFlow.form_name, F.text)
async def on_form_name(message: Message, state: FSMContext) -> None:
    await form_name_step(message, state, message.text or "")


async def form_phone_step(message: Message, state: FSMContext, text: str) -> None:
    text = (text or "").strip()
    data = await state.get_data()
    order_data = data["order"]
    if text.lower() in SKIP_WORDS:
        order_data["phone"] = None
        await state.update_data(order=order_data, phone_skipped=True)
    elif text != KEEP_WORD:
        phone = extract_bare_phone(text)
        if phone is None:
            await message.answer(
                NOT_A_PHONE + " «-» оставит номер как есть.",
                reply_markup=cancel_keyboard(),
            )
            return
        order_data["phone"] = phone
        await state.update_data(order=order_data)
    elif not order_data.get("phone"):
        # оставить нечего: телефона в черновике нет
        await state.update_data(phone_skipped=True)
    await message.answer(ASK_CATEGORY_TEXT, reply_markup=with_cancel(category_keyboard()))
    await state.set_state(OrderFlow.form_category)


@router.message(OrderFlow.form_phone, F.text)
async def on_form_phone(message: Message, state: FSMContext) -> None:
    await form_phone_step(message, state, message.text or "")


@router.callback_query(OrderFlow.form_category, F.data.startswith("cat:"))
async def on_form_category(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    order_data = data["order"]
    order_data["category"] = callback.data.split(":", 1)[1]
    await state.update_data(order=order_data)
    await callback.answer()
    await callback.message.answer(
        ASK_SOURCE_TEXT, reply_markup=with_cancel(source_keyboard())
    )
    await state.set_state(OrderFlow.form_source)


async def form_category_text_step(message: Message, state: FSMContext, text: str) -> None:
    """Категорию можно и напечатать (или наговорить), не только выбрать кнопкой."""
    text = (text or "").strip().lower()
    data = await state.get_data()
    order_data = data["order"]
    matched = next((cat.value for cat in Category if cat.value == text), None)
    if matched is None and not (text == KEEP_WORD and order_data.get("category")):
        await message.answer(
            "Выберите категорию кнопкой ниже:",
            reply_markup=with_cancel(category_keyboard()),
        )
        return
    if matched is not None:
        order_data["category"] = matched
        await state.update_data(order=order_data)
    await message.answer(ASK_SOURCE_TEXT, reply_markup=with_cancel(source_keyboard()))
    await state.set_state(OrderFlow.form_source)


@router.message(OrderFlow.form_category, F.text)
async def on_form_category_text(message: Message, state: FSMContext) -> None:
    await form_category_text_step(message, state, message.text or "")


@router.callback_query(OrderFlow.form_source, F.data.startswith("src:"))
async def on_form_source(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    order_data = data["order"]
    order_data["source"] = callback.data.split(":", 1)[1]
    await state.update_data(order=order_data)
    await callback.answer()
    await callback.message.answer(ASK_PROBLEM_TEXT, reply_markup=cancel_keyboard())
    await state.set_state(OrderFlow.form_problem)


async def form_source_text_step(message: Message, state: FSMContext, text: str) -> None:
    """Источник можно напечатать или наговорить; «-» пропускает вопрос.

    Пропущенный источник остаётся пустым — при записи сделки подставится
    «Прочее» (см. build_deal_fields), переспрашивать не нужно.
    """
    cleaned = (text or "").strip().lower()
    matched = next((src.value for src in Source if src.value.lower() == cleaned), None)
    if matched is None and cleaned != KEEP_WORD:
        await message.answer(
            "Выберите источник кнопкой ниже:",
            reply_markup=with_cancel(source_keyboard()),
        )
        return
    if matched is not None:
        data = await state.get_data()
        order_data = data["order"]
        order_data["source"] = matched
        await state.update_data(order=order_data)
    await message.answer(ASK_PROBLEM_TEXT, reply_markup=cancel_keyboard())
    await state.set_state(OrderFlow.form_problem)


@router.message(OrderFlow.form_source, F.text)
async def on_form_source_text(message: Message, state: FSMContext) -> None:
    await form_source_text_step(message, state, message.text or "")


async def form_problem_step(message: Message, state: FSMContext, text: str) -> None:
    await _set_field(state, "problem", text)
    data = await state.get_data()
    if not data["order"].get("problem"):
        await message.answer(
            "Без описания заявку не завести. Опишите, что нужно сделать.",
            reply_markup=cancel_keyboard(),
        )
        return
    await message.answer(
        "Вопрос 6 из 6. Срок выполнения? Например «завтра до 18:00», "
        "«нет» если срок не важен, «-» чтобы оставить как есть.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(OrderFlow.form_deadline)


@router.message(OrderFlow.form_problem, F.text)
async def on_form_problem(message: Message, state: FSMContext) -> None:
    await form_problem_step(message, state, message.text or "")


async def form_deadline_step(
    message: Message, state: FSMContext, db: Database, text: str
) -> None:
    text = (text or "").strip()
    data = await state.get_data()
    order_data = data["order"]
    if text.lower() in SKIP_WORDS:
        order_data["deadline"] = None
        await state.update_data(order=order_data)
    elif text != KEEP_WORD:
        # Ответ опросника разбирается так же, как срок из свободного текста:
        # «завтра до 18:00» становится датой, непонятное сохраняется как есть.
        order_data["deadline"] = (
            dates.parse_human_date(text, dates.now_local()) or text
        )
        await state.update_data(order=order_data)
    await _continue_flow(message, state, db)


@router.message(OrderFlow.form_deadline, F.text)
async def on_form_deadline(message: Message, state: FSMContext, db: Database) -> None:
    await form_deadline_step(message, state, db, message.text or "")


async def ask_category_text_step(
    message: Message, state: FSMContext, db: Database, text: str
) -> None:
    """Голосовой ответ на уточняющий вопрос о категории.

    Кнопки голосом не нажать, поэтому распознанный текст сверяется со
    списком категорий: совпадение продолжает поток, иначе клавиатура
    показывается снова.
    """
    matched = next(
        (cat.value for cat in Category if cat.value == (text or "").strip().lower()), None
    )
    if matched is None:
        await message.answer(
            "Выберите категорию кнопкой ниже:",
            reply_markup=with_cancel(category_keyboard()),
        )
        return
    data = await state.get_data()
    order_data = data["order"]
    order_data["category"] = matched
    await state.update_data(order=order_data)
    await _continue_flow(message, state, db)
