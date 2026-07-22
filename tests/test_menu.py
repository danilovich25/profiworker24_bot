"""Главное меню: /start с клавиатурой, кнопки «Новая заявка/Найти/Последние».

Кнопки reply-клавиатуры приходят обычным текстом, поэтому их хендлеры стоят
до разбора свободного текста: нажатие кнопки не должно уходить в модель.
"""

from types import SimpleNamespace

import pytest
from aiogram.methods import SetMyCommands

from app.db import Database
from app.handlers import routers
from app.handlers.search import (
    ACTIVE_ORDER_WARNING,
    ASK_QUERY,
    MENU_REFRESHED,
    SearchFlow,
)
from app.handlers.start import (
    BTN_FIND,
    BTN_LAST,
    BTN_NEW,
    LEGACY_BTN_FIND,
    LEGACY_BTN_LAST,
    LEGACY_BTN_NEW,
    NEW_ORDER_HINT,
)
from app.main import create_dispatcher, setup_bot_commands
from app.services import llm
from tests.conftest import make_callback_update, make_message_update
from tests.test_handlers_messages import FULL_ORDER
from tests.test_search import FakeSearchBitrix


@pytest.fixture(autouse=True)
def _detach_routers():
    """Роутеры - модульные синглтоны; после теста отвязываем их от диспетчера."""
    yield
    for r in routers:
        r._parent_router = None


@pytest.fixture
async def flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "menu.db"))
    await db.init()
    bx = FakeSearchBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


async def send(flow, text: str, user_id: int = 1, **extra) -> None:
    await flow.dp.feed_update(
        flow.bot, make_message_update(flow.bot, text, user_id=user_id, **extra)
    )


async def test_start_shows_menu_keyboard(flow):
    await send(flow, "/start")
    msg = flow.session.sent_messages[-1]
    assert "заявк" in msg.text.lower()
    buttons = [b.text for row in msg.reply_markup.keyboard for b in row]
    from app.handlers.start import BTN_MY_REMINDERS, BTN_REMIND

    assert buttons == [BTN_NEW, BTN_FIND, BTN_LAST, BTN_REMIND, BTN_MY_REMINDERS]


async def test_new_order_button_hints(flow):
    await send(flow, BTN_NEW)
    assert flow.session.sent_texts[-1] == NEW_ORDER_HINT


async def test_find_button_starts_search(flow):
    await send(flow, BTN_FIND)
    assert flow.session.sent_texts[-1] == ASK_QUERY

    await send(flow, "154")  # запрос после кнопки работает как после /find
    assert "№154" in flow.session.sent_texts[-1]
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


async def test_last_button_lists_recent(flow):
    await send(flow, BTN_LAST)
    reply = flow.session.sent_texts[-1]
    assert "№155" in reply and "№154" in reply
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


async def test_menu_buttons_not_parsed_as_orders(flow, monkeypatch):
    calls = {"count": 0}

    async def counting_parse(text):
        calls["count"] += 1
        return None

    monkeypatch.setattr(llm, "parse_order", counting_parse)

    await send(flow, BTN_NEW)
    await send(flow, BTN_LAST)
    assert calls["count"] == 0  # кнопки меню в модель не уходят


async def test_find_button_in_ask_phone_keeps_order(flow, monkeypatch):
    """«Найти» на вопросе о телефоне не уничтожает незавершённую заявку.

    Раньше кнопка включала поиск: заявка в FSM стиралась, а её content-claim
    оставался — повтор исходного текста отвечал «дубль» вместо продолжения.
    Теперь кнопка в состояниях незавершённой заявки не перехватывается, и
    номер по-прежнему завершает начатую заявку.
    """
    orders = {
        "иван, сантехника, замена крана": FULL_ORDER.model_copy(update={"phone": None}),
    }

    async def fake(text: str):
        return orders.get(text.lower())

    monkeypatch.setattr(llm, "parse_order", fake)

    await send(flow, "Иван, сантехника, замена крана")
    assert "Не указан телефон" in flow.session.sent_texts[-1]

    await send(flow, BTN_FIND)  # кнопка меню посреди вопроса о номере
    assert ASK_QUERY not in flow.session.sent_texts  # поиск не включился

    await send(flow, "89141234567")  # заявка жива: номер завершает её
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text
    assert "+79141234567" in card.text


async def test_find_button_in_ask_category_keeps_order(flow, monkeypatch):
    """«Найти» на вопросе о категории не стирает заявку: выбор кнопкой работает."""
    orders = {
        "иван, 89141234567, замена крана": FULL_ORDER.model_copy(update={"category": None}),
    }

    async def fake(text: str):
        return orders.get(text.lower())

    monkeypatch.setattr(llm, "parse_order", fake)

    await send(flow, "Иван, 89141234567, замена крана")
    assert "категорию" in flow.session.sent_texts[-1]

    await send(flow, BTN_FIND)
    assert ASK_QUERY not in flow.session.sent_texts

    await flow.dp.feed_update(
        flow.bot, make_callback_update(flow.bot, "cat:сантехника")
    )
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text


@pytest.mark.parametrize("command", ["/start", "/help", "/new"])
async def test_navigation_commands_close_search_flow(flow, monkeypatch, command):
    """/start, /help и /new закрывают ожидание поискового запроса.

    Раньше SearchFlow переживал приветствие: пользователь следовал подсказке,
    присылал заявку — и текст поглощался как поисковый запрос без LLM-разбора.
    """
    calls = {"count": 0}

    async def counting(text: str):
        calls["count"] += 1
        return None

    monkeypatch.setattr(llm, "parse_order", counting)

    await send(flow, "/find")
    assert flow.session.sent_texts[-1] == ASK_QUERY

    await send(flow, command)
    await send(flow, "Иван, сантехника, замена крана")

    assert calls["count"] == 1  # текст ушёл в разбор заявки, а не в поиск
    assert "Пришлите текст заявки" in flow.session.sent_texts[-1]


async def test_start_keeps_unfinished_form(flow, monkeypatch):
    """/start не сбрасывает незаконченный опросник (полный сброс — это /new)."""

    async def unavailable(text: str):
        raise llm.LLMUnavailable("недоступна")

    monkeypatch.setattr(llm, "parse_order", unavailable)

    await send(flow, "Иван, замена крана")
    assert "Вопрос 1 из 6" in flow.session.sent_texts[-1]

    await send(flow, "/start")  # приветствие посреди опросника
    await send(flow, "Иван")  # ответ на вопрос 1 по-прежнему принимается
    assert "Вопрос 2 из 6" in flow.session.sent_texts[-1]


async def test_menu_button_inside_form_preserves_current_question(flow, monkeypatch):
    """Reply-кнопка не запускает поиск и не записывается ответом опросника."""

    async def unavailable(text: str):
        raise llm.LLMUnavailable("недоступна")

    monkeypatch.setattr(llm, "parse_order", unavailable)

    await send(flow, "Иван, замена крана")  # модель недоступна -> опросник
    assert "Вопрос 1 из 6" in flow.session.sent_texts[-1]

    await send(flow, BTN_FIND)
    assert flow.session.sent_texts[-1] == ACTIVE_ORDER_WARNING
    assert ASK_QUERY not in flow.session.sent_texts

    await send(flow, "Иван")  # прежний вопрос всё ещё активен
    assert "Вопрос 2 из 6" in flow.session.sent_texts[-1]

    await send(flow, "нет")
    assert "Вопрос 3 из 6" in flow.session.sent_texts[-1]


async def test_setup_bot_commands(bot, session):
    await setup_bot_commands(bot)
    request = [r for r in session.requests if isinstance(r, SetMyCommands)][-1]
    names = [c.command for c in request.commands]
    assert names == ["new", "find", "last", "remind", "reminders", "help"]


# ---------------------------------------------------------------------------
# Кнопки клавиатуры СТАРОГО бота (legacy/telebot-mvp): у сотрудника, не
# нажимавшего /start после обновления, в чате остались прежние кнопки —
# они обязаны работать, а не уходить в разбор заявки с подсказкой
# «нажмите „Найти“» (жалоба заказчика 21.07: «вылазит кнопка Найти»).
# ---------------------------------------------------------------------------


async def test_legacy_find_button_opens_search_immediately(flow, monkeypatch):
    calls = {"count": 0}

    async def counting_parse(text):
        calls["count"] += 1
        return None

    monkeypatch.setattr(llm, "parse_order", counting_parse)

    await send(flow, LEGACY_BTN_FIND)

    assert calls["count"] == 0  # старая кнопка не уходит в модель
    assert flow.session.sent_texts[-1] == ASK_QUERY  # сразу поле запроса
    refresh = flow.session.sent_messages[-2]
    assert refresh.text == MENU_REFRESHED  # устаревшая клавиатура заменена
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state

    await send(flow, "154")  # запрос после старой кнопки работает как поиск
    assert "№154" in flow.session.sent_texts[-1]


async def test_legacy_last_button_lists_recent(flow):
    await send(flow, LEGACY_BTN_LAST)
    reply = flow.session.sent_texts[-1]
    assert "№155" in reply and "№154" in reply
    assert MENU_REFRESHED in flow.session.sent_texts
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


async def test_legacy_new_button_hints_and_refreshes_keyboard(flow):
    await send(flow, LEGACY_BTN_NEW)
    msg = flow.session.sent_messages[-1]
    assert msg.text == NEW_ORDER_HINT
    buttons = [b.text for row in msg.reply_markup.keyboard for b in row]
    from app.handlers.start import BTN_MY_REMINDERS, BTN_REMIND

    assert buttons == [BTN_NEW, BTN_FIND, BTN_LAST, BTN_REMIND, BTN_MY_REMINDERS]


async def test_legacy_buttons_do_not_break_active_order(flow, monkeypatch):
    """Старые кнопки посреди опросника не стирают заявку, как и новые."""

    async def unavailable(text: str):
        raise llm.LLMUnavailable("недоступна")

    monkeypatch.setattr(llm, "parse_order", unavailable)

    await send(flow, "Иван, замена крана")
    assert "Вопрос 1 из 6" in flow.session.sent_texts[-1]

    await send(flow, LEGACY_BTN_FIND)
    assert flow.session.sent_texts[-1] == ACTIVE_ORDER_WARNING
    assert ASK_QUERY not in flow.session.sent_texts

    await send(flow, "Иван")  # прежний вопрос всё ещё активен
    assert "Вопрос 2 из 6" in flow.session.sent_texts[-1]
