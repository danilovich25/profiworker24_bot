"""Синхронизация CRM → бот для Telegram-напоминаний сделок.

«Назначенная дата» заявки живёт в деле сделки (crm.activity.todo), и заказчик
переносит её прямо в Bitrix24 — правит дело бота или заводит своё в карточке.
Очередь напоминаний обязана догонять такие правки: перед отправкой и в
периодической сверке (reconcile_deal_reminders).

Тесты гоняют НАСТОЯЩИЙ планировщик (send_due_reminders / reconcile) поверх
подменённого транспорта: Telegram — RecordingSession, Bitrix — фейк с
клиентской семантикой fast-bitrix24 (SemanticBitrixFake), который отвечает на
crm.activity.list как сервер — фильтром по OWNER_ID/COMPLETED и конвертом
с постраничной выдачей.
"""

from datetime import datetime
from typing import Any

import pytest
from fast_bitrix24.server_response import ErrorInServerResponseException

from app.db import Database
from app.services import dates, tasks
from app.services.bitrix import TODO_OWNER_TYPE_DEAL
from tests.conftest import SemanticBitrixFake

DEAL = 78

# Срок, записанный ботом при создании заявки: 21.07.2026 10:00 Владивосток.
OLD_DUE = int(datetime.fromisoformat("2026-07-21T10:00:00+10:00").timestamp())
OLD_TEXT = "заявка №78 — повесить люстру. Срок: 21.07.2026 10:00"


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def todo(
    todo_id: int,
    deadline: str,
    deal_id: int = DEAL,
    completed: str = "N",
    subject: str = "Заявка №78: повесить люстру",
) -> dict[str, Any]:
    """Дело сделки в форме ответа crm.activity.list."""
    return {
        "ID": str(todo_id),
        "OWNER_ID": deal_id,
        "OWNER_TYPE_ID": TODO_OWNER_TYPE_DEAL,
        "SUBJECT": subject,
        "DEADLINE": deadline,
        "COMPLETED": completed,
        "PROVIDER_TYPE_ID": "TODO",
    }


class FakeTodoBitrix(SemanticBitrixFake):
    """«Портал» с делами сделок: серверная фильтрация crm.activity.list."""

    def __init__(self, todos: list[dict[str, Any]] | None = None, fail: bool = False):
        self.todos = list(todos or [])
        self.fail = fail
        self.list_calls = 0

    async def _dispatch(self, method: str, params: dict) -> Any:
        assert method == "crm.activity.list", f"неожиданный метод {method}"
        self.list_calls += 1
        if self.fail:
            raise ErrorInServerResponseException("QUERY_ERROR: портал недоступен")
        filt = params.get("filter") or {}
        assert filt.get("OWNER_TYPE_ID") == TODO_OWNER_TYPE_DEAL
        assert filt.get("COMPLETED") == "N"
        assert filt.get("PROVIDER_TYPE_ID") == "TODO"
        return [
            row
            for row in self.todos
            if row["OWNER_ID"] == filt.get("OWNER_ID") and row["COMPLETED"] != "Y"
        ]


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "sync.db"))
    await database.init()
    return database


async def test_crm_moved_deadline_later_holds_sending(db, bot, session):
    """Срок перенесли в CRM позже: в старый момент бот молчит и ждёт нового.

    Это главный сценарий жалобы заказчика: дата поменялась в Bitrix24, а
    очередь бота жила по старой. Без сверки напоминание ушло бы в старый
    момент и с устаревшим текстом.
    """
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)
    # 25.07 09:00 по зоне портала (+03) = 25.07 16:00 во Владивостоке.
    bx = FakeTodoBitrix([todo(14, "2026-07-25T09:00:00+03:00")])

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 0

    assert session.sent_texts == []  # в старый момент — тишина
    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == epoch("2026-07-25T09:00:00+03:00")
    assert "Срок: 25.07.2026 16:00" in pending["text"]  # текст догнал CRM

    # В новый момент напоминание уходит, и дата в нём — новая.
    new_due = pending["due_ts"]
    assert await tasks.send_due_reminders(bot, db, now_ts=new_due + 5, bitrix=bx) == 1
    assert "25.07.2026 16:00" in session.sent_texts[-1]


async def test_crm_moved_deadline_earlier_reconcile_pulls_it_in(db, bot, session):
    """Срок перенесли в CRM на более раннее время — сверка догоняет ДО отправки.

    Проверка только в момент отправки такую правку прозевала бы: сохранённый
    срок ещё не наступил, и планировщик даже не смотрел бы на запись.
    """
    far_due = epoch("2026-08-21T10:00:00+10:00")
    await db.add_reminder(1, "заявка №78 — повесить люстру. Срок: 21.08.2026 10:00",
                          far_due, "deal", DEAL, 14)
    near = "2026-07-21T03:03:00+03:00"  # 10:03 по Владивостоку
    bx = FakeTodoBitrix([todo(14, near)])

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    pending = await db.pending_deal_reminder(DEAL)
    assert pending["due_ts"] == epoch(near)
    assert "Срок: 21.07.2026 10:03" in pending["text"]

    assert await tasks.send_due_reminders(bot, db, now_ts=epoch(near) + 5, bitrix=bx) == 1
    assert "21.07.2026 10:03" in session.sent_texts[-1]


async def test_manual_todo_replaces_completed_bot_todo(db, bot, session):
    """Дело бота закрыли, в карточке завели своё — напоминание едет за ним.

    Именно так заказчик проверял напоминания 21.07: завершил дело бота и
    создал в сделке собственное дело с нужным временем. Бот о таком деле не
    знал и молчал.
    """
    far_due = epoch("2026-08-21T10:00:00+10:00")
    await db.add_reminder(1, OLD_TEXT, far_due, "deal", DEAL, 14)
    manual = "2026-07-21T04:00:00+03:00"  # 11:00 по Владивостоку
    bx = FakeTodoBitrix(
        [
            todo(14, "2026-08-21T03:00:00+03:00", completed="Y"),  # дело бота закрыто
            todo(16, manual, subject="Позвонить клиенту"),  # ручное дело в CRM
        ]
    )

    assert await tasks.reconcile_deal_reminders(bx, db) == 1
    pending = await db.pending_deal_reminder(DEAL)
    assert pending["activity_id"] == 16  # привязка переехала на ручное дело
    assert pending["due_ts"] == epoch(manual)

    assert await tasks.send_due_reminders(bot, db, now_ts=epoch(manual) + 5, bitrix=bx) == 1
    assert "21.07.2026 11:00" in session.sent_texts[-1]


async def test_all_todos_closed_cancels_reminder(db, bot, session):
    """Дел со сроком не осталось — напоминание отменяется, а не шлётся."""
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)
    bx = FakeTodoBitrix([])  # всё завершили или удалили прямо в CRM

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 0

    assert session.sent_texts == []
    assert await db.pending_deal_reminder(DEAL) is None
    # Отменённое не оживает на следующих проходах.
    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 500, bitrix=bx) == 0
    assert session.sent_texts == []


async def test_reminder_without_todo_link_survives_empty_crm(db, bot, session):
    """Дело не создалось при заведении заявки — напоминание всё равно уходит.

    Telegram-канал обещан «гарантированным»: пустой список дел при
    activity_id=None означает «сверять не с чем», а не «дату отменили».
    """
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, None)
    bx = FakeTodoBitrix([])

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 1
    assert "заявка №78" in session.sent_texts[-1]


async def test_crm_failure_falls_open_and_sends(db, bot, session):
    """Портал недоступен — напоминание уходит по сохранённому сроку.

    Молчание из-за сбоя CRM хуже напоминания по чуть устаревшей дате.
    """
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)
    bx = FakeTodoBitrix(fail=True)

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 1
    assert "заявка №78" in session.sent_texts[-1]


async def test_same_deadline_in_portal_zone_sends_without_churn(db, bot, session):
    """Тот же момент в зоне портала (+03) — совпадение, не «перенос».

    21.07 10:00 Владивостока и 21.07 03:00 портала — одно время: очередь не
    дёргается и напоминание уходит в срок.
    """
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)
    bx = FakeTodoBitrix([todo(14, "2026-07-21T03:00:00+03:00")])

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 1
    # Текст исходный: сверка не переписывала его из-за нулевой разницы.
    assert "21.07.2026 10:00" in session.sent_texts[-1]


async def test_task_reminders_are_not_synced_with_deals(db, bot, session):
    """Напоминания-задачи (intent=reminder) сверка сделок не трогает."""
    await db.add_reminder(1, "перезвонить поставщику. Срок: 21.07.2026 10:00",
                          OLD_DUE, "task", 5)
    bx = FakeTodoBitrix([])

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 1
    assert bx.list_calls == 0  # в CRM за делами сделок не ходили


async def test_reminder_without_bitrix_keeps_old_behaviour(db, bot, session):
    """Без клиента Bitrix (CRM не настроена) очередь работает как раньше."""
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5) == 1
    assert "заявка №78" in session.sent_texts[-1]


def test_bitrix_deadline_epoch_parses_portal_forms():
    """Разбор DEADLINE: полный ISO, короткий офсет «+03», мусор и пустота."""
    assert dates.bitrix_deadline_epoch("2026-07-21T03:03:00+03:00") == epoch(
        "2026-07-21T03:03:00+03:00"
    )
    # Живой портал отвечал и коротким офсетом («2026-07-21T03:03+03»).
    assert dates.bitrix_deadline_epoch("2026-07-21T03:03+03") == epoch(
        "2026-07-21T03:03:00+03:00"
    )
    # Без зоны — местное время приложения (Asia/Vladivostok).
    assert dates.bitrix_deadline_epoch("2026-07-21T10:00:00") == epoch(
        "2026-07-21T10:00:00+10:00"
    )
    assert dates.bitrix_deadline_epoch("") is None
    assert dates.bitrix_deadline_epoch(None) is None
    assert dates.bitrix_deadline_epoch("не дата") is None
