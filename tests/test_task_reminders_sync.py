"""Синхронизация Bitrix → бот для отдельных напоминаний (kind=task).

Отдельное напоминание зеркалится задачей Bitrix24 (tasks.task.add), и
заказчик правит её прямо в портале: переносит крайний срок или завершает.
Очередь Telegram-пингов обязана догонять такие правки — тем же рисунком,
что и напоминания сделок: перед отправкой и в периодической сверке.

Тесты гоняют НАСТОЯЩИЙ планировщик (send_due_reminders / reconcile) поверх
подменённого транспорта: Telegram — RecordingSession, Bitrix — фейк с
клиентской семантикой fast-bitrix24, отвечающий на tasks.task.get как
сервер: конвертом {"task": {...}} с полями в нижнем регистре.
"""

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

import pytest
from fast_bitrix24.server_response import ErrorInServerResponseException

from app.db import Database
from app.services import dates, tasks
from tests.conftest import SemanticBitrixFake

TASK = 55
CHAT = 1

DUE = int(datetime.fromisoformat("2026-07-23T08:00:00+10:00").timestamp())
TEXT = "позвонить заказчику. Срок: 23.07.2026 08:00"


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


class FakeTaskBitrix(SemanticBitrixFake):
    """«Портал» с одной задачей: серверная выдача tasks.task.get."""

    def __init__(
        self,
        deadline: str | None = None,
        status: str = "2",
        missing: bool = False,
        fail: bool = False,
    ):
        self.deadline = deadline
        self.status = status
        self.missing = missing
        self.fail = fail
        self.get_calls = 0

    async def _dispatch(self, method: str, params: dict) -> Any:
        assert method == "tasks.task.get", f"неожиданный метод {method}"
        self.get_calls += 1
        if self.fail:
            raise RuntimeError("сеть недоступна")
        if self.missing:
            raise ErrorInServerResponseException(
                "ERROR_TASK_NOT_FOUND_OR_NOT_ACCESSIBLE: Задача не найдена"
            )
        assert int(params.get("taskId")) == TASK
        return {
            "task": {"id": str(TASK), "deadline": self.deadline, "status": self.status}
        }


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "task-sync.db"))
    await database.init()
    return database


async def test_task_deadline_moved_in_bitrix_moves_tg_ping(db, bot, session):
    """Крайний срок задачи перенесли в Bitrix24 — пинг переезжает за ним.

    Заказчик спрашивал ровно это: «если поменяю дату напоминания, оно
    синхронизируется с тг ботом?» Без сверки пинг ушёл бы по старой дате.
    """
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    moved = "2026-07-23T12:30:00+10:00"
    bx = FakeTaskBitrix(deadline=moved)

    assert await tasks.reconcile_task_reminders(bx, db) == 1

    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [epoch(moved)]
    assert "Срок: 23.07.2026 12:30" in rows[0]["text"]

    # В старый момент — тишина, в новый пинг уходит с новой датой.
    assert await tasks.send_due_reminders(bot, db, now_ts=DUE + 5, bitrix=bx) == 0
    assert session.sent_texts == []
    assert (
        await tasks.send_due_reminders(bot, db, now_ts=epoch(moved) + 5, bitrix=bx) == 1
    )
    assert "23.07.2026 12:30" in session.sent_texts[-1]


async def test_presend_sync_holds_task_ping(db, bot, session):
    """Перенос, сделанный до сверки, ловится прямо в момент отправки."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    moved = "2026-07-23T12:30:00+10:00"
    bx = FakeTaskBitrix(deadline=moved)

    assert await tasks.send_due_reminders(bot, db, now_ts=DUE + 5, bitrix=bx) == 0

    assert session.sent_texts == []
    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [epoch(moved)]


async def test_completed_task_cancels_ping(db, bot, session):
    """Завершённая в Bitrix24 задача снимает Telegram-пинг: напоминать не о чем."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    bx = FakeTaskBitrix(deadline=dates.epoch_to_iso(DUE), status="5")

    await tasks.reconcile_task_reminders(bx, db)

    assert await db.pending_task_reminders() == []
    assert await tasks.send_due_reminders(bot, db, now_ts=DUE + 5, bitrix=bx) == 0
    assert session.sent_texts == []


async def test_deleted_task_cancels_ping(db, bot, session):
    """Удалённая задача (портал явно отвечает «не найдена») снимает пинг."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    bx = FakeTaskBitrix(missing=True)

    await tasks.reconcile_task_reminders(bx, db)

    assert await db.pending_task_reminders() == []
    assert await tasks.send_due_reminders(bot, db, now_ts=DUE + 5, bitrix=bx) == 0
    assert session.sent_texts == []


@pytest.mark.parametrize(
    "error",
    [
        "ACCESS_DENIED: нет прав на задачу",
        "ERROR_METHOD_NOT_FOUND: Method not found",
        # Код без человеческого «задача не найдена» неоднозначен: задача
        # может существовать, но быть недоступной — снимать пинг нельзя.
        "ERROR_TASK_NOT_FOUND_OR_NOT_ACCESSIBLE: доступ ограничен",
    ],
)
async def test_ambiguous_refusal_keeps_ping(db, bot, session, error):
    """Отказ портала БЕЗ явного «задача не найдена» — fail-open.

    ACCESS_DENIED, METHOD_NOT_FOUND и прочие отказы не доказывают, что
    задачи нет: снятие пинга по ним хоронило бы живое напоминание навсегда.
    """
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)

    class DeniedBitrix(SemanticBitrixFake):
        async def _dispatch(self, method: str, params: dict) -> Any:
            raise ErrorInServerResponseException(error)

    await tasks.reconcile_task_reminders(DeniedBitrix(), db)

    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [DUE]


async def test_reopened_task_revives_cancelled_ping(db, bot, session):
    """Задачу завершили, пинг снят; переоткрыли со сроком в будущем — пинг ожил.

    Без воскрешения отмена была бы терминальной: переоткрытая в Bitrix24
    задача молчала бы в Telegram навсегда.
    """
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    await tasks.reconcile_task_reminders(
        FakeTaskBitrix(deadline=dates.epoch_to_iso(DUE), status="5"), db
    )
    assert await db.pending_task_reminders() == []

    future = int(time.time()) + 7200
    bx = FakeTaskBitrix(deadline=dates.epoch_to_iso(future), status="2")
    await tasks.reconcile_task_reminders(bx, db)

    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [future]
    assert dates.format_epoch(future) in rows[0]["text"]


async def test_completed_task_stays_cancelled(db, bot, session):
    """Завершённая задача не воскрешает пинг, сколько бы сверок ни прошло."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    bx = FakeTaskBitrix(deadline=dates.epoch_to_iso(int(time.time()) + 7200), status="5")
    await tasks.reconcile_task_reminders(bx, db)
    await tasks.reconcile_task_reminders(bx, db)

    assert await db.pending_task_reminders() == []


async def test_crm_failure_keeps_stored_schedule(db, bot, session):
    """Недоступный портал не трогает очередь: fail-open, пинг по сохранённому.

    Молчание из-за упавшей сети хуже напоминания по чуть устаревшей дате —
    транспортный сбой не равен решению заказчика.
    """
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    bx = FakeTaskBitrix(fail=True)

    await tasks.reconcile_task_reminders(bx, db)

    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [DUE]
    assert await tasks.send_due_reminders(bot, db, now_ts=DUE + 5, bitrix=bx) == 1
    assert "позвонить заказчику" in session.sent_texts[-1]


async def test_empty_deadline_keeps_stored_schedule(db, bot, session):
    """Задача без разобранного срока не роняет пинг: живём по сохранённому."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)
    bx = FakeTaskBitrix(deadline=None)

    await tasks.reconcile_task_reminders(bx, db)

    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [DUE]


async def test_task_sync_tolerance_boundary(db, bot, session):
    """Граница совпадения сроков — жёсткие секунды: 59 — та же дата, 60 — перенос."""
    await db.add_reminder(CHAT, TEXT, DUE, "task", TASK)

    await tasks.reconcile_task_reminders(
        FakeTaskBitrix(deadline=dates.epoch_to_iso(DUE + 59)), db
    )
    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [DUE]

    await tasks.reconcile_task_reminders(
        FakeTaskBitrix(deadline=dates.epoch_to_iso(DUE + 60)), db
    )
    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [DUE + 60]


async def test_reminder_loop_reconciles_tasks_at_startup(db, bot):
    """Первая сверка задач — сразу при старте цикла, как и у сделок."""
    now = int(time.time())
    far_due = now + 9 * 86400
    near_due = now + 2 * 3600
    text = f"позвонить заказчику. Срок: {dates.format_epoch(far_due)}"
    await db.add_reminder(CHAT, text, far_due, "task", TASK)
    bx = FakeTaskBitrix(deadline=dates.epoch_to_iso(near_due))

    loop_task = asyncio.create_task(tasks.reminder_loop(bot, db, bitrix=bx))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = await db.pending_task_reminders()
            if rows and rows[0]["due_ts"] == near_due:
                break
            await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    assert bx.get_calls >= 1
    rows = await db.pending_task_reminders()
    assert [row["due_ts"] for row in rows] == [near_due]
