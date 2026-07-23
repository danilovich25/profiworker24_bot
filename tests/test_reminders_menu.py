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
from app.handlers.messages import OrderFlow
from app.handlers.reminders import (
    MY_REMINDERS_EMPTY,
    REMIND_NO_DATE,
    REMIND_PROMPT,
    ReminderFlow,
)
from app.handlers.search import ACTIVE_ORDER_WARNING, ASK_QUERY, SearchFlow
from app.handlers.start import BTN_FIND, BTN_MY_REMINDERS, BTN_REMIND
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
        if method == "tasks.task.get":
            index = int(params["taskId"]) - 77
            if not 0 <= index < len(self.tasks):
                return []
            fields = self.tasks[index]
            return {
                "task": {
                    "id": str(77 + index),
                    "deadline": fields.get("DEADLINE"),
                    "status": str(fields.get("STATUS", "2")),
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


async def skip_binding(flow, user_id: int = 1) -> None:
    """Отвечает «Без привязки» на вопрос о заявке кнопкой из самого вопроса."""
    from tests.conftest import make_callback_update

    data = None
    for message in reversed(flow.session.sent_messages):
        keyboard = getattr(message.reply_markup, "inline_keyboard", None)
        if not keyboard:
            continue
        for row in keyboard:
            for button in row:
                if button.callback_data and button.callback_data.endswith(":none"):
                    data = button.callback_data
                    break
    assert data is not None, "вопроса о привязке с кнопкой «Без привязки» не было"
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
    await skip_binding(flow)

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
    await skip_binding(flow)

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
    await skip_binding(flow)

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


async def test_reminders_command_leaves_reminder_flow(flow):
    """/reminders в режиме ввода показывает список и ЗАКРЫВАЕТ режим.

    Иначе следующий текст с датой молча становился бы напоминанием.
    """
    await send(flow, BTN_REMIND)
    await send(flow, "/reminders")

    assert flow.session.sent_texts[-1] == MY_REMINDERS_EMPTY
    assert await state_of(flow) is None


async def test_cancel_button_on_due_reminder_is_stale(flow):
    """Пинг, чей срок уже наступил, кнопкой не отменяется: он уже уходит.

    Планировщик шлёт до отметки, и отмена «в момент срока» рапортовала бы
    успех, завершала задачу Bitrix, а сообщение всё равно приходило.
    """
    now = int(time.time())
    rid = await flow.db.add_reminder(
        1, "почти ушедший пинг. Срок: сейчас", now - 5, "task", 88
    )

    from tests.conftest import make_callback_update

    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, f"rem:cancel:{rid}")
    )

    rows = await flow.db.pending_task_reminders()
    assert [row["id"] for row in rows] == [rid]  # пинг не тронут
    assert flow.bx.completed == []  # задача Bitrix не завершалась
    assert all("отменено" not in t.lower() for t in flow.session.sent_texts)


async def test_user_cancelled_ping_is_not_revived_by_reconcile(flow):
    """Ручная отмена терминальна: открытая задача её не воскрешает.

    Гонка ревью: пользователь отменил пинг, а reconcile успел между
    SQLite-отменой и завершением задачи в Bitrix (или завершение упало) —
    открытая задача со сроком в будущем НЕ должна оживлять отменённое
    пользователем.
    """
    from datetime import datetime, timedelta, timezone

    from app.services import tasks as tasks_service
    from tests.conftest import SemanticBitrixFake, make_callback_update

    now = int(time.time())
    future = now + 3600
    rid = await flow.db.add_reminder(
        1, "позвонить заказчику. Срок: скоро", future, "task", 77
    )
    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, f"rem:cancel:{rid}")
    )
    assert await flow.db.pending_task_reminders() == []
    assert flow.bx.completed == [77]

    deadline_iso = datetime.fromtimestamp(
        future, timezone(timedelta(hours=10))
    ).isoformat()

    class OpenTaskBitrix(SemanticBitrixFake):
        async def _dispatch(self, method: str, params: dict):
            assert method == "tasks.task.get"
            return {"task": {"id": "77", "deadline": deadline_iso, "status": "2"}}

    await tasks_service.reconcile_task_reminders(OpenTaskBitrix(), flow.db)

    assert await flow.db.pending_task_reminders() == []


async def test_cancel_long_overdue_pending_works(flow):
    """Пинг, застрявший в pending сильно позже срока, отменить МОЖНО.

    Гвард гонки с отправкой держит только окно вокруг срока: при упавшем
    планировщике или вечных ретраях пользователь не должен терять кнопку.
    """
    now = int(time.time())
    rid = await flow.db.add_reminder(
        1, "застрявший пинг. Срок: давно", now - 300, "task", 90
    )

    from tests.conftest import make_callback_update

    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, f"rem:cancel:{rid}")
    )

    assert await flow.db.pending_task_reminders() == []
    assert flow.bx.completed == [90]


class AddThenFailBitrix(FakeReminderBitrix):
    """Портал, у которого первый tasks.task.add ПРОХОДИТ, но ответ теряется."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_add_responses = 1

    async def _dispatch(self, method: str, params: dict):
        if method == "tasks.task.add" and self.lose_add_responses > 0:
            self.lose_add_responses -= 1
            self.tasks.append(params["fields"])
            raise RuntimeError("обрыв связи после отправки")
        return await super()._dispatch(method, params)


async def test_reminder_ping_survives_lost_add_response(tmp_path, bot, session, monkeypatch):
    """Ответ task.add потерялся, сверка нашла задачу — TG-пинг ВСЁ РАВНО встаёт.

    Раньше пинг ставился только при чистом ответе add: после таймаута бот
    обещал «Пришлю напоминание», но в очередь ничего не попадало.
    """
    db = Database(str(tmp_path / "lost-add.db"))
    await db.init()
    bx = AddThenFailBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    flow = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    text = "через 2 часа позвонить заказчику"
    mock_parse(monkeypatch, {text: reminder_order("позвонить заказчику")})
    try:
        await send(flow, BTN_REMIND)
        before = int(time.time())
        await send(flow, text)
        await skip_binding(flow)

        replies = "\n".join(flow.session.sent_texts)
        assert "задача №77" in replies  # сверка нашла созданную задачу
        rows = await db.pending_task_reminders()
        assert len(rows) == 1
        assert abs(rows[0]["due_ts"] - (before + 2 * 3600)) <= 120
    finally:
        await dp.storage.close()
