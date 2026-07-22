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

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

import aiosqlite
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


# ---------------------------------------------------------------------------
# Просроченные дела: ненаступивший срок важнее старого хвоста
# ---------------------------------------------------------------------------


def test_nearest_todo_prefers_upcoming_over_overdue():
    """Просроченное дело не перебивает ненаступившее.

    Бот не завершает своё дело после отправки напоминания, поэтому рядом с
    актуальным делом в сделке висят старые просроченные хвосты. Ближайшим
    считается ненаступившее — перенос на хвост утащил бы напоминание в
    прошлое и выстрелил немедленно.
    """
    now = int(time.time())
    overdue = todo(9, dates.epoch_to_iso(now - 86400), subject="Старый хвост")
    upcoming = todo(14, dates.epoch_to_iso(now + 86400))
    assert tasks.nearest_todo([overdue, upcoming], now) == (14, now + 86400)
    assert tasks.nearest_todo([upcoming, overdue], now) == (14, now + 86400)


def test_nearest_todo_picks_earliest_of_two_upcoming():
    """Из двух ненаступивших дел ближайшее — с меньшим сроком, в любом порядке."""
    now = int(time.time())
    near = todo(21, dates.epoch_to_iso(now + 3600))
    far = todo(22, dates.epoch_to_iso(now + 7200))
    assert tasks.nearest_todo([near, far], now) == (21, now + 3600)
    assert tasks.nearest_todo([far, near], now) == (21, now + 3600)


def test_nearest_todo_overdue_fallback_picks_latest():
    """Все дела просрочены — берётся самое позднее, ближайшее к «сейчас»."""
    now = int(time.time())
    older = todo(9, dates.epoch_to_iso(now - 3 * 86400))
    newer = todo(14, dates.epoch_to_iso(now - 3600))
    assert tasks.nearest_todo([older, newer], now) == (14, now - 3600)
    assert tasks.nearest_todo([newer, older], now) == (14, now - 3600)


async def test_overdue_todo_does_not_drag_reminder_into_past(db, bot, session):
    """Просроченный хвост дел не утаскивает ожидающее напоминание в прошлое.

    Напоминание ждёт будущего срока, а в CRM рядом с актуальным делом висит
    старое незакрытое. Перенос на просроченное дело выстрелил бы немедленно,
    и реальная дата потерялась бы.
    """
    now = int(time.time())
    future_due = now + 9 * 86400
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(future_due)}"
    await db.add_reminder(1, text, future_due, "deal", DEAL, 14)
    bx = FakeTodoBitrix(
        [
            todo(9, dates.epoch_to_iso(now - 86400), subject="Старый хвост"),
            todo(14, dates.epoch_to_iso(future_due)),
        ]
    )

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    pending = await db.pending_deal_reminder(DEAL)
    assert pending["due_ts"] == future_due  # осталось на актуальном деле
    assert pending["activity_id"] == 14
    assert await tasks.send_due_reminders(bot, db, now_ts=now + 5, bitrix=bx) == 0
    assert session.sent_texts == []  # немедленного выстрела нет


async def test_single_overdue_todo_still_delivers_late(db, bot, session):
    """Единственное дело просрочено (планировщик спал) — напоминание уходит.

    Просроченные дела — fallback, а не мусор: опоздавшая отправка лучше
    отменённой, дубль лучше молчания.
    """
    now = int(time.time())
    stored_due = now - 2 * 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(stored_due)}"
    await db.add_reminder(1, text, stored_due, "deal", DEAL, 14)
    bx = FakeTodoBitrix([todo(14, dates.epoch_to_iso(stored_due))])

    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=bx) == 1
    assert "заявка №78" in session.sent_texts[-1]


# ---------------------------------------------------------------------------
# Границы допуска сверки
# ---------------------------------------------------------------------------


async def test_move_by_exactly_one_minute_is_synced(db):
    """Перенос ровно на минуту — это перенос, а не дрейф.

    Секунды в тесте НАРОЧНО жёсткие, не через SYNC_TOLERANCE_SECONDS: тест
    фиксирует сам порог — раздутый допуск молча терял бы минутные переносы.
    """
    now = int(time.time())
    due = now + 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    await db.add_reminder(1, text, due, "deal", DEAL, 14)
    moved = due + 60  # ровно минута
    bx = FakeTodoBitrix([todo(14, dates.epoch_to_iso(moved))])

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    pending = await db.pending_deal_reminder(DEAL)
    assert pending["due_ts"] == moved
    assert f"Срок: {dates.format_epoch(moved)}" in pending["text"]


async def test_thirty_minute_move_is_never_a_drift(db):
    """Полчаса — заведомо перенос: порог допуска не может его проглотить."""
    now = int(time.time())
    due = now + 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    await db.add_reminder(1, text, due, "deal", DEAL, 14)
    moved = due + 30 * 60
    bx = FakeTodoBitrix([todo(14, dates.epoch_to_iso(moved))])

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    assert (await db.pending_deal_reminder(DEAL))["due_ts"] == moved


async def test_move_within_tolerance_keeps_queue_calm(db):
    """Дрейф в 59 секунд (точность портала — минута) очередь не дёргает."""
    now = int(time.time())
    due = now + 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    await db.add_reminder(1, text, due, "deal", DEAL, 14)
    drifted = due + 59
    bx = FakeTodoBitrix([todo(14, dates.epoch_to_iso(drifted))])

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    pending = await db.pending_deal_reminder(DEAL)
    assert pending["due_ts"] == due  # без изменений
    assert pending["text"] == text


async def test_unparsed_deadlines_fail_open_not_cancel(db, bot, session):
    """Дела есть, но их сроки не разобрались — напоминание живёт дальше.

    Непарсибельный DEADLINE — сбой чтения, а не «с датой разобрались»:
    отмена по нему молча теряла бы напоминание. Fail-open — очередь работает
    по сохранённому сроку.
    """
    now = int(time.time())
    due = now + 3600
    await db.add_reminder(1, OLD_TEXT, due, "deal", DEAL, 14)
    bx = FakeTodoBitrix([todo(14, "завтра к обеду")])  # DEADLINE не ISO

    assert await tasks.reconcile_deal_reminders(bx, db) == 1

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == due
    assert await tasks.send_due_reminders(bot, db, now_ts=due + 5, bitrix=bx) == 1
    assert "заявка №78" in session.sent_texts[-1]


# ---------------------------------------------------------------------------
# Воскрешение отменённых напоминаний
# ---------------------------------------------------------------------------


async def test_cancelled_reminder_revives_on_new_manual_todo(db, bot, session):
    """Отменённое напоминание воскресает, когда у сделки появляется дело.

    Живой сценарий: заказчик завершил дело бота и через пару минут завёл в
    карточке своё. Если между этими действиями успела пройти сверка, она
    отменила напоминание по пустому списку дел — ручное дело со сроком в
    будущем обязано вернуть его в очередь, иначе Telegram промолчит.
    """
    now = int(time.time())
    due = now + 3 * 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    await db.add_reminder(1, text, due, "deal", DEAL, 14)

    # Тик сверки между «завершил дело бота» и «завёл своё»: дел нет — отмена.
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    manual_due = now + 2 * 3600
    bx = FakeTodoBitrix(
        [todo(16, dates.epoch_to_iso(manual_due), subject="Позвонить клиенту")]
    )
    await tasks.reconcile_deal_reminders(bx, db)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None  # напоминание вернулось в очередь
    assert pending["activity_id"] == 16
    assert pending["due_ts"] == manual_due
    assert f"Срок: {dates.format_epoch(manual_due)}" in pending["text"]

    sent = await tasks.send_due_reminders(bot, db, now_ts=manual_due + 5, bitrix=bx)
    assert sent == 1
    assert "заявка №78" in session.sent_texts[-1]


async def test_cancelled_reminder_ignores_overdue_todo(db, bot, session):
    """Просроченное дело отменённое напоминание не воскрешает.

    Воскрешение — только по делу со сроком в будущем: срабатывание задним
    числом по старому хвосту хуже тишины, с той датой уже разобрались.
    """
    now = int(time.time())
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1

    bx = FakeTodoBitrix([todo(16, dates.epoch_to_iso(now - 3600))])
    await tasks.reconcile_deal_reminders(bx, db)

    assert await db.pending_deal_reminder(DEAL) is None
    assert await tasks.send_due_reminders(bot, db, now_ts=now + 10, bitrix=bx) == 0
    assert session.sent_texts == []


async def test_revival_window_counts_from_cancellation_not_creation(db):
    """Окно воскрешения отсчитывается от момента ОТМЕНЫ, а не создания записи.

    Живой провал: заявка со сроком «через две недели» создана десять дней
    назад; сегодня заказчик завершил дело бота (сверка отменила запись) и
    через пару минут завёл в карточке своё дело в будущем. Окно по
    created_at такую запись не видит никогда — хотя отменили её только что,
    и воскрешение обязано её подобрать.
    """
    now = int(time.time())
    due = now + 4 * 86400
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    rid = await db.add_reminder(1, text, due, "deal", DEAL, 14)
    # Запись живёт в базе десять дней (заявку завели сильно заранее).
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE reminders SET created_at = datetime('now', '-10 days') "
            "WHERE id = ?",
            (rid,),
        )
        await conn.commit()

    # Сегодня: дело бота завершили — сверка отменяет запись.
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    # Через пару минут в карточке завели своё дело со сроком в будущем.
    manual_due = now + 2 * 3600
    bx = FakeTodoBitrix(
        [todo(16, dates.epoch_to_iso(manual_due), subject="Позвонить клиенту")]
    )
    await tasks.reconcile_deal_reminders(bx, db)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None  # отмена была только что — запись в окне
    assert pending["activity_id"] == 16
    assert pending["due_ts"] == manual_due


async def test_old_schema_reminders_migrate_and_stay_revivable(tmp_path):
    """База прежней схемы (без cancelled_at) мигрирует без потери кандидатов.

    На проде база живая: колонка добавляется ALTER TABLE, а старым
    отменённым строкам моментом отмены назначается created_at — недавно
    отменённые остаются кандидатами на воскрешение, как и до миграции.
    """
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """CREATE TABLE reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                text        TEXT NOT NULL,
                due_ts      INTEGER NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'deal',
                entity_id   INTEGER,
                activity_id INTEGER,
                status      TEXT NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        await conn.execute(
            "INSERT INTO reminders (chat_id, text, due_ts, kind, entity_id, "
            "activity_id, status, created_at) "
            "VALUES (1, ?, ?, 'deal', ?, 14, 'cancelled', "
            "datetime('now', '-1 day'))",
            (OLD_TEXT, OLD_DUE, DEAL),
        )
        await conn.commit()

    database = Database(path)
    await database.init()  # миграция живой базы

    rows = await database.cancelled_deal_reminders()
    assert [r["entity_id"] for r in rows] == [DEAL]


async def test_boundary_todo_does_not_block_revival(db, bot, session):
    """Дело на границе «сейчас» не блокирует воскрешение напоминания.

    Дело в пределах минутного допуска классифицируется ненаступившим и
    побеждает будущие. Отвергать его воскрешением — транзиентно хоронить
    напоминание при живых делах у сделки. Его минута настала: запись
    воскресает и уходит немедленно.
    """
    now = int(time.time())
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    boundary = now - 30  # в пределах допуска от «сейчас»
    bx = FakeTodoBitrix(
        [
            todo(16, dates.epoch_to_iso(boundary), subject="Минута настала"),
            todo(17, dates.epoch_to_iso(now + 3600)),
        ]
    )
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None  # воскрешение не заблокировано
    assert pending["activity_id"] == 16
    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=bx) == 1
    assert "заявка №78" in session.sent_texts[-1]


async def test_todo_exactly_at_tolerance_edge_still_revives(db):
    """Дело ровно на границе допуска (−60 секунд) ещё воскрешает запись.

    Граница воскрешения обязана совпадать с границей nearest_todo, иначе
    дело, которое сверка считает ненаступившим, блокировало бы воскрешение.
    Секунды в тесте жёсткие: они фиксируют сам порог.
    """
    now = int(time.time())
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1

    bx = FakeTodoBitrix([todo(16, dates.epoch_to_iso(now - 60))])
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == now - 60


async def test_cancelled_scan_does_not_starve_any_record(db):
    """Каждая отменённая запись рано или поздно попадает в проход воскрешения.

    Кандидатов в окне больше, чем LIMIT одного прохода: детерминированный
    порядок навсегда прятал бы «лишние» записи за верхушкой — они не
    воскресли бы никогда. Случайный порядок даёт каждой записи шанс на
    каждом проходе (вероятность пропуска за 200 проходов — исчезающая).
    """
    now = int(time.time())
    deal_ids = list(range(1001, 1026))  # 25 сделок > лимита прохода (20)
    for deal_id in deal_ids:
        rid = await db.add_reminder(
            1, f"заявка №{deal_id}", now + 3600, "deal", deal_id, 14
        )
        assert await db.cancel_reminder(rid)

    seen: set[int] = set()
    for _ in range(200):
        seen.update(r["entity_id"] for r in await db.cancelled_deal_reminders())
        if seen == set(deal_ids):
            break
    assert seen == set(deal_ids)


async def test_one_deal_tails_do_not_eat_scan_slots(db):
    """Хвосты отменённых записей одной сделки не вытесняют другие сделки.

    Отменённые записи не удаляются, и у одной сделки их копится много
    (правки срока, повторные отмены). От сделки в проход обязана идти одна
    запись — самая свежая, с актуальным текстом: иначе двадцать хвостов
    одной сделки съедали бы весь лимит прохода, а соседняя сделка не
    попадала бы в сверку никогда.
    """
    now = int(time.time())
    starved = await db.add_reminder(
        1, "заявка №200 — прочистить трубу", now + 3600, "deal", 200, 14
    )
    assert await db.cancel_reminder(starved)
    last_tail = None
    for attempt in range(25):
        last_tail = await db.add_reminder(
            1, f"заявка №78 — правка {attempt}", now + 3600, "deal", DEAL, 14
        )
        assert await db.cancel_reminder(last_tail)

    rows = await db.cancelled_deal_reminders()

    by_deal = {r["entity_id"]: r for r in rows}
    assert set(by_deal) == {DEAL, 200}  # обе сделки в одном проходе
    assert by_deal[DEAL]["id"] == last_tail  # от сделки — самая свежая запись
    assert len(rows) == 2


async def test_revival_does_not_duplicate_live_pending(db):
    """Сделка с живым ожидающим напоминанием второго из отменённых не получает.

    После отмены заказчик поставил новый срок через бота — в очереди снова
    есть ожидающая запись. Старая отменённая не должна воскресать рядом:
    два напоминания по одной сделке — дубль.
    """
    now = int(time.time())
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    new_due = now + 2 * 3600
    await db.add_reminder(
        1,
        f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(new_due)}",
        new_due,
        "deal",
        DEAL,
        500,
    )
    bx = FakeTodoBitrix([todo(500, dates.epoch_to_iso(new_due))])

    await tasks.reconcile_deal_reminders(bx, db)

    rows = [r for r in await db.pending_deal_reminders() if r["entity_id"] == DEAL]
    assert len(rows) == 1
    assert rows[0]["activity_id"] == 500


async def test_sent_reminder_blocks_revival_of_same_due(db, bot, session):
    """Отправленное напоминание не даёт воскресить отменённый хвост той же даты.

    Бот своё дело в CRM не завершает: после отправки дело остаётся открытым
    и в первую минуту после срока ещё классифицируется ненаступившим. Без
    гарда отменённый хвост той же сделки воскресал бы со сроком в прошлом,
    и по уже отработанной дате уходил второй «⏰» со старым текстом.
    Sent-строка того же срока — доказательство, что по нему отработали.
    """
    now = int(time.time())
    # Хвост: дело бота завершили в CRM, сверка отменила запись.
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    # Новый срок поставили через бота; напоминание ушло в свой момент.
    due = now
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(due)}"
    await db.add_reminder(1, text, due, "deal", DEAL, 16)
    bx = FakeTodoBitrix([todo(16, dates.epoch_to_iso(due))])
    assert await tasks.send_due_reminders(bot, db, now_ts=due + 5, bitrix=bx) == 1

    # Дело 16 так и открыто; сверка приходит в окне [срок, срок+60).
    await tasks.reconcile_deal_reminders(bx, db, now_ts=due + 30)

    assert await db.pending_deal_reminder(DEAL) is None  # хвост не воскрес
    assert await tasks.send_due_reminders(bot, db, now_ts=due + 45, bitrix=bx) == 0
    assert len(session.sent_texts) == 1  # «⏰» по этому сроку ровно один


async def test_old_sent_reminder_does_not_block_revival(db, bot, session):
    """Sent-строка с ДРУГОЙ датой легитимному воскрешению не мешает.

    Старое напоминание отработало давно; сегодня запись отменили и завели
    дело на новую дату. Гард сравнивает сроки: отправка по другой дате —
    не дубль, воскрешение обязано пройти.
    """
    now = int(time.time())
    old_due = now - 7200
    await db.add_reminder(1, OLD_TEXT, old_due, "deal", DEAL, 14)
    bx_old = FakeTodoBitrix([todo(14, dates.epoch_to_iso(old_due))])
    assert await tasks.send_due_reminders(bot, db, now_ts=old_due + 5, bitrix=bx_old) == 1

    # Новый срок поставили через бота, затем дело завершили — отмена.
    await db.add_reminder(1, OLD_TEXT, now + 3600, "deal", DEAL, 16)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    # Через пару минут в карточке завели дело на новую дату.
    new_due = now + 7200
    bx = FakeTodoBitrix([todo(17, dates.epoch_to_iso(new_due), subject="Перезвонить")])
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None  # воскрешение живо
    assert pending["activity_id"] == 17
    assert pending["due_ts"] == new_due


async def test_sent_guard_boundary_is_sync_tolerance(db, bot, session):
    """Граница sent-гарда — минутный допуск сверки, секунды жёсткие.

    Дело на 60 секунд позже отправленного срока — уже ДРУГАЯ дата (точность
    портала — минута), воскрешение живо; на 59 — та же, воскрешение
    режется. Порог зажат с обеих сторон: мутант не может ни расширить его
    (перестало бы воскресать легитимное), ни сузить (пролез бы дубль).
    """
    now = int(time.time())
    # Две сделки с одинаковой историей: отменённый хвост + напоминание,
    # отправленное в момент `now`. Разница только в сроке нового дела.
    for deal_id in (301, 302):
        await db.add_reminder(1, f"заявка №{deal_id}", now + 3600, "deal", deal_id, 14)
    assert await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db) == 2
    for deal_id, act in ((301, 16), (302, 26)):
        await db.add_reminder(1, f"заявка №{deal_id}", now, "deal", deal_id, act)
    bx_send = FakeTodoBitrix(
        [
            todo(16, dates.epoch_to_iso(now), deal_id=301),
            todo(26, dates.epoch_to_iso(now), deal_id=302),
        ]
    )
    assert await tasks.send_due_reminders(bot, db, now_ts=now + 5, bitrix=bx_send) == 2

    # Дела бота завершили; в карточках завели новые: +60с и +59с к сроку.
    bx = FakeTodoBitrix(
        [
            todo(17, dates.epoch_to_iso(now + 60), deal_id=301),
            todo(27, dates.epoch_to_iso(now + 59), deal_id=302),
        ]
    )
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now + 10)

    revived = await db.pending_deal_reminder(301)
    assert revived is not None  # ровно допуск — другая дата, воскресло
    assert revived["due_ts"] == now + 60
    assert await db.pending_deal_reminder(302) is None  # 59с — тот же срок


# ---------------------------------------------------------------------------
# Устойчивость планировщика
# ---------------------------------------------------------------------------


class HangingBitrix(SemanticBitrixFake):
    """«Портал», который повис: запрос не отвечает и не падает."""

    async def _dispatch(self, method: str, params: dict) -> Any:
        await asyncio.Event().wait()


async def test_hung_portal_does_not_stall_scheduler_pass(db, bot, session, monkeypatch):
    """Зависший портал не блокирует проход планировщика.

    Сверка перед отправкой ограничена дедлайном: без него один повисший
    crm.activity.list остановил бы все отправки прохода. По истечении
    дедлайна — fail-open, отправка по сохранённому сроку.
    """
    monkeypatch.setattr(tasks, "SYNC_CRM_DEADLINE", 0.05)
    now = int(time.time())
    await db.add_reminder(1, OLD_TEXT, now - 60, "deal", DEAL, 14)

    sent = await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=HangingBitrix())
    assert sent == 1
    assert "заявка №78" in session.sent_texts[-1]


async def test_reminder_loop_reconciles_at_startup(db, bot):
    """Первая сверка — сразу при старте цикла, а не через RECONCILE_INTERVAL.

    Правки, сделанные в CRM пока бот лежал, должны догоняться в первые же
    секунды после подъёма: до первого сна цикла очередь уже сверена.
    """
    now = int(time.time())
    far_due = now + 9 * 86400
    near_due = now + 2 * 3600
    text = f"заявка №78 — повесить люстру. Срок: {dates.format_epoch(far_due)}"
    await db.add_reminder(1, text, far_due, "deal", DEAL, 14)
    bx = FakeTodoBitrix([todo(14, dates.epoch_to_iso(near_due))])

    loop_task = asyncio.create_task(tasks.reminder_loop(bot, db, bitrix=bx))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = await db.pending_deal_reminder(DEAL)
            if pending is not None and pending["due_ts"] == near_due:
                break
            await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    assert bx.list_calls >= 1  # сверка прошла до первого сна цикла
    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == near_due


# ---------------------------------------------------------------------------
# Перевооружение после отправки: пинг и на само время заявки
# ---------------------------------------------------------------------------

# Кейс заказчика 22.07: заявка на 22:00 (дело бота), своё дело-напоминание
# на 21:31. Ранний пинг ушёл, а в момент самой заявки бот молчал: после
# отправки у сделки не оставалось pending-записи, и дело 22:00 никто не ждал.
DEAL_DUE = epoch("2026-07-22T22:00:00+10:00")
EARLY_DUE = epoch("2026-07-22T21:31:00+10:00")
DEAL_TODO = todo(14, "2026-07-22T22:00:00+10:00", subject="Заявка №78: электрика")
EARLY_TODO = todo(16, "2026-07-22T21:31:00+10:00", subject="Запланировано дело")
EARLY_TEXT = "заявка №78 — электрика. Срок: 22.07.2026 21:31"


async def test_ping_fires_at_deal_time_after_early_manual_reminder(db, bot, session):
    """Заявка в 22:00 + своё напоминание на 21:31 — бот шлёт ОБА пинга.

    После отправки раннего напоминания очередь обязана перевооружиться на
    следующее ненаступившее дело сделки — само время заявки.
    """
    await db.add_reminder(
        1, "заявка №78 — электрика. Срок: 22.07.2026 22:00", DEAL_DUE, "deal", DEAL, 14
    )
    bx = FakeTodoBitrix([DEAL_TODO, EARLY_TODO])

    # Сверка до раннего срока: напоминание переезжает на ручное дело 21:31.
    await tasks.reconcile_deal_reminders(bx, db, now_ts=EARLY_DUE - 600)
    assert await tasks.send_due_reminders(bot, db, now_ts=EARLY_DUE + 5, bitrix=bx) == 1
    assert "21:31" in session.sent_texts[-1]

    # Сразу после отправки очередь снова вооружена — на само время заявки.
    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == DEAL_DUE
    assert pending["activity_id"] == 14

    # В момент заявки уходит второй пинг, и срок в нём — её собственный.
    assert await tasks.send_due_reminders(bot, db, now_ts=DEAL_DUE + 5, bitrix=bx) == 1
    assert len(session.sent_texts) == 2
    assert "22.07.2026 22:00" in session.sent_texts[-1]


async def test_reconcile_arms_deal_time_when_crm_was_down_at_send(db, bot, session):
    """CRM лежала в момент раннего пинга — сверка после подъёма вооружает 22:00.

    Немедленное перевооружение при отправке требует живого портала; если его
    не было, следующее дело обязан подхватить периодический проход сверки.
    """
    await db.add_reminder(1, EARLY_TEXT, EARLY_DUE, "deal", DEAL, 16)
    assert await tasks.send_due_reminders(bot, db, now_ts=EARLY_DUE + 5, bitrix=None) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    bx = FakeTodoBitrix([DEAL_TODO, EARLY_TODO])
    await tasks.reconcile_deal_reminders(bx, db, now_ts=EARLY_DUE + 120)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == DEAL_DUE
    assert await tasks.send_due_reminders(bot, db, now_ts=DEAL_DUE + 5, bitrix=bx) == 1
    assert "22.07.2026 22:00" in session.sent_texts[-1]


async def test_sent_rearm_does_not_duplicate_single_todo(db, bot, session):
    """Единственное дело: после отправки перевооружение не плодит второй пинг.

    Бот своё дело в CRM не завершает — открытое дело с уже отработанным
    сроком не должно вооружать дубль ни в первую минуту, ни позже.
    """
    await db.add_reminder(1, OLD_TEXT, OLD_DUE, "deal", DEAL, 14)
    bx = FakeTodoBitrix([todo(14, "2026-07-21T10:00:00+10:00")])

    assert await tasks.send_due_reminders(bot, db, now_ts=OLD_DUE + 5, bitrix=bx) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    for now in (OLD_DUE + 30, OLD_DUE + 120):
        await tasks.reconcile_deal_reminders(bx, db, now_ts=now)
        assert await db.pending_deal_reminder(DEAL) is None
        assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=bx) == 0
    assert len(session.sent_texts) == 1


async def test_rearm_boundary_is_sync_tolerance(db, bot, session):
    """Граница «та же дата» у перевооружения — ровно допуск сверки.

    Дело на 59-й секунде от отправленного срока — та же дата (дубль зарезан),
    на 60-й — уже другая (пинг обязан встать). Секунды жёсткие, не через
    константу: раздутый мутантом допуск обязан уронить тест.
    """
    await db.add_reminder(1, EARLY_TEXT, EARLY_DUE, "deal", DEAL, 16)
    bx = FakeTodoBitrix([EARLY_TODO])
    assert await tasks.send_due_reminders(bot, db, now_ts=EARLY_DUE + 5, bitrix=bx) == 1

    bx59 = FakeTodoBitrix(
        [EARLY_TODO, todo(18, "2026-07-22T21:31:59+10:00", subject="Запланировано дело")]
    )
    await tasks.reconcile_deal_reminders(bx59, db, now_ts=EARLY_DUE + 10)
    assert await db.pending_deal_reminder(DEAL) is None

    bx60 = FakeTodoBitrix(
        [EARLY_TODO, todo(18, "2026-07-22T21:32:00+10:00", subject="Запланировано дело")]
    )
    await tasks.reconcile_deal_reminders(bx60, db, now_ts=EARLY_DUE + 10)
    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == EARLY_DUE + 60
    assert pending["activity_id"] == 18


async def test_card_open_resync_arms_next_todo_after_sent(db, bot, session):
    """Открытие карточки вооружает следующее дело и после отправленного пинга.

    Дела карточки уже прочитаны — ждать периодическую сверку не нужно.
    """
    await db.add_reminder(1, EARLY_TEXT, EARLY_DUE, "deal", DEAL, 16)
    assert await tasks.send_due_reminders(bot, db, now_ts=EARLY_DUE + 5, bitrix=None) == 1
    assert await db.pending_deal_reminder(DEAL) is None

    await tasks.resync_deal_reminder(
        db, DEAL, [DEAL_TODO, EARLY_TODO], now_ts=EARLY_DUE + 120
    )

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == DEAL_DUE
    assert pending["activity_id"] == 14


async def test_rearm_catches_todo_missed_by_scan_gap(db, bot, session):
    """Дело, наступившее между проходами скана, вооружается с опозданием.

    Сканы идут раз в RECONCILE_INTERVAL, и дело, чей срок пришёлся на зазор
    (или чью сделку случайный LIMIT отложил на проход), к следующему проходу
    уже «просрочено». Жёсткий отсев просроченных терял такой пинг навсегда —
    grace на пару интервалов шлёт его с опозданием: поздний пинг лучше
    потерянного.
    """
    now = int(time.time())
    early_due = now - 900
    missed_due = now - 180
    await db.add_reminder(
        1,
        f"заявка №{DEAL} — электрика. Срок: {dates.format_epoch(early_due)}",
        early_due,
        "deal",
        DEAL,
        16,
    )
    assert await tasks.send_due_reminders(bot, db, now_ts=early_due + 5, bitrix=None) == 1

    bx = FakeTodoBitrix(
        [
            todo(14, dates.epoch_to_iso(missed_due), subject="Заявка: электрика"),
            todo(16, dates.epoch_to_iso(early_due), subject="Запланировано дело"),
        ]
    )
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now)

    pending = await db.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == missed_due
    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=bx) == 1


async def test_rearm_skips_long_overdue_todo(db, bot, session):
    """Давно просроченное дело пинг не вооружает: задним числом хуже тишины.

    Граница grace — жёсткими секундами (661 > 2 интервала + допуск), чтобы
    раздутый мутантом интервал уронил тест, а не отмасштабировал границу.
    """
    now = int(time.time())
    early_due = now - 3 * 3600
    stale_due = now - 661
    await db.add_reminder(
        1,
        f"заявка №{DEAL} — электрика. Срок: {dates.format_epoch(early_due)}",
        early_due,
        "deal",
        DEAL,
        16,
    )
    assert await tasks.send_due_reminders(bot, db, now_ts=early_due + 5, bitrix=None) == 1

    bx = FakeTodoBitrix(
        [
            todo(14, dates.epoch_to_iso(stale_due), subject="Заявка: электрика"),
            todo(16, dates.epoch_to_iso(early_due), subject="Запланировано дело"),
        ]
    )
    await tasks.reconcile_deal_reminders(bx, db, now_ts=now)

    assert await db.pending_deal_reminder(DEAL) is None


async def test_sent_scan_rotates_without_starvation(db, bot, session):
    """Скан sent-строк — честная ротация: давно не проверявшиеся первыми.

    Случайный порядок без памяти давал перекрытия выборок: сделка, чей пинг
    только что ушёл, возвращалась в пул и могла вытеснять ещё не
    проверенные. Теперь порядок по rearm_checked_at — каждая запись
    гарантированно сканируется не реже, чем раз в ceil(N/limit) проходов.
    """
    now = int(time.time())
    for deal_id in (78, 79, 80):
        await db.add_reminder(
            1, f"заявка №{deal_id}. Срок: скоро", now - 300, "deal", deal_id, deal_id * 10
        )
    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=None) == 3

    seen = []
    for _ in range(3):
        rows = await db.sent_deal_reminders(limit=1)
        assert len(rows) == 1
        seen.append(rows[0]["entity_id"])
        await db.mark_rearm_checked(rows[0]["id"])

    assert sorted(seen) == [78, 79, 80]  # три прохода покрывают все три сделки

    # Четвёртый проход возвращается к началу ротации, ничего не голодает.
    rows = await db.sent_deal_reminders(limit=1)
    assert rows[0]["entity_id"] == seen[0]


async def test_reconcile_marks_scanned_sent_rows(db, bot, session):
    """Проход сверки отмечает просканированные sent-строки для ротации.

    Сбой чтения дел отметку НЕ ставит: такая сделка не теряет приоритет и
    перечитывается следующим проходом (fail-open).
    """
    now = int(time.time())
    await db.add_reminder(1, "заявка №78. Срок: скоро", now - 300, "deal", DEAL, 14)
    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=None) == 1

    await tasks.reconcile_deal_reminders(FakeTodoBitrix([]), db, now_ts=now)
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT rearm_checked_at FROM reminders WHERE status = 'sent'"
        )
        checked = [row[0] for row in await cur.fetchall()]
    assert checked and all(checked)

    # Второй sent — портал недоступен: отметка не ставится, приоритет цел.
    await db.add_reminder(1, "заявка №79. Срок: скоро", now - 300, "deal", 79, 21)
    assert await tasks.send_due_reminders(bot, db, now_ts=now, bitrix=None) == 1
    await tasks.reconcile_deal_reminders(FakeTodoBitrix(fail=True), db, now_ts=now)
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT rearm_checked_at FROM reminders "
            "WHERE status = 'sent' AND entity_id = 79"
        )
        row = await cur.fetchone()
    assert row is not None and row[0] is None


async def test_sent_scan_window_limits_rearm_cost(db, bot, session):
    """Давно отправленные записи не сканируются: окно перевооружения конечно.

    Каждая строка скана стоит одного crm.activity.list на проход — сделки,
    отправленные раньше окна, из кандидатов выпадают.
    """
    await db.add_reminder(1, EARLY_TEXT, EARLY_DUE, "deal", DEAL, 16)
    assert await tasks.send_due_reminders(bot, db, now_ts=EARLY_DUE + 5, bitrix=None) == 1
    rows = await db.sent_deal_reminders()
    assert [row["entity_id"] for row in rows] == [DEAL]

    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE reminders SET sent_at = datetime('now', '-259200 seconds') "
            "WHERE status = 'sent'"
        )
        await conn.commit()
    assert await db.sent_deal_reminders() == []


async def test_old_schema_sent_rows_migrate_and_rearm(tmp_path, bot, session):
    """База старой схемы (без sent_at) мигрирует, и её sent-строки сканируются.

    Момент отправки старых строк неизвестен, и любая оценка (created_at —
    запись могла быть создана задолго до срока; due_ts — планировщик после
    простоя шлёт и с опозданием в дни) кого-нибудь теряет. Поэтому бэкфил —
    моментом миграции: разово в окно попадает вся история sent-строк, зато
    ни одна недавно отправленная не выпадает из перевооружения. Цена — по
    одному crm.activity.list на сделку в проход, пока окно не остынет.
    """
    now = int(time.time())
    early_due = now - 600
    deal_due = now + 1200
    stale_due = now - 3 * 86400
    early_text = f"заявка №{DEAL} — электрика. Срок: {dates.format_epoch(early_due)}"
    path = str(tmp_path / "old-sent.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
            "text TEXT NOT NULL, due_ts INTEGER NOT NULL, "
            "kind TEXT NOT NULL DEFAULT 'deal', entity_id INTEGER, "
            "activity_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "cancelled_at TEXT DEFAULT NULL)"
        )
        # Запись создана давно (месяц назад): бэкфил по created_at терял бы её.
        await conn.execute(
            "INSERT INTO reminders (chat_id, text, due_ts, kind, entity_id, "
            "activity_id, status, created_at) "
            "VALUES (1, ?, ?, 'deal', ?, 16, 'sent', datetime('now', '-30 days'))",
            (early_text, early_due, DEAL),
        )
        # Срок трёхдневной давности, отправлена перед миграцией (простой бота):
        # бэкфил по due_ts терял бы её — а она тоже обязана попасть в скан.
        await conn.execute(
            "INSERT INTO reminders (chat_id, text, due_ts, kind, entity_id, "
            "activity_id, status) VALUES (1, 'поздний пинг', ?, 'deal', 79, 21, 'sent')",
            (stale_due,),
        )
        await conn.commit()

    database = Database(path)
    await database.init()

    rows = await database.sent_deal_reminders(limit=50)
    assert sorted(row["entity_id"] for row in rows) == [DEAL, 79]
    bx = FakeTodoBitrix(
        [
            todo(14, dates.epoch_to_iso(deal_due), subject="Заявка: электрика"),
            todo(16, dates.epoch_to_iso(early_due), subject="Запланировано дело"),
        ]
    )
    await tasks.reconcile_deal_reminders(bx, database, now_ts=now)
    pending = await database.pending_deal_reminder(DEAL)
    assert pending is not None
    assert pending["due_ts"] == deal_due


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
