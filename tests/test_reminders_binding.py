"""Привязка отдельных напоминаний к заявкам.

Заказчик просил: при постановке напоминания бот спрашивает, к какой заявке
оно относится (номер, организация, телефон, «последняя») или это обычное
напоминание без привязки; привязку можно назвать и сразу в тексте
(«к последней заявке завтра в 8 позвонить»). Привязанная задача Bitrix24
связывается со сделкой (UF_CRM_TASK), а Telegram-пинг называет заявку.

Тесты гоняют полный диспетчер поверх RecordingSession и фейк-портала с
поисковой семантикой (FakeSearchBitrix) плюс задачи (tasks.task.*).
"""

import time
from types import SimpleNamespace

import pytest

from app.db import Database
from app.handlers import routers
from app.handlers.reminders import (
    BIND_BTN_LAST,
    BIND_BTN_NONE,
    BIND_FAILED,
    BIND_MANY,
    BIND_NOT_FOUND,
    BIND_PROMPT,
    REMIND_SCHEDULED_DEAL,
    ReminderFlow,
)
from app.handlers.search import ASK_QUERY, SearchFlow
from app.handlers.start import BTN_FIND, BTN_MY_REMINDERS, BTN_REMIND
from app.main import create_dispatcher
from app.schemas import Intent, ParsedOrder
from app.services import llm, speech
from tests.conftest import (
    make_callback_update,
    make_message_update,
    make_voice_update,
)
from tests.test_search import FakeSearchBitrix


class FakeBindingBitrix(FakeSearchBitrix):
    """Поисковый портал из test_search плюс задачи-напоминания (tasks.task.*)."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[dict] = []
        self.completed: list[int] = []

    async def _dispatch(self, method: str, params: dict):
        if method == "tasks.task.add":
            self.tasks.append(params["fields"])
            return {"task": {"id": 77 + len(self.tasks) - 1}}
        if method == "tasks.task.list":
            tag = (params.get("filter") or {}).get("TAG")
            rows = [
                {"id": str(77 + index)}
                for index, fields in enumerate(self.tasks)
                if tag is None or tag in (fields.get("TAGS") or [])
            ]
            return {"tasks": rows}
        if method == "tasks.task.complete":
            self.completed.append(int(params["taskId"]))
            return {"task": True}
        if method == "tasks.task.get":
            index = int(params["taskId"]) - 77
            if not 0 <= index < len(self.tasks):
                return []
            fields = self.tasks[index]
            return {
                "task": {
                    "id": str(77 + index),
                    "deadline": fields.get("DEADLINE"),
                    "status": "2",
                    "ufCrmTask": fields.get("UF_CRM_TASK") or [],
                }
            }
        return await super()._dispatch(method, params)


@pytest.fixture(autouse=True)
def _detach_routers():
    yield
    for r in routers:
        r._parent_router = None


@pytest.fixture
async def flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "bind.db"))
    await db.init()
    bx = FakeBindingBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


async def send(flow, text: str, user_id: int = 1, **extra) -> None:
    await flow.dp.feed_update(
        flow.bot, make_message_update(flow.bot, text, user_id=user_id, **extra)
    )


async def press(flow, data: str, user_id: int = 1) -> None:
    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, data, user_id=user_id)
    )


async def state_of(flow, user_id: int = 1):
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=user_id, user_id=user_id)
    return await context.get_state()


def reminder_order(problem: str, deadline: str | None = None) -> ParsedOrder:
    return ParsedOrder(problem=problem, deadline=deadline, intent=Intent.reminder)


def mock_parse(monkeypatch, mapping: dict[str, ParsedOrder]):
    async def fake(text: str):
        return mapping.get(text.lower())

    monkeypatch.setattr(llm, "parse_order", fake)


def buttons_of(message) -> list[tuple[str, str]]:
    if message.reply_markup is None:
        return []
    return [
        (b.text, b.callback_data)
        for row in message.reply_markup.inline_keyboard
        for b in row
    ]


REMIND_TEXT = "через 2 часа позвонить заказчику"


async def start_reminder(flow, monkeypatch, text: str = REMIND_TEXT) -> None:
    """Кнопка «Напоминание» + текст с датой: доводит до вопроса о привязке."""
    mock_parse(monkeypatch, {REMIND_TEXT: reminder_order("позвонить заказчику")})
    await send(flow, BTN_REMIND)
    await send(flow, text)


def bind_data(flow, suffix: str) -> str:
    """callback_data кнопки привязки с последнего вопроса (несёт nonce)."""
    for message in reversed(flow.session.sent_messages):
        for text, data in buttons_of(message):
            if data.startswith("rem:bind:") and data.endswith(":" + suffix):
                return data
    raise AssertionError(f"кнопки rem:bind:*:{suffix} нет в отправленных")


async def press_bind(flow, suffix: str, user_id: int = 1) -> None:
    await press(flow, bind_data(flow, suffix), user_id=user_id)


# --- Вопрос о привязке ---------------------------------------------------


async def test_reminder_asks_binding_question(flow, monkeypatch):
    """После текста с датой бот спрашивает про заявку, задача НЕ создана."""
    await start_reminder(flow, monkeypatch)

    msg = flow.session.sent_messages[-1]
    assert msg.text == BIND_PROMPT
    labels = [text for text, _ in buttons_of(msg)]
    assert BIND_BTN_LAST in labels
    assert BIND_BTN_NONE in labels
    assert bind_data(flow, "last")
    assert bind_data(flow, "none")
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_bind_none_creates_unbound(flow, monkeypatch):
    """«Без привязки» ставит обычное напоминание, как раньше."""
    await start_reminder(flow, monkeypatch)
    before = int(time.time())
    await press_bind(flow, "none")

    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert abs(rows[0]["due_ts"] - (before + 2 * 3600)) <= 120
    replies = "\n".join(flow.session.sent_texts)
    assert "Пришлю" in replies
    assert await state_of(flow) is None


async def test_bind_last_binds_to_latest_deal(flow, monkeypatch):
    """«К последней заявке» привязывает к самой новой сделке (ID выше)."""
    await start_reminder(flow, monkeypatch)
    await press_bind(flow, "last")

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_155"]
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert "№155" in rows[0]["text"]
    replies = "\n".join(flow.session.sent_texts)
    assert "по заявке" in replies
    assert "№155" in replies
    assert await state_of(flow) is None


async def test_bind_by_deal_number_answer(flow, monkeypatch):
    """Ответ номером заявки привязывает к ней."""
    await start_reminder(flow, monkeypatch)
    await send(flow, "154")

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_154"]
    rows = await flow.db.pending_task_reminders()
    assert "№154" in rows[0]["text"]
    assert await state_of(flow) is None


async def test_bind_by_text_single_match(flow, monkeypatch):
    """Ответ названием находит одну сделку и привязывает без уточнения."""
    await start_reminder(flow, monkeypatch)
    await send(flow, "сантехника")

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_154"]
    assert await state_of(flow) is None


async def test_bind_by_phone_many_matches_offers_choice(flow, monkeypatch):
    """Телефон с несколькими сделками: список с кнопками выбора."""
    await start_reminder(flow, monkeypatch)
    await send(flow, "+79141234567")

    assert flow.bx.tasks == []
    msg = flow.session.sent_messages[-1]
    assert BIND_MANY in msg.text
    assert bind_data(flow, "155")
    assert bind_data(flow, "154")
    assert bind_data(flow, "none")
    assert await state_of(flow) == ReminderFlow.binding.state

    await press_bind(flow, "154")
    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_154"]
    assert await state_of(flow) is None


async def test_bind_not_found_reasks_then_none_works(flow, monkeypatch):
    """Не нашли заявку: переспрос, режим не закрыт, «Без привязки» работает."""
    await start_reminder(flow, monkeypatch)
    await send(flow, "999")

    assert flow.session.sent_texts[-1] == BIND_NOT_FOUND
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state

    await press_bind(flow, "none")
    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]


async def test_bind_search_failure_keeps_flow(flow, monkeypatch):
    """Сбой CRM на поиске: честный ответ, режим не закрыт, задача не создана."""
    await start_reminder(flow, monkeypatch)
    flow.bx.fail_methods.add("crm.deal.list")
    await send(flow, "сантехника")

    assert flow.session.sent_texts[-1] == BIND_FAILED
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


# --- Привязка прямо в тексте напоминания ---------------------------------


async def test_inline_last_binding_skips_question(flow, monkeypatch):
    """«К последней заявке …» привязывает сразу, без вопроса."""
    mock_parse(monkeypatch, {REMIND_TEXT: reminder_order("позвонить заказчику")})
    await send(flow, BTN_REMIND)
    await send(flow, "к последней заявке через 2 часа позвонить заказчику")

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_155"]
    # Текст привязки не утёк в заголовок задачи.
    assert "последн" not in flow.bx.tasks[0]["TITLE"].lower()
    assert await state_of(flow) is None


async def test_inline_deal_number_binding(flow, monkeypatch):
    """«К заявке 154 …» привязывает к номеру сразу."""
    mock_parse(monkeypatch, {REMIND_TEXT: reminder_order("позвонить заказчику")})
    await send(flow, BTN_REMIND)
    await send(flow, "к заявке 154 через 2 часа позвонить заказчику")

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_154"]
    assert await state_of(flow) is None


async def test_inline_plain_reminder_skips_question(flow, monkeypatch):
    """«Обычное напоминание …» сразу ставит без привязки и без вопроса."""
    mock_parse(monkeypatch, {REMIND_TEXT: reminder_order("позвонить заказчику")})
    await send(flow, BTN_REMIND)
    await send(flow, "обычное напоминание через 2 часа позвонить заказчику")

    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]
    assert await state_of(flow) is None


# --- Голос, меню, устаревшие кнопки --------------------------------------


async def test_voice_answer_binds_last(flow, monkeypatch):
    """Голосовой ответ на вопрос о привязке работает как текст."""

    async def fake_stt(data: bytes) -> str:
        return "к последней заявке"

    monkeypatch.setattr(speech, "recognize_ogg", fake_stt)
    await start_reminder(flow, monkeypatch)
    flow.session.stream_chunks = [b"OggS", b"data"]
    await flow.dp.feed_update(flow.bot, make_voice_update(flow.bot))

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_155"]
    assert await state_of(flow) is None


async def test_menu_button_escapes_binding_flow(flow, monkeypatch):
    """Кнопка «Найти» из вопроса о привязке уводит в поиск."""
    await start_reminder(flow, monkeypatch)
    await send(flow, BTN_FIND)

    assert flow.session.sent_texts[-1] == ASK_QUERY
    assert await state_of(flow) == SearchFlow.query.state
    assert flow.bx.tasks == []


async def test_cancel_command_leaves_binding_flow(flow, monkeypatch):
    await start_reminder(flow, monkeypatch)
    await send(flow, "/cancel")

    assert await state_of(flow) is None
    assert flow.bx.tasks == []


async def test_stale_bind_callback_is_ignored(flow):
    """Клик по кнопке привязки без ожидающего напоминания ничего не создаёт."""
    await press(flow, "rem:bind:154")

    assert flow.bx.tasks == []
    assert await flow.db.pending_task_reminders() == []


async def test_foreign_bind_click_is_ignored(flow, monkeypatch):
    """Чужое нажатие кнопки привязки не создаёт напоминание автору."""
    await start_reminder(flow, monkeypatch)
    await press_bind(flow, "last", user_id=2)

    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_my_reminders_shows_deal_binding(flow, monkeypatch):
    """Список «Мои напоминания» показывает, к какой заявке пинг."""
    await start_reminder(flow, monkeypatch)
    await press_bind(flow, "last")
    await send(flow, BTN_MY_REMINDERS)

    listing = flow.session.sent_messages[-1]
    assert "№155" in listing.text


async def test_scheduled_deal_confirmation_mentions_deal(flow, monkeypatch):
    """Подтверждение называет заявку и дату (формат REMIND_SCHEDULED_DEAL)."""
    await start_reminder(flow, monkeypatch)
    await press_bind(flow, "last")

    prefix = REMIND_SCHEDULED_DEAL.split("{when}")[0]
    assert any(t.startswith(prefix) for t in flow.session.sent_texts)


# --- Правки по ревью Sol R1 ----------------------------------------------


async def test_old_binding_buttons_do_not_bind_new_reminder(flow, monkeypatch):
    """Кнопка от СТАРОГО вопроса не привязывает НОВОЕ напоминание (nonce).

    Сценарий ревью: получить кнопки для напоминания A, начать напоминание B,
    нажать старую кнопку A — задача не должна создаться вовсе, тем более с
    привязкой из чужого вопроса.
    """
    await start_reminder(flow, monkeypatch)
    stale_button = bind_data(flow, "last")

    # Новое напоминание B: свой вопрос, свой nonce.
    await send(flow, BTN_REMIND)
    await send(flow, REMIND_TEXT)
    fresh_button = bind_data(flow, "none")
    assert fresh_button != stale_button

    await press(flow, stale_button)
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state

    await press(flow, fresh_button)
    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]


async def test_legacy_format_bind_callback_is_stale(flow, monkeypatch):
    """Колбэк без nonce (старый формат кнопки) не создаёт ничего."""
    await start_reminder(flow, monkeypatch)
    await press(flow, "rem:bind:none")
    await press(flow, "rem:bind:154")

    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_pending_removed_during_search_prevents_creation(
    tmp_path, bot, session, monkeypatch
):
    """Снятый во время CRM-поиска pending останавливает создание (Sol R1).

    Через диспетчер такую гонку закрывает PruningEventIsolation (апдейты
    одного пользователя сериализуются), поэтому контракт проверяется на
    самой функции: пока ответ «сантехника» ждёт портал, pending снимается
    (отмена с другого воркера, рестарт со сбросом FSM) — найденная после
    этого сделка НЕ должна превращаться в созданную задачу.
    """
    import asyncio

    from app.handlers.reminders import handle_binding_answer

    class GatedBitrix(FakeBindingBitrix):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()

        async def _dispatch(self, method: str, params: dict):
            if method == "crm.deal.list":
                self.entered.set()
                await self.gate.wait()
            return await super()._dispatch(method, params)

    db = Database(str(tmp_path / "gate.db"))
    await db.init()
    bx = GatedBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    flow = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    try:
        await start_reminder(flow, monkeypatch)
        context = dp.fsm.get_context(bot=bot, chat_id=1, user_id=1)
        message = make_message_update(bot, "сантехника").message
        answer_task = asyncio.create_task(
            handle_binding_answer(message, context, db, "сантехника", bx)
        )
        await asyncio.wait_for(bx.entered.wait(), timeout=5)
        await context.clear()
        bx.gate.set()
        await asyncio.wait_for(answer_task, timeout=5)

        assert flow.bx.tasks == []
        assert await flow.db.pending_task_reminders() == []
        assert all("Пришлю" not in t for t in flow.session.sent_texts)
    finally:
        await dp.storage.close()


async def test_answer_with_posledn_inside_name_is_text_search(flow, monkeypatch):
    """«Последняя миля» — поисковый запрос, а не «последняя заявка» (Sol R1)."""
    await start_reminder(flow, monkeypatch)
    await send(flow, "Последняя миля")

    # Ничего не привязано молча: совпадений нет, бот переспросил.
    assert flow.bx.tasks == []
    assert flow.session.sent_texts[-1] == BIND_NOT_FOUND
    assert await state_of(flow) == ReminderFlow.binding.state


def test_parse_binding_answer_last_forms():
    from app.services.binding import parse_binding_answer

    assert parse_binding_answer("последняя").kind == "last"
    assert parse_binding_answer("к последней заявке").kind == "last"
    assert parse_binding_answer("Последняя").kind == "last"
    assert parse_binding_answer("ООО Последний шанс").kind == "text"
    assert parse_binding_answer("Последняя миля").kind == "text"


def test_parse_binding_answer_natural_forms():
    """«Номер заявки 154», «по заявке 154» — это ID, а не текстовый поиск.

    Иначе подстрочный поиск «154» мог бы молча привязать к чужой сделке
    с «154» в названии (Sol R3).
    """
    from app.services.binding import parse_binding_answer

    for answer in (
        "номер заявки 154",
        "по заявке 154",
        "заявка 154",
        "к заявке номер 154",
        "заявка № 154",
    ):
        ref = parse_binding_answer(answer)
        assert (ref.kind, ref.value) == ("deal_id", "154"), answer
    phone = parse_binding_answer("номер телефона 89141234567")
    assert (phone.kind, phone.value) == ("phone", "+79141234567")
    # Название, начинающееся со служебного слова, остаётся текстовым поиском.
    assert parse_binding_answer("Номерной фонд").kind == "text"


async def test_multi_reminder_message_does_not_inline_bind(flow, monkeypatch):
    """Два напоминания в одном сообщении: ссылка из второго не привязывает первое.

    Sol R3: напоминание берётся из orders[0], а «к заявке 154» может
    относиться ко второму фрагменту — инлайн-привязка при многоэлементном
    вводе игнорируется, кнопочный флоу задаёт вопрос явно.
    """
    raw = "к заявке 154 через 2 часа позвонить и завтра в 9 отправить смету"
    cleaned = "через 2 часа позвонить и завтра в 9 отправить смету"

    async def fake(text: str):
        if text.lower() == cleaned:
            return [
                reminder_order("позвонить"),
                reminder_order("отправить смету"),
            ]
        return None

    monkeypatch.setattr(llm, "parse_order", fake)
    await send(flow, BTN_REMIND)
    await send(flow, raw)

    assert flow.bx.tasks == []
    assert flow.session.sent_messages[-1].text == BIND_PROMPT
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_free_text_multi_reminder_does_not_inline_bind(flow, monkeypatch):
    """Свободный текст с двумя напоминаниями не привязывается по ссылке."""
    from app.handlers.messages import BIND_INLINE_MISS

    raw = "напомни к заявке 154 завтра в 8 позвонить и завтра в 9 смета"

    async def fake(text: str):
        if text.lower() == raw:
            return [
                reminder_order("позвонить"),
                reminder_order("смета"),
            ]
        return None

    monkeypatch.setattr(llm, "parse_order", fake)
    await send(flow, raw)

    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]
    # Привязка не заявлялась надёжно: ни привязки, ни жалобы на промах.
    assert BIND_INLINE_MISS not in flow.session.sent_texts


def test_parse_binding_answer_stt_punctuation():
    """Автопунктуация STT не превращает номер заявки в текстовый запрос."""
    from app.services.binding import parse_binding_answer

    assert parse_binding_answer("154.").kind == "deal_id"
    assert parse_binding_answer("154.").value == "154"
    assert parse_binding_answer("№154,").kind == "deal_id"
    assert parse_binding_answer("К заявке 154.").kind == "deal_id"
    assert parse_binding_answer("Последняя.").kind == "last"
    assert parse_binding_answer("Без привязки.").kind == "none"


# --- Правки по ревью Sol R2 ----------------------------------------------


async def test_restarted_flow_kills_old_binding_button(flow, monkeypatch):
    """«Напоминание» заново снимает pending: старая кнопка мертва СРАЗУ.

    Окно ревью R2: вопрос A → «Напоминание» (новый ввод) → до текста B
    нажать старую кнопку A. Раньше pending A ещё лежал в FSM, nonce
    совпадал — и отменённое рестартом A создавалось.
    """
    await start_reminder(flow, monkeypatch)
    stale_button = bind_data(flow, "last")

    await send(flow, BTN_REMIND)
    assert await state_of(flow) == ReminderFlow.query.state

    await press(flow, stale_button)
    assert flow.bx.tasks == []
    assert await flow.db.pending_task_reminders() == []
    # Рестарт не сломан: ввод B работает дальше.
    assert await state_of(flow) == ReminderFlow.query.state


async def test_bind_callback_requires_binding_state(flow, monkeypatch):
    """Кнопка привязки вне шага привязки — «Уже неактуально», не создание."""
    await start_reminder(flow, monkeypatch)
    stale_button = bind_data(flow, "last")
    await send(flow, BTN_FIND)
    assert await state_of(flow) == SearchFlow.query.state

    await press(flow, stale_button)
    assert flow.bx.tasks == []
    assert await state_of(flow) == SearchFlow.query.state


async def test_consume_pending_is_atomic(flow, monkeypatch):
    """Два конкурентных потребителя pending: побеждает ровно один (Sol R2).

    MemoryStorage сам по себе не отдаёт управление между get и set; тест
    вставляет точку переключения в get_data — без взаимного исключения оба
    потребителя увидели бы pending и оба пошли бы создавать.
    """
    import asyncio

    from app.handlers.reminders import _consume_pending

    await start_reminder(flow, monkeypatch)
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    pending = (await context.get_data())["rem_pending"]
    nonce = pending["nonce"]

    orig_get = flow.dp.storage.get_data

    async def yielding_get(key):
        data = await orig_get(key)
        await asyncio.sleep(0)
        return data

    monkeypatch.setattr(flow.dp.storage, "get_data", yielding_get)

    first, second = await asyncio.gather(
        _consume_pending(context, nonce), _consume_pending(context, nonce)
    )
    winners = [item for item in (first, second) if item is not None]
    assert len(winners) == 1
    assert winners[0]["nonce"] == nonce


# --- Правки по ревью Sol R4 ----------------------------------------------


def test_parse_binding_answer_inner_punctuation():
    """«Номер заявки: 154» — точный ID, а не текстовый поиск (Sol R4)."""
    from app.services.binding import parse_binding_answer

    for answer in (
        "номер заявки: 154",
        "заявка: 154",
        "к заявке - 154",
        "номер, 154",
    ):
        ref = parse_binding_answer(answer)
        assert (ref.kind, ref.value) == ("deal_id", "154"), answer
    phone = parse_binding_answer("номер телефона: 89141234567")
    assert (phone.kind, phone.value) == ("phone", "+79141234567")


async def test_too_short_text_answer_reasks_without_search(flow, monkeypatch):
    """Ответ «а» или из одних служебных слов не запускает широкий поиск."""
    from app.handlers.reminders import BIND_QUERY_TOO_SHORT

    await start_reminder(flow, monkeypatch)
    calls: list[str] = []
    orig = flow.bx._dispatch

    async def counting(method, params):
        calls.append(method)
        return await orig(method, params)

    monkeypatch.setattr(flow.bx, "_dispatch", counting)

    await send(flow, "а")
    assert flow.session.sent_texts[-1] == BIND_QUERY_TOO_SHORT
    await send(flow, "к заявке")
    assert flow.session.sent_texts[-1] == BIND_QUERY_TOO_SHORT

    assert calls == []
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_reused_task_keeps_actual_binding(flow, monkeypatch):
    """Идемпотентный повтор называет ФАКТИЧЕСКУЮ привязку задачи (Sol R4).

    Репро ревью: задача создана с D_155, fence done записан, подтверждение
    оборвалось. К повтору «последней» стала другая сделка — но пинг и
    подтверждение обязаны называть №155 (привязку задачи), а не свежую.
    """
    from datetime import timedelta

    from app.handlers.messages import _create_reminder
    from app.services import dates
    from app.services.tasks import _key_tag

    key = "rem:sol-r4"
    flow.bx.tasks.append(
        {
            "TITLE": "Позвонить",
            "UF_CRM_TASK": ["D_155"],
            "TAGS": [_key_tag(key)],
        }
    )
    await flow.db.get_or_create_task_fence(key)
    await flow.db.mark_task_fence_sent(key)
    await flow.db.complete_task_fence(key, 77)

    deadline = (dates.now_local() + timedelta(hours=2)).isoformat()
    order = reminder_order("позвонить", deadline)
    message = make_message_update(flow.bot, "повтор").message

    created, label = await _create_reminder(
        message,
        flow.db,
        flow.bx,
        order,
        key=key,
        deal_id=156,
        deal_label="№156 · чужая сделка",
    )

    assert created
    assert label is not None and "№155" in label and "№156" not in label
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert "№155" in rows[0]["text"]
    assert "№156" not in rows[0]["text"]


# --- Свободный текст intent=reminder (Sol R1, M1) -------------------------


async def test_free_text_inline_binding_binds(flow, monkeypatch):
    """«Напомни к заявке 154 …» свободным текстом привязывает задачу."""
    text = "напомни к заявке 154 завтра в 8 позвонить заказчику"
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})

    await send(flow, text)

    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["UF_CRM_TASK"] == ["D_154"]
    replies = "\n".join(flow.session.sent_texts)
    assert "по заявке" in replies
    assert "№154" in replies
    assert await state_of(flow) is None


async def test_free_text_inline_binding_miss_creates_unbound(flow, monkeypatch):
    """Свободный текст с ненайденной заявкой ставит обычное и говорит об этом."""
    from app.handlers.messages import BIND_INLINE_MISS

    text = "напомни к заявке 999 завтра в 8 позвонить заказчику"
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})

    await send(flow, text)

    assert len(flow.bx.tasks) == 1
    assert "UF_CRM_TASK" not in flow.bx.tasks[0]
    assert BIND_INLINE_MISS in flow.session.sent_texts
