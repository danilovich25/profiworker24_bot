"""Правка найденной заявки: карточка, накопление правок, одно сохранение.

Ключевой инвариант: обновляется ТА ЖЕ сделка (crm.deal.update по тому же ID)
и тот же контакт — фейк портала вообще не умеет crm.deal.add, любой путь,
случайно создающий новую сделку, уронит тест AssertionError'ом.
"""

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.handlers import edit as edit_handlers
from app.handlers import routers
from app.main import create_dispatcher
from app.services import dates, llm
from app.services.bitrix import UF_EXPENSE, UF_PROFIT, UF_SERVICE_CATEGORY
from tests.conftest import make_callback_update, make_message_update
from tests.test_search import FakeSearchBitrix

VVO = ZoneInfo("Asia/Vladivostok")

# «Сейчас» заморожено: 19.07.2026 15:00 Владивостока.
FROZEN_NOW = datetime(2026, 7, 19, 15, 0, tzinfo=VVO)

FAR_FUTURE_TS = 4102444800


@pytest.fixture(autouse=True)
def _detach_routers():
    yield
    for r in routers:
        r._parent_router = None


@pytest.fixture
async def flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "edit.db"))
    await db.init()
    bx = FakeSearchBitrix()
    # Дополняем первую сделку полями раунда 2: источник, категория, деньги.
    bx.deals[0].update(
        {
            "SOURCE_ID": "AVITO",
            UF_SERVICE_CATEGORY: "сантехника",
            "OPPORTUNITY": "5000",
            UF_EXPENSE: "2000",
            UF_PROFIT: "3000",
        }
    )
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


async def send(flow, text: str) -> None:
    await flow.dp.feed_update(flow.bot, make_message_update(flow.bot, text))


async def press(flow, data: str) -> None:
    await flow.dp.feed_update(flow.bot, make_callback_update(flow.bot, data))


def freeze_now(monkeypatch):
    monkeypatch.setattr(dates, "now_local", lambda: FROZEN_NOW)


# ---------------------------------------------------------------------------
# Карточка заявки
# ---------------------------------------------------------------------------


async def test_open_deal_shows_card(flow):
    await press(flow, "deal:open:154")

    card = flow.session.sent_messages[-1]
    assert "Заявка №154" in card.text
    assert "Клиент: Иван Петров" in card.text
    assert "Телефон: +79141234567" in card.text
    assert "Категория: сантехника" in card.text
    assert "Источник: Авито" in card.text
    assert "Описание: замена крана" in card.text
    assert "Стадия: Новая заявка" in card.text
    assert "Доход: 5000 руб." in card.text
    assert "Прибыль: 3000 руб." in card.text
    assert "Создана: 18.07.2026 10:00" in card.text
    callbacks = [
        b.callback_data for row in card.reply_markup.inline_keyboard for b in row
    ]
    assert "deal:edit:154" in callbacks


async def test_open_missing_deal_says_gone(flow):
    await press(flow, "deal:open:999")

    assert "не найдена" in flow.session.sent_texts[-1]


# ---------------------------------------------------------------------------
# Правка полей: та же сделка, тот же номер
# ---------------------------------------------------------------------------


async def test_edit_income_recomputes_profit_same_deal(flow):
    await press(flow, "deal:edit:154")
    fields_msg = flow.session.sent_messages[-1]
    callbacks = [
        b.callback_data for row in fields_msg.reply_markup.inline_keyboard for b in row
    ]
    # контакт есть — доступны и имя с телефоном
    for wanted in ("dedit:f:name", "dedit:f:phone", "dedit:f:income", "dedit:save"):
        assert wanted in callbacks

    await press(flow, "dedit:f:income")
    await send(flow, "7000")
    assert "Доход → 7000" in flow.session.sent_texts[-1]

    await press(flow, "dedit:save")

    update = flow.bx.deal_updates[0]
    assert update["id"] == 154
    assert update["fields"]["OPPORTUNITY"] == 7000
    # прибыль пересчитана от текущего расхода 2000
    assert update["fields"][UF_PROFIT] == 5000
    assert any("Заявка №154 обновлена" in t for t in flow.session.sent_texts)
    # свежая карточка показана с новым доходом
    assert any("Доход: 7000 руб." in t for t in flow.session.sent_texts)


async def test_edit_category_and_problem_rebuild_title(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:category")
    await press(flow, "cat:электрика")
    await press(flow, "dedit:f:problem")
    await send(flow, "заменить розетку")
    await press(flow, "dedit:save")

    fields = flow.bx.deal_updates[0]["fields"]
    assert fields["TITLE"] == "электрика: заменить розетку"
    assert fields[UF_SERVICE_CATEGORY] == "электрика"
    assert flow.bx.deals[0]["TITLE"] == "электрика: заменить розетку"


async def test_edit_source_updates_native_field(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:source")
    await press(flow, "src:Сарафанное радио")
    await press(flow, "dedit:save")

    assert flow.bx.deal_updates[0]["fields"]["SOURCE_ID"] == "SARAFAN"


async def test_edit_name_and_phone_update_same_contact(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:name")
    await send(flow, "Пётр")
    await press(flow, "dedit:f:phone")
    await send(flow, "не номер")
    assert "Не похоже на номер" in flow.session.sent_texts[-1]
    await send(flow, "8 914 000 11 22")
    await press(flow, "dedit:save")

    update = flow.bx.contact_updates[0]
    assert update["id"] == 15
    assert update["fields"]["NAME"] == "Пётр"
    # заменяется существующий номер (по ID значения), а не добавляется второй
    assert update["fields"]["PHONE"] == [{"ID": "501", "VALUE": "+79140001122"}]


async def test_edit_deadline_moves_reminders(flow, monkeypatch):
    """Смена срока переносит Telegram-напоминание и дело CRM той же заявки."""
    freeze_now(monkeypatch)
    old_due = int(datetime(2026, 7, 21, 10, 0, tzinfo=VVO).timestamp())
    await flow.db.add_reminder(1, "заявка №154 — старый срок", old_due, "deal", 154, 500)

    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:deadline")
    await send(flow, "24.07.2026 в 10:00")
    await press(flow, "dedit:save")

    # комментарий сделки получил новый срок в читаемом формате
    assert "Срок: 24.07.2026 10:00" in flow.bx.deal_updates[0]["fields"]["COMMENTS"]
    # дело CRM перенесено по сохранённому activity_id, новое не создано
    assert flow.bx.activities == []
    moved = flow.bx.activity_updates[0]
    assert moved["id"] == 500
    assert moved["ownerId"] == 154
    assert moved["deadline"] == "2026-07-24T10:00:00+10:00"
    # живой портал перезаписывает незаданные поля: без title в update
    # у дела слетал заголовок (найдено живым прогоном 21.07)
    assert moved["title"] == "Заявка №154: замена крана"
    # в очереди ровно одно (новое) напоминание с новым сроком
    rows = await flow.db.due_reminders(FAR_FUTURE_TS)
    assert len(rows) == 1
    assert rows[0]["due_ts"] == int(datetime(2026, 7, 24, 10, 0, tzinfo=VVO).timestamp())
    assert "24.07.2026 10:00" in rows[0]["text"]


async def test_edit_deadline_hint_format_keeps_time(flow, monkeypatch):
    """Формат из подсказки бота «24.07.2026 10:00» не теряет время.

    Дело CRM и Telegram-напоминание уходят на 10:00 Владивостока, а не на
    утренний дефолт для «даты без времени».
    """
    freeze_now(monkeypatch)

    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:deadline")
    await send(flow, "24.07.2026 10:00")
    await press(flow, "dedit:save")

    assert "Срок: 24.07.2026 10:00" in flow.bx.deal_updates[0]["fields"]["COMMENTS"]
    # напоминания раньше не было — создано новое дело с точным временем
    todo = flow.bx.activities[0]
    assert todo["ownerId"] == 154
    assert todo["deadline"] == "2026-07-24T10:00:00+10:00"
    rows = await flow.db.due_reminders(FAR_FUTURE_TS)
    assert len(rows) == 1
    assert rows[0]["due_ts"] == int(datetime(2026, 7, 24, 10, 0, tzinfo=VVO).timestamp())
    assert "24.07.2026 10:00" in rows[0]["text"]


async def test_edit_cancel_discards_changes(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:income")
    await send(flow, "9999")
    await press(flow, "dedit:cancel")

    assert edit_handlers.EDIT_CANCELLED in flow.session.sent_texts[-1]
    assert flow.bx.deal_updates == []

    # состояние свободно: сохранить уже нечего
    await press(flow, "dedit:save")
    assert flow.bx.deal_updates == []


async def test_cancel_command_cancels_edit(flow):
    await press(flow, "deal:edit:154")
    await send(flow, "/cancel")

    assert edit_handlers.EDIT_CANCELLED in flow.session.sent_texts[-1]


async def test_save_failure_keeps_changes_for_retry(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:income")
    await send(flow, "7000")

    flow.bx.fail_methods.add("crm.deal.update")
    await press(flow, "dedit:save")
    assert edit_handlers.EDIT_SAVE_FAILED in flow.session.sent_texts[-1]
    assert flow.bx.deal_updates == []

    flow.bx.fail_methods.clear()
    await press(flow, "dedit:save")  # повторное сохранение — те же правки
    assert flow.bx.deal_updates[0]["fields"]["OPPORTUNITY"] == 7000
    assert any("Заявка №154 обновлена" in t for t in flow.session.sent_texts)


async def test_deal_edit_does_not_break_active_order_input(flow, monkeypatch):
    """Кнопка «Изменить» не затирает незаконченный ввод заявки.

    Иначе контент-хэш начатой заявки залипал бы на сутки, а ответы уезжали
    бы не туда.
    """

    async def unavailable(text: str):
        raise llm.LLMUnavailable("недоступна")

    monkeypatch.setattr(llm, "parse_order", unavailable)
    await send(flow, "новая заявка от Иванова")
    assert "Как зовут клиента" in flow.session.sent_texts[-1]

    await press(flow, "deal:edit:154")
    assert flow.session.sent_texts[-1] == edit_handlers.ACTIVE_INPUT_WARNING

    # опросник живой: следующий текст — по-прежнему ответ на вопрос 1
    await send(flow, "Иван")
    assert "Вопрос 2 из 6" in flow.session.sent_texts[-1]


async def test_search_is_blocked_during_edit(flow):
    await press(flow, "deal:edit:154")

    await send(flow, "Найти")
    assert flow.session.sent_texts[-1] == edit_handlers.EDIT_IN_PROGRESS
    await send(flow, "/find")
    assert flow.session.sent_texts[-1] == edit_handlers.EDIT_IN_PROGRESS


async def test_save_without_changes_hints(flow):
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:save")

    assert flow.session.sent_texts[-1] == edit_handlers.EDIT_NO_CHANGES
    assert flow.bx.deal_updates == []


async def test_stale_edit_buttons_do_not_crash(flow):
    """Кнопки правки после рестарта (состояния нет) отвечают подсказкой."""
    await press(flow, "dedit:f:income")
    await press(flow, "dedit:save")

    assert flow.bx.deal_updates == []


# ---------------------------------------------------------------------------
# Синхронизация CRM → бот: карточка и очередь напоминаний при открытии
# ---------------------------------------------------------------------------


def deal_todo_row(todo_id: int, deal_id: int, deadline: str) -> dict:
    """Дело сделки в форме ответа crm.activity.list."""
    return {
        "ID": str(todo_id),
        "OWNER_ID": str(deal_id),
        "OWNER_TYPE_ID": 2,
        "SUBJECT": f"Заявка №{deal_id}: замена крана",
        "DEADLINE": deadline,
        "COMPLETED": "N",
        "PROVIDER_TYPE_ID": "TODO",
    }


async def test_card_shows_fresh_deadline_from_crm_todo(flow):
    """Срок в карточке — из ДЕЛА сделки, а не из копии в комментарии.

    Заказчик перенёс «назначенную дату» в Bitrix24: комментарий сделки
    остался со старым сроком, но карточка обязана показывать новый.
    """
    flow.bx.deals[0]["COMMENTS"] += "\nСрок: 25.07.2026 10:00"
    # 26.07 05:00 в зоне портала (+03) = 26.07 12:00 во Владивостоке.
    flow.bx.deal_todos.append(deal_todo_row(500, 154, "2026-07-26T05:00:00+03:00"))

    await press(flow, "deal:open:154")

    card = flow.session.sent_messages[-1]
    assert "Срок: 26.07.2026 12:00" in card.text
    assert "25.07.2026" not in card.text  # устаревшая копия не показана


async def test_card_falls_back_to_comments_without_todos(flow):
    """Дел у сделки нет — срок берётся из строки комментария, как раньше."""
    flow.bx.deals[0]["COMMENTS"] += "\nСрок: 25.07.2026 10:00"

    await press(flow, "deal:open:154")

    assert "Срок: 25.07.2026 10:00" in flow.session.sent_messages[-1].text


async def test_open_deal_resyncs_reminder_queue(flow):
    """Открытие карточки сразу догоняет правку срока, сделанную в CRM."""
    old_due = int(datetime(2026, 7, 25, 10, 0, tzinfo=VVO).timestamp())
    await flow.db.add_reminder(
        1, "заявка №154 — замена крана. Срок: 25.07.2026 10:00", old_due, "deal", 154, 500
    )
    moved = "2026-07-26T05:00:00+03:00"  # 26.07 12:00 Владивостока
    flow.bx.deal_todos.append(deal_todo_row(500, 154, moved))

    await press(flow, "deal:open:154")

    pending = await flow.db.pending_deal_reminder(154)
    assert pending is not None
    assert pending["due_ts"] == int(datetime.fromisoformat(moved).timestamp())
    assert "Срок: 26.07.2026 12:00" in pending["text"]


async def test_edit_deadline_adopts_existing_crm_todo(flow, monkeypatch):
    """Правка срока без записи в очереди переносит СУЩЕСТВУЮЩЕЕ дело CRM.

    Напоминание могло уже уйти (записи pending нет), а дело в карточке
    осталось: смена срока не должна плодить второе дело рядом с ним.
    """
    freeze_now(monkeypatch)
    flow.bx.deal_todos.append(deal_todo_row(600, 154, "2026-07-22T03:00:00+03:00"))

    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:deadline")
    await send(flow, "24.07.2026 10:00")
    await press(flow, "dedit:save")

    assert flow.bx.activities == []  # нового дела нет
    moved = flow.bx.activity_updates[0]
    assert moved["id"] == 600
    assert moved["deadline"] == "2026-07-24T10:00:00+10:00"


async def test_changes_summary_formats_money_without_float_tail(flow):
    """Сводка правок показывает «Доход → 7777», а не «7777.0»."""
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:income")
    await send(flow, "7777")

    summary = flow.session.sent_texts[-1]
    assert "Доход → 7777" in summary
    assert "7777.0" not in summary


async def test_changes_summary_formats_millions_plainly(flow):
    """Сводка правок: «Доход → 1500000», а не экспонента «1.5e+06»."""
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:income")
    await send(flow, "1500000")

    summary = flow.session.sent_texts[-1]
    assert "Доход → 1500000" in summary
    assert "e+06" not in summary


async def test_card_money_formats_millions_plainly(flow):
    """Суммы от миллиона в карточке — числом, а не «2.5e+06 руб.»."""
    flow.bx.deals[0]["OPPORTUNITY"] = "2500000"

    await press(flow, "deal:open:154")

    card = flow.session.sent_messages[-1].text
    assert "Доход: 2500000 руб." in card
    assert "e+06" not in card


async def test_open_deal_with_no_todos_cancels_stale_reminder(flow):
    """Открыл карточку, а дел у сделки ноль — напоминание отменяется сразу.

    Дело завершили или удалили в CRM: сверка при открытии карточки видит
    пустой (но успешно прочитанный) список дел и снимает напоминание, не
    дожидаясь периодического прохода.
    """
    await flow.db.add_reminder(
        1,
        "заявка №154 — замена крана. Срок: 25.07.2026 10:00",
        FAR_FUTURE_TS,
        "deal",
        154,
        500,
    )

    await press(flow, "deal:open:154")

    assert await flow.db.pending_deal_reminder(154) is None


async def test_card_deadline_prefers_upcoming_todo(flow, monkeypatch):
    """Срок в карточке — ненаступившее дело, а не просроченный хвост."""
    freeze_now(monkeypatch)
    # 18.07 05:00 (+03) = 18.07 12:00 ВВО — просрочено к FROZEN_NOW (19.07 15:00).
    flow.bx.deal_todos.append(deal_todo_row(700, 154, "2026-07-18T05:00:00+03:00"))
    flow.bx.deal_todos.append(deal_todo_row(701, 154, "2026-07-26T05:00:00+03:00"))

    await press(flow, "deal:open:154")

    card = flow.session.sent_messages[-1]
    assert "Срок: 26.07.2026 12:00" in card.text


async def test_open_card_revives_cancelled_reminder(flow):
    """Открытие карточки возвращает отменённое напоминание за новым делом.

    Сверка успела отменить напоминание по пустому списку дел, потом в CRM
    завели дело со сроком в будущем: сотрудник открыл карточку — очередь
    догоняет сразу, не дожидаясь периодического прохода.
    """
    rid = await flow.db.add_reminder(
        1,
        "заявка №154 — замена крана. Срок: 25.07.2026 10:00",
        FAR_FUTURE_TS,
        "deal",
        154,
        500,
    )
    assert await flow.db.cancel_reminder(rid)
    manual_due = int(time.time()) + 2 * 3600
    flow.bx.deal_todos.append(deal_todo_row(600, 154, dates.epoch_to_iso(manual_due)))

    await press(flow, "deal:open:154")

    pending = await flow.db.pending_deal_reminder(154)
    assert pending is not None
    assert pending["activity_id"] == 600
    assert pending["due_ts"] == manual_due


async def test_new_request_button_does_not_wipe_unsaved_edits(flow):
    """«Новая заявка» (включая легаси-кнопку и /new) не стирает правки молча.

    Кнопка старого бота посреди правки сбрасывала накопленные изменения без
    предупреждения. При незаконченной правке бот просит сначала сохранить
    или отменить — правки живы, «Сохранить» пишет их в ту же сделку.
    """
    await press(flow, "deal:edit:154")
    await press(flow, "dedit:f:income")
    await send(flow, "7777")

    for btn in ("Новая заявка", "🆕 Новая заявка", "/new"):
        await send(flow, btn)
        assert "сохраните или отмените" in flow.session.sent_texts[-1].lower()

    await press(flow, "dedit:save")
    assert flow.bx.deal_updates[-1]["fields"]["OPPORTUNITY"] == 7777


async def test_new_button_without_changes_resets_edit(flow):
    """Без накопленных правок «Новая заявка» работает как раньше — сброс."""
    await press(flow, "deal:edit:154")

    await send(flow, "🆕 Новая заявка")

    assert "Пришлите заявку" in flow.session.sent_texts[-1]
