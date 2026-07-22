"""Отдельные напоминания: кнопка «Напоминание», список «Мои напоминания», отмена.

Заказчик ставит напоминание, не привязанное к заявке: любую дату и время
плюс любой текст, текстом или голосом. Тесты гоняют полный диспетчер
(мидлвари, FSM, роутеры) поверх RecordingSession и фейк-портала: задача
зеркалится в Bitrix24 (tasks.task.add), Telegram-пинг встаёт в очередь.
"""

import time
from types import SimpleNamespace

import pytest

from app.db import Database
from app.handlers import routers
from app.handlers.reminders import (
    MY_REMINDERS_EMPTY,
    REMIND_NO_DATE,
    REMIND_PROMPT,
    ReminderFlow,
)
from app.handlers.search import ACTIVE_ORDER_WARNING, ASK_QUERY, SearchFlow
from app.handlers.start import BTN_FIND, BTN_MY_REMINDERS, BTN_REMIND
from app.handlers.messages import OrderFlow
from app.main import create_dispatcher
from app.schemas import Intent, ParsedOrder
from app.services import llm, speech
from tests.conftest import make_message_update, make_voice_update
from tests.test_handlers_messages import FakeBitrix


class FakeReminderBitrix(FakeBitrix):
    """Портал заявочного фейка плюс завершение задач (tasks.task.complete)."""

    def __init__(self) -> None:
        super().__init__()
        self.completed: list[int] = []

    async def _dispatch(self, method: str, params: dict):
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
    db = Database(str(tmp_path / "remind.db"))
    await db.init()
    bx = FakeReminderBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


async def send(flow, text: str, user_id: int = 1, **extra) -> None:
    await flow.dp.feed_update(
        flow.bot, make_message_update(flow.bot, text, user_id=user_id, **extra)
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


async def test_remind_button_prompts_force_reply(flow):
    await send(flow, BTN_REMIND)

    msg = flow.session.sent_messages[-1]
    assert msg.text == REMIND_PROMPT
    assert await state_of(flow) == ReminderFlow.query.state


async def test_reminder_created_from_text(flow, monkeypatch):
    """Текст с датой и временем ставит задачу в Bitrix24 и Telegram-пинг."""
    text = "через 2 часа позвонить заказчику"
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})

    await send(flow, BTN_REMIND)
    before = int(time.time())
    await send(flow, text)

    # Задача в Bitrix24 создана с заголовком из текста напоминания.
    assert len(flow.bx.tasks) == 1
    assert flow.bx.tasks[0]["TITLE"].startswith("Позвонить заказчику")
    # Telegram-пинг стоит в очереди на разобранный срок (сейчас + 2 часа).
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert rows[0]["chat_id"] == 1
    assert abs(rows[0]["due_ts"] - (before + 2 * 3600)) <= 120
    # Подтверждение обещает пинг в телеграм, состояние закрыто.
    replies = "\n".join(flow.session.sent_texts)
    assert "задача №77" in replies
    assert "Пришлю" in replies
    assert await state_of(flow) is None


async def test_reminder_without_date_reasks(flow, monkeypatch):
    """Без распознанной даты бот переспрашивает и остаётся в режиме ввода."""
    text = "позвонить заказчику"
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})

    await send(flow, BTN_REMIND)
    await send(flow, text)

    assert flow.session.sent_texts[-1] == REMIND_NO_DATE
    assert flow.bx.tasks == []
    assert await flow.db.pending_task_reminders() == []
    assert await state_of(flow) == ReminderFlow.query.state


async def test_reminder_llm_down_falls_back_to_raw_text(flow, monkeypatch):
    """Модель лежит: срок берёт детерминированный парсер, текст — как прислан."""

    async def failing(text: str):
        raise llm.LLMUnavailable("модель недоступна")

    monkeypatch.setattr(llm, "parse_order", failing)

    await send(flow, BTN_REMIND)
    before = int(time.time())
    await send(flow, "через 2 часа позвонить заказчику")

    assert len(flow.bx.tasks) == 1
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert abs(rows[0]["due_ts"] - (before + 2 * 3600)) <= 120
    assert "позвонить заказчику" in rows[0]["text"]


async def test_reminder_voice_creates(flow, monkeypatch):
    """Голос в режиме напоминания работает как текст: распознал и поставил."""
    text = "через 2 часа позвонить заказчику"

    async def fake_stt(data: bytes) -> str:
        return text

    monkeypatch.setattr(speech, "recognize_ogg", fake_stt)
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})
    flow.session.stream_chunks = [b"OggS", b"data"]

    await send(flow, BTN_REMIND)
    await flow.dp.feed_update(flow.bot, make_voice_update(flow.bot))

    assert len(flow.bx.tasks) == 1
    rows = await flow.db.pending_task_reminders()
    assert len(rows) == 1
    assert await state_of(flow) is None


async def test_my_reminders_lists_and_cancel_button_works(flow):
    """Список показывает оба вида пингов; отмена доступна только отдельным."""
    now = int(time.time())
    task_id = await flow.db.add_reminder(
        1, "позвонить заказчику. Срок: 23.07.2026 08:00", now + 3600, "task", 77
    )
    await flow.db.add_reminder(
        1, "заявка №78 — электрика. Срок: 23.07.2026 10:00", now + 7200, "deal", 78, 14
    )

    await send(flow, BTN_MY_REMINDERS)

    listing = flow.session.sent_messages[-1]
    assert "позвонить заказчику" in listing.text
    assert "№78" in listing.text
    buttons = [
        b for row in listing.reply_markup.inline_keyboard for b in row
    ]
    assert [b.callback_data for b in buttons] == [f"rem:cancel:{task_id}"]

    from tests.conftest import make_callback_update

    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, f"rem:cancel:{task_id}")
    )

    assert await flow.db.pending_task_reminders() == []
    assert flow.bx.completed == [77]
    # Пинг сделки не тронут: им управляет дело в Битриксе.
    assert (await flow.db.pending_deal_reminder(78)) is not None


async def test_my_reminders_empty(flow):
    await send(flow, BTN_MY_REMINDERS)
    assert flow.session.sent_texts[-1] == MY_REMINDERS_EMPTY


async def test_menu_buttons_escape_reminder_flow(flow):
    """Кнопка «Найти» из режима напоминания уводит в поиск, а не в текст."""
    await send(flow, BTN_REMIND)
    await send(flow, BTN_FIND)

    assert flow.session.sent_texts[-1] == ASK_QUERY
    assert await state_of(flow) == SearchFlow.query.state


async def test_remind_button_protected_during_active_order(flow):
    """Посреди уточнений заявки кнопки напоминаний не рвут диалог."""
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    await context.set_state(OrderFlow.ask_phone)

    await send(flow, BTN_REMIND)
    assert flow.session.sent_texts[-1] == ACTIVE_ORDER_WARNING
    await send(flow, BTN_MY_REMINDERS)
    assert flow.session.sent_texts[-1] == ACTIVE_ORDER_WARNING
    assert await state_of(flow) == OrderFlow.ask_phone.state


async def test_cancel_command_leaves_reminder_flow(flow):
    await send(flow, BTN_REMIND)
    await send(flow, "/cancel")

    assert await state_of(flow) is None
