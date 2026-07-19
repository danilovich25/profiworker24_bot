"""Whitelist: доступ только пользователям из ALLOWED_TG_IDS."""

from app.db import Database
from app.middlewares.whitelist import DENY_TEXT, WhitelistMiddleware
from tests.conftest import make_callback_update, make_message_update


async def _feed(mw, update):
    """Прогоняет событие апдейта через мидлварь, возвращает признак 'дошло до хендлера'."""
    handled = []

    async def handler(event, data):
        handled.append(event)

    event = update.message or update.callback_query
    await mw(handler, event, {})
    return bool(handled)


async def test_allowed_user_passes(tmp_path, bot):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    mw = WhitelistMiddleware(db, allowed_ids={1})

    assert await _feed(mw, make_message_update(bot, "заявка", user_id=1)) is True


async def test_empty_whitelist_denies_by_default(tmp_path, bot, session):
    """Пустой список без явного флага — fail-closed: не пускать никого.

    Раньше пустой список молча пускал всех (fail-open) — теперь прод по
    умолчанию закрыт, а постороннему уходит обычный отказ раз в сутки.
    """
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    mw = WhitelistMiddleware(db, allowed_ids=set(), allow_all=False)

    assert await _feed(mw, make_message_update(bot, "заявка", user_id=999)) is False
    assert session.sent_texts == [DENY_TEXT]


async def test_empty_whitelist_with_allow_all_lets_everyone_in(tmp_path, bot):
    """Открытый режим для staging включается только явным ALLOW_ALL_USERS=true."""
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    mw = WhitelistMiddleware(db, allowed_ids=set(), allow_all=True)

    assert await _feed(mw, make_message_update(bot, "заявка", user_id=999)) is True


async def test_stranger_denied_once_per_day(tmp_path, bot, session):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    mw = WhitelistMiddleware(db, allowed_ids={1})

    # первое сообщение постороннего: до хендлера не дошло, но получен отказ
    assert await _feed(mw, make_message_update(bot, "пустите", user_id=777)) is False
    assert session.sent_texts == [DENY_TEXT]

    # второе за тот же день: молча игнорируется
    assert await _feed(mw, make_message_update(bot, "ну пустите", user_id=777)) is False
    assert session.sent_texts == [DENY_TEXT]


async def test_stranger_callback_denied(tmp_path, bot, session):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    mw = WhitelistMiddleware(db, allowed_ids={1})

    assert await _feed(mw, make_callback_update(bot, "order:create", user_id=777)) is False
    # ответ ушёл через answerCallbackQuery, а не сообщением в чат
    assert session.sent_texts == []
    assert any(type(r).__name__ == "AnswerCallbackQuery" for r in session.requests)
