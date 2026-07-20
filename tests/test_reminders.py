"""Telegram-напоминания: очередь в SQLite и планировщик send_due_reminders.

Планировщик тестируется по одному проходу с явным «сейчас» (now_ts), поэтому
тесты детерминированы. Telegram подменён RecordingSession из conftest.
"""

import pytest
from aiogram.methods import SendMessage

from app.db import Database
from app.services import tasks


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "reminders.db"))
    await database.init()
    return database


async def test_due_reminder_is_sent_once(db, bot, session):
    await db.add_reminder(
        1, "заявка №154 — замена крана. Срок: 24.07.2026 10:00", 1000, "deal", 154
    )

    assert await tasks.send_due_reminders(bot, db, now_ts=1001) == 1
    text = session.sent_texts[-1]
    assert text.startswith("⏰ Напоминание: ")
    assert "заявка №154" in text and "24.07.2026 10:00" in text

    # отправленное не уходит второй раз
    assert await tasks.send_due_reminders(bot, db, now_ts=2000) == 0
    assert len(session.sent_texts) == 1


async def test_future_reminder_waits_for_its_moment(db, bot, session):
    await db.add_reminder(1, "рано", 2000)

    assert await tasks.send_due_reminders(bot, db, now_ts=1999) == 0
    assert session.sent_texts == []

    assert await tasks.send_due_reminders(bot, db, now_ts=2000) == 1


async def test_reminders_survive_restart(tmp_path, bot, session):
    """Очередь в SQLite: «рестарт» (новый Database) ничего не теряет."""
    path = str(tmp_path / "restart.db")
    before = Database(path)
    await before.init()
    await before.add_reminder(1, "пережить рестарт", 1000)

    after = Database(path)  # процесс поднялся заново
    await after.init()

    assert await tasks.send_due_reminders(bot, after, now_ts=1500) == 1
    assert "пережить рестарт" in session.sent_texts[-1]


async def test_send_failure_retries_then_gives_up(db, bot, session, monkeypatch):
    await db.add_reminder(1, "недоставляемое", 1000)
    attempts = {"n": 0}
    original = session.make_request

    async def failing(bot_, method, timeout=None):
        if isinstance(method, SendMessage):
            attempts["n"] += 1
            raise RuntimeError("сеть Telegram недоступна")
        return await original(bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", failing)

    for _ in range(tasks.REMINDER_MAX_ATTEMPTS):
        assert await tasks.send_due_reminders(bot, db, now_ts=1001) == 0

    # попытки исчерпаны: напоминание помечено failed и больше не дёргается
    assert await tasks.send_due_reminders(bot, db, now_ts=1002) == 0
    assert attempts["n"] == tasks.REMINDER_MAX_ATTEMPTS


async def test_send_failure_then_success_delivers(db, bot, session, monkeypatch):
    """Разовый сбой сети не теряет напоминание — следующий проход доставит."""
    await db.add_reminder(1, "со второй попытки", 1000)
    state = {"failed": False}
    original = session.make_request

    async def flaky(bot_, method, timeout=None):
        if isinstance(method, SendMessage) and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("сеть моргнула")
        return await original(bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", flaky)

    assert await tasks.send_due_reminders(bot, db, now_ts=1001) == 0
    assert await tasks.send_due_reminders(bot, db, now_ts=1002) == 1
    assert "со второй попытки" in session.sent_texts[-1]


async def test_drop_pending_deal_reminders_for_reschedule(db, bot, session):
    """Перенос срока: старое напоминание сделки снимается, отправленное — нет."""
    first = await db.add_reminder(1, "старый срок", 1000, "deal", 154, 500)
    await db.mark_reminder_sent(first)
    await db.add_reminder(1, "ещё не отправлено", 3000, "deal", 154, 500)
    await db.add_reminder(1, "другая сделка", 3000, "deal", 155)

    dropped = await db.drop_pending_deal_reminders(154)

    assert dropped == 1
    left = await db.due_reminders(4102444800)  # «2100 год»: всё наступило
    assert [r["entity_id"] for r in left] == [155]


async def test_pending_deal_reminder_returns_activity_id(db):
    await db.add_reminder(1, "текст", 3000, "deal", 154, 500)

    pending = await db.pending_deal_reminder(154)

    assert pending is not None
    assert pending["activity_id"] == 500
    assert pending["due_ts"] == 3000
    assert await db.pending_deal_reminder(999) is None
