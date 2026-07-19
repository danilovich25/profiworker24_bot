"""Базовые проверки: конфиг, импорты, маскирование, лимитер, init_bitrix."""

import asyncio

import pytest
from aiogram.fsm.storage.base import StorageKey

from app.config import Settings
from app.middlewares.logging import mask_phones
from app.middlewares.pruning_isolation import PruningEventIsolation
from app.middlewares.rate_limit import SlidingWindowLimiter
from app.schemas import Category, Intent, ParsedOrder
from app.services.bitrix import UFFieldsError


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == "/data/bot.db"
    assert s.allowed_ids == set()
    assert s.allow_all_users is False  # открытый режим только явным флагом
    assert s.tz == "Asia/Vladivostok"


def test_allowed_ids_parsing():
    s = Settings(_env_file=None, allowed_tg_ids="123, 456,notanid")
    assert s.allowed_ids == {123, 456}


def test_allowed_ids_semicolon_and_space_separators():
    assert Settings(_env_file=None, allowed_tg_ids="123;456").allowed_ids == {123, 456}
    assert Settings(_env_file=None, allowed_tg_ids="123 456 789").allowed_ids == {123, 456, 789}


def test_allowed_ids_nonempty_without_digits_raises():
    """Непустой ALLOWED_TG_IDS без единого ID — ошибка конфигурации на старте."""
    s = Settings(_env_file=None, allowed_tg_ids="abc")
    with pytest.raises(ValueError, match="ALLOWED_TG_IDS"):
        s.allowed_ids


def test_parsed_order_profit():
    order = ParsedOrder(problem="замена крана", income_rub=8000, expense_rub=5000)
    assert order.profit_rub == 3000
    assert order.category is None  # категория не указана - бот уточнит
    assert order.intent == Intent.new_order
    assert ParsedOrder(problem="x", category="сантехника").category == Category.plumbing
    assert ParsedOrder(problem="x").profit_rub is None


def test_mask_phones_hides_digits():
    masked = mask_phones("Иван, +79141234567, замена крана")
    assert "+79141234567" not in masked
    assert "9141234567" not in masked
    assert "замена крана" in masked


def test_sliding_window_limiter():
    limiter = SlidingWindowLimiter(limit=3, window=60)
    assert all(limiter.hit(1, now=t) for t in (0, 1, 2))
    assert limiter.hit(1, now=3) is False  # 4-е сообщение в окне режется
    assert limiter.hit(2, now=3) is True  # другой пользователь не страдает
    assert limiter.hit(1, now=61) is True  # окно уехало, снова можно


def test_main_imports():
    import app.main  # noqa: F401


async def test_init_bitrix_disables_crm_when_uf_fields_fail(monkeypatch):
    """Битые UF-поля отключают CRM целиком, а не оставляют полуживой клиент."""
    from app import main

    async def failing(bx):
        raise UFFieldsError("В CRM нет обязательных полей: UF_CRM_TG_MSG_ID")

    monkeypatch.setattr(main, "get_bitrix", lambda url: object())
    monkeypatch.setattr(main, "ensure_uf_fields", failing)

    assert await main.init_bitrix("https://portal.example/rest/1/x/") is None


async def test_init_bitrix_returns_client_when_fields_ready(monkeypatch):
    from app import main

    client = object()

    async def ok(bx):
        return None

    monkeypatch.setattr(main, "get_bitrix", lambda url: client)
    monkeypatch.setattr(main, "ensure_uf_fields", ok)

    assert await main.init_bitrix("https://portal.example/rest/1/x/") is client


async def test_init_bitrix_none_without_webhook():
    from app import main

    assert await main.init_bitrix("") is None
    assert await main.init_bitrix("PENDING") is None


async def test_healthcheck_loop_purges_expired():
    """Часовой цикл зовёт чистку просроченных записей на каждом тике."""
    from app import main

    purged = asyncio.Event()

    class StubDb:
        async def purge_expired(self):
            purged.set()

    task = asyncio.create_task(main.healthcheck_loop(StubDb()))
    await asyncio.wait_for(purged.wait(), timeout=2)  # чистка вызвана
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_event_isolation_prunes_unused_chat_locks():
    """Уникальные чаты не накапливаются в памяти после обработки события."""
    isolation = PruningEventIsolation()

    for chat_id in range(500):
        key = StorageKey(bot_id=1, chat_id=chat_id, user_id=chat_id)
        async with isolation.lock(key):
            pass

    assert isolation.lock_count == 0


async def test_event_isolation_keeps_same_chat_serialized_and_then_prunes():
    isolation = PruningEventIsolation()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    active = 0
    maximum = 0

    async def work():
        nonlocal active, maximum
        async with isolation.lock(key):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(work() for _ in range(20)))

    assert maximum == 1
    assert isolation.lock_count == 0
