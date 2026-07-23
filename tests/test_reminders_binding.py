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


# --- Вопрос о привязке ---------------------------------------------------


async def test_reminder_asks_binding_question(flow, monkeypatch):
    """После текста с датой бот спрашивает про заявку, задача НЕ создана."""
    await start_reminder(flow, monkeypatch)

    msg = flow.session.sent_messages[-1]
    assert msg.text == BIND_PROMPT
    labels = [text for text, _ in buttons_of(msg)]
    assert BIND_BTN_LAST in labels
    assert BIND_BTN_NONE in labels
    data = [d for _, d in buttons_of(msg)]
    assert "rem:bind:last" in data
    assert "rem:bind:none" in data
    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_bind_none_creates_unbound(flow, monkeypatch):
    """«Без привязки» ставит обычное напоминание, как раньше."""
    await start_reminder(flow, monkeypatch)
    before = int(time.time())
    await press(flow, "rem:bind:none")

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
    await press(flow, "rem:bind:last")

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
    data = [d for _, d in buttons_of(msg)]
    assert "rem:bind:155" in data
    assert "rem:bind:154" in data
    assert "rem:bind:none" in data
    assert await state_of(flow) == ReminderFlow.binding.state

    await press(flow, "rem:bind:154")
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

    await press(flow, "rem:bind:none")
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
    await press(flow, "rem:bind:154", user_id=2)

    assert flow.bx.tasks == []
    assert await state_of(flow) == ReminderFlow.binding.state


async def test_my_reminders_shows_deal_binding(flow, monkeypatch):
    """Список «Мои напоминания» показывает, к какой заявке пинг."""
    await start_reminder(flow, monkeypatch)
    await press(flow, "rem:bind:last")
    await send(flow, BTN_MY_REMINDERS)

    listing = flow.session.sent_messages[-1]
    assert "№155" in listing.text


async def test_scheduled_deal_confirmation_mentions_deal(flow, monkeypatch):
    """Подтверждение называет заявку и дату (формат REMIND_SCHEDULED_DEAL)."""
    await start_reminder(flow, monkeypatch)
    await press(flow, "rem:bind:last")

    prefix = REMIND_SCHEDULED_DEAL.split("{when}")[0]
    assert any(t.startswith(prefix) for t in flow.session.sent_texts)
