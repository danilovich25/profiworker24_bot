"""Дедупликация: ключи сообщений, таблица processed, статусы обработки."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

from app.db import CLAIM_TIMEOUT_SECONDS, Database
from app.middlewares.dedup import DedupMiddleware, content_hash, dedup_key
from tests.conftest import make_message_update


async def test_processed_table_marks_once(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.init()

    assert await db.try_mark_processed("msg:1:100") is True
    assert await db.try_mark_processed("msg:1:100") is False  # повтор = дубль
    assert await db.try_mark_processed("msg:1:101") is True  # другой ключ проходит


async def test_processed_table_stores_deal_id(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.init()

    await db.try_mark_processed("msg:1:100")
    assert await db.get_deal_id("msg:1:100") is None
    await db.set_deal_id("msg:1:100", 154)
    assert await db.get_deal_id("msg:1:100") == 154
    assert await db.get_deal_id("msg:9:999") is None


def test_dedup_key_plain_message():
    msg = SimpleNamespace(forward_origin=None, chat=SimpleNamespace(id=10), message_id=55)
    assert dedup_key(msg) == "msg:10:55"


def test_dedup_key_forward_with_open_origin():
    # у пересланного сообщения новый message_id, ключ строится по источнику
    origin = SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=7)
    msg = SimpleNamespace(forward_origin=origin, chat=SimpleNamespace(id=10), message_id=55)
    other = SimpleNamespace(forward_origin=origin, chat=SimpleNamespace(id=10), message_id=56)

    assert dedup_key(msg) == "fwd:-100123:7"
    assert dedup_key(msg) == dedup_key(other)  # тот же forward дважды = тот же ключ


def test_dedup_key_forward_from_user():
    date = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    origin = SimpleNamespace(
        chat=None, message_id=None, sender_user=SimpleNamespace(id=42), date=date
    )
    text = "Иван, 89141234567, замена крана"
    msg = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=55, text=text
    )

    expected = f"fwd:u42:{int(date.timestamp())}:{content_hash(text)[:16]}"
    assert dedup_key(msg) == expected


def test_dedup_key_forward_from_user_same_second_different_text():
    # два РАЗНЫХ сообщения от одного юзера в одну секунду - разные ключи,
    # второе легитимное не должно считаться дублем
    date = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    origin = SimpleNamespace(
        chat=None, message_id=None, sender_user=SimpleNamespace(id=42), date=date
    )
    first = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=55, text="замена крана"
    )
    second = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=56, text="сборка шкафа"
    )

    assert dedup_key(first) != dedup_key(second)


async def test_dedup_key_forward_hidden_origin(tmp_path):
    # у MessageOriginHiddenUser нет ни chat, ни sender_user - ключ по тексту
    # и дате исходника: один и тот же forward дважды даёт один ключ
    date = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    origin = SimpleNamespace(chat=None, message_id=None, sender_user=None, date=date)
    text = "Пётр, 89147654321, сборка шкафа"
    first = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=55, text=text
    )
    second = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=56, text=text
    )

    key1, key2 = dedup_key(first), dedup_key(second)
    assert key1 == key2
    assert key1 == f"fwd:hidden:{content_hash(text)}:{int(date.timestamp())}"

    db = Database(str(tmp_path / "test.db"))
    await db.init()
    assert await db.try_mark_processed(key1) is True
    assert await db.try_mark_processed(key2) is False  # второй forward помечен дублем


def test_dedup_key_forward_hidden_origin_different_text_not_glued():
    date = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    origin = SimpleNamespace(chat=None, message_id=None, sender_user=None, date=date)
    a = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=55, text="кран"
    )
    b = SimpleNamespace(
        forward_origin=origin, chat=SimpleNamespace(id=10), message_id=56, text="шкаф"
    )

    assert dedup_key(a) != dedup_key(b)


def test_content_hash_ignores_case_and_spacing():
    a = content_hash("Иван, 89141234567,  замена крана")
    b = content_hash("иван, 89141234567, замена   крана")
    c = content_hash("другой текст")

    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# Статусы обработки: сбой хендлера и брошенные in_progress не «отравляют» ключ
# ---------------------------------------------------------------------------


async def _age_processed(db: Database, key: str, hours: int) -> None:
    """Состаривает запись processed прямо в базе."""
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE processed SET ts = datetime('now', ?) WHERE key = ?",
            (f"-{hours} hours", key),
        )
        await conn.commit()


async def test_failed_handler_frees_key(tmp_path, bot):
    """Сбой хендлера освобождает ключ: то же сообщение можно обработать снова."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    mw = DedupMiddleware(db)
    event = make_message_update(bot, "заявка").message

    async def failing(event, data):
        raise RuntimeError("хендлер упал")

    with pytest.raises(RuntimeError):
        await mw(failing, event, {})

    handled = []

    async def ok(event, data):
        handled.append(event)

    await mw(ok, event, {})
    assert len(handled) == 1  # ключ не «отравлен», повторная отправка обработана


async def test_cancelled_handler_frees_key(tmp_path, bot):
    """Отмена задачи (шатдаун) освобождает ключ и пробрасывается дальше.

    CancelledError не наследует Exception: без отдельной ветки в мидлвари
    ключ оставался бы «в обработке» на DEDUP_STALE_SECONDS (6 часов).
    """
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    mw = DedupMiddleware(db)
    event = make_message_update(bot, "заявка").message

    async def cancelled(event, data):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mw(cancelled, event, {})

    handled = []

    async def ok(event, data):
        handled.append(event)

    await mw(ok, event, {})
    assert len(handled) == 1  # ключ освобождён, повторная отправка обработана


# Тесты ниже переведены с try_mark_processed(in_progress=True) на
# claim_processing: захват обработки теперь возвращает токен владельца,
# а не голый флаг (защита от гонки перехвата брошенной записи).


async def test_fresh_in_progress_duplicate_blocked(tmp_path):
    """Свежий дубль во время обработки отсекается."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()

    assert await db.claim_processing("msg:1:100") is not None
    assert await db.claim_processing("msg:1:100") is None


async def test_stale_in_progress_key_is_retaken(tmp_path):
    """Брошенный in_progress без deal_id старше порога перехватывается как новый."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    assert await db.claim_processing("msg:1:100") is not None

    await _age_processed(db, "msg:1:100", hours=7)

    assert await db.claim_processing("msg:1:100") is not None


async def test_stale_done_key_not_retaken(tmp_path):
    """Успешно завершённая обработка не перехватывается даже спустя сутки."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    token = await db.claim_processing("msg:1:100")
    assert await db.mark_done("msg:1:100", token) is True

    await _age_processed(db, "msg:1:100", hours=24)

    assert await db.claim_processing("msg:1:100") is None


async def test_stale_key_with_deal_id_not_retaken(tmp_path):
    """Ключ с созданной заявкой хранится вечно — по нему показывается её номер."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    await db.claim_processing("msg:1:100")
    await db.set_deal_id("msg:1:100", 154)

    await _age_processed(db, "msg:1:100", hours=24)

    assert await db.claim_processing("msg:1:100") is None
    assert await db.get_deal_id("msg:1:100") == 154


async def test_stale_reclaim_invalidates_old_owner(tmp_path):
    """Перехваченный обработчик не может стереть или завершить запись нового.

    Гонка: первый воркер завис, его запись перехватили; проснувшись, он
    зовёт unmark/mark_done со СТАРЫМ токеном — оба compare-and-set обязаны
    отказать, иначе он удалил бы дедуп-ключ нового владельца.
    """
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    old_token = await db.claim_processing("msg:1:100")
    assert old_token is not None

    await _age_processed(db, "msg:1:100", hours=7)
    new_token = await db.claim_processing("msg:1:100")
    assert new_token is not None
    assert new_token != old_token

    # старый владелец не проходит ни один compare-and-set
    assert await db.unmark_processed("msg:1:100", old_token) is False
    assert await db.mark_done("msg:1:100", old_token) is False
    assert await db.set_deal_id("msg:1:100", 999, proc_token=old_token) is False

    # запись цела и по-прежнему в обработке у нового владельца
    assert await db.claim_processing("msg:1:100") is None
    assert await db.get_deal_id("msg:1:100") is None
    assert await db.mark_done("msg:1:100", new_token) is True


async def test_unmark_never_deletes_key_with_deal(tmp_path):
    """Ключ с записанной сделкой не освобождается даже своим токеном."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    token = await db.claim_processing("msg:1:100")
    await db.set_deal_id("msg:1:100", 154)

    assert await db.unmark_processed("msg:1:100", token) is False
    assert await db.get_deal_id("msg:1:100") == 154


async def test_duplicate_with_deal_id_reports_number(tmp_path, bot, session):
    """Дубль сообщения, по которому заявка уже создана, отвечает её номером."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    mw = DedupMiddleware(db)
    event = make_message_update(bot, "заявка").message

    async def ok(event, data):
        pass

    await mw(ok, event, {})
    await db.set_deal_id(dedup_key(event), 154)

    handled = []

    async def h2(event, data):
        handled.append(event)

    await mw(h2, event, {})
    assert handled == []  # до хендлера дубль не дошёл
    assert "заявка №154" in session.sent_texts[-1]


async def test_whitelist_deny_keys_unaffected(tmp_path):
    """Ключи whitelist_deny (без in_progress) не перехватываются со временем."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    assert await db.try_mark_processed("whitelist_deny:777:2026-07-16") is True

    await _age_processed(db, "whitelist_deny:777:2026-07-16", hours=7)

    assert await db.try_mark_processed("whitelist_deny:777:2026-07-16") is False


# ---------------------------------------------------------------------------
# Контент-дедуп: тот же текст в окне 24 часов (второй уровень)
# Тесты переписаны вместе с механизмом: раздельные «проверить» и
# «зарегистрировать» оставляли гонку — два одинаковых текста, пришедших
# одновременно, проходили проверку оба за время работы модели. Теперь
# проверка и захват хэша атомарны (claim_content).
# ---------------------------------------------------------------------------


async def _make_content_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    return db


async def _age_content_claims(db: Database, hours: int) -> None:
    """Состаривает захваты контент-хэшей прямо в базе."""
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE content_claims SET ts = datetime('now', ?)", (f"-{hours} hours",)
        )
        await conn.commit()


async def test_claim_content_first_wins(tmp_path):
    """Первый захват хэша проходит, повтор в том же чате получает отказ."""
    db = await _make_content_db(tmp_path)

    assert await db.claim_content(1, "hash1", "msg:1:100") is None  # занято нами
    assert await db.claim_content(1, "hash1", "msg:1:101") == {"deal_id": None}  # дубль
    assert await db.claim_content(2, "hash1", "msg:2:100") is None  # другой чат
    assert await db.claim_content(1, "hash2", "msg:1:102") is None  # другой текст


async def test_claim_content_parallel_single_winner(tmp_path):
    """Гонка одинаковых текстов: захват достаётся ровно одному."""
    db = await _make_content_db(tmp_path)

    results = await asyncio.gather(
        *(db.claim_content(1, "h", f"msg:1:{i}") for i in range(5))
    )

    assert results.count(None) == 1  # один победитель, остальным вернулся дубль


async def test_claim_content_reports_deal_number(tmp_path):
    """После создания сделки повтор текста получает её номер."""
    db = await _make_content_db(tmp_path)
    assert await db.claim_content(1, "h", "msg:1:100") is None

    # сделка фиксируется штатным путём: complete_draft дописывает номер
    # и в контент-дедуп той же транзакцией
    await db.save_draft("d1", chat_id=1, user_id=1, parsed_json="{}", dedup_key="msg:1:100")
    draft = await db.claim_draft("d1")
    assert await db.complete_draft("d1", "msg:1:100", 154, draft["claim_token"]) is True

    assert await db.claim_content(1, "h", "msg:1:200") == {"deal_id": 154}


async def test_claim_content_expires_after_window(tmp_path):
    """Захват старше 24 часов перезанимается заново, вторая строка не плодится."""
    db = await _make_content_db(tmp_path)
    assert await db.claim_content(1, "h", "msg:1:100") is None

    await _age_content_claims(db, hours=25)

    assert await db.claim_content(1, "h", "msg:1:200") is None  # окно прошло
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM content_claims")
        row = await cur.fetchone()
    assert row[0] == 1  # просроченная строка заменена, а не добавлена вторая


async def test_release_content_cas_and_deal_guard(tmp_path):
    """Освобождение хэша: только свой ключ и никогда — с записанной сделкой."""
    db = await _make_content_db(tmp_path)
    assert await db.claim_content(1, "h", "msg:1:100") is None

    # чужой ключ захват не снимает
    assert await db.release_content(1, "h", "msg:1:999") is False
    assert await db.claim_content(1, "h", "msg:1:101") == {"deal_id": None}

    # свой ключ снимает, хэш снова свободен
    assert await db.release_content(1, "h", "msg:1:100") is True
    assert await db.claim_content(1, "h", "msg:1:102") is None

    # захват с созданной сделкой не освобождается даже своим ключом
    await db.save_draft("d1", chat_id=1, user_id=1, parsed_json="{}", dedup_key="msg:1:102")
    draft = await db.claim_draft("d1")
    await db.complete_draft("d1", "msg:1:102", 154, draft["claim_token"])
    assert await db.release_content(1, "h", "msg:1:102") is False
    assert await db.claim_content(1, "h", "msg:1:103") == {"deal_id": 154}


# ---------------------------------------------------------------------------
# Отложенные тексты (кнопка «Создать всё равно»): аренда и физический TTL
# ---------------------------------------------------------------------------


async def test_pending_text_claim_release_delete(tmp_path):
    """Аренда отложенного текста: CAS по владельцу, удаление после обработки."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 2, "текст заявки", "msg:1:5")

    pending = await db.get_pending_text("t1")
    assert pending == {
        "chat_id": 1,
        "user_id": 2,
        "text": "текст заявки",
        "dedup_key": "msg:1:5",
        "phone_asked": False,
    }

    claimed, busy = await db.claim_pending_text("t1")
    assert claimed is not None and busy is False
    assert claimed["text"] == "текст заявки"
    owner = claimed["claim_token"]
    # живую аренду не перехватить, исход различим: запись занята, а не пропала
    assert await db.claim_pending_text("t1") == (None, True)

    # сбой обработки: чужой токен аренду не снимает, свой — снимает
    assert await db.release_pending_text("t1", "чужой-токен") is False
    assert await db.release_pending_text("t1", owner) is True
    reclaimed, busy = await db.claim_pending_text("t1")  # кнопка срабатывает повторно
    assert reclaimed is not None and busy is False

    # удаление — только владельцем и только один раз
    assert await db.delete_pending_text("t1", "чужой-токен") is False
    assert await db.delete_pending_text("t1", reclaimed["claim_token"]) is True
    assert await db.get_pending_text("t1") is None
    assert await db.claim_pending_text("t1") == (None, False)  # записи больше нет


async def test_pending_text_keeps_phone_asked_flag(tmp_path):
    """Признак «телефон уже спрашивали» переживает откладывание текста."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 2, "текст заявки", "msg:1:5", phone_asked=True)

    pending = await db.get_pending_text("t1")
    assert pending is not None and pending["phone_asked"] is True

    claimed, _ = await db.claim_pending_text("t1")
    assert claimed is not None and claimed["phone_asked"] is True


async def test_pending_text_stale_claim_retaken(tmp_path):
    """Брошенная аренда (процесс убит посреди обработки) перехватывается."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст", "msg:1:5")
    first, _ = await db.claim_pending_text("t1")
    assert first is not None

    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET claimed_at = datetime('now', ?)",
            (f"-{CLAIM_TIMEOUT_SECONDS + 30} seconds",),
        )
        await conn.commit()

    second, _ = await db.claim_pending_text("t1")
    assert second is not None
    assert second["claim_token"] != first["claim_token"]
    # проснувшийся старый владелец не трогает чужую аренду
    assert await db.release_pending_text("t1", first["claim_token"]) is False
    assert await db.delete_pending_text("t1", first["claim_token"]) is False


async def test_refresh_pending_claim_cas(tmp_path):
    """Продление аренды текста: только своим токеном; потеря детектируется."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст", "msg:1:5")
    claimed, _ = await db.claim_pending_text("t1")
    owner = claimed["claim_token"]

    assert await db.refresh_pending_claim("t1", owner) is True
    assert await db.refresh_pending_claim("t1", "чужой-токен") is False

    # запись, изъятая победителем, не продлевается — владение потеряно
    assert await db.delete_pending_text("t1", owner) is True
    assert await db.refresh_pending_claim("t1", owner) is False


async def test_finalize_pending_to_draft_fencing(tmp_path):
    """Fencing-переход: черновик из отложенного текста создаёт только владелец.

    Гонка перехвата: первый обработчик завис, его аренду перехватил второй.
    Переход «pending-строка → черновик» атомарен и защищён CAS по
    claim_token, поэтому проснувшийся зомби не может создать второй черновик.
    """
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст", "msg:1:5")
    first, _ = await db.claim_pending_text("t1")
    assert first is not None

    # аренда брошена (обработчик завис) и перехвачена вторым нажатием
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET claimed_at = datetime('now', ?)",
            (f"-{CLAIM_TIMEOUT_SECONDS + 30} seconds",),
        )
        await conn.commit()
    second, _ = await db.claim_pending_text("t1")
    assert second is not None

    # победитель проводит переход: строка изъята, черновик создан
    assert await db.finalize_pending_to_draft(
        "t1",
        second["claim_token"],
        "d-winner",
        chat_id=1,
        user_id=1,
        parsed_json="{}",
        dedup_key="msg:1:5",
    ) is True
    assert await db.get_draft("d-winner") is not None
    assert await db.get_pending_text("t1") is None

    # зомби со старым токеном проигрывает CAS: второго черновика нет
    assert await db.finalize_pending_to_draft(
        "t1",
        first["claim_token"],
        "d-zombie",
        chat_id=1,
        user_id=1,
        parsed_json="{}",
        dedup_key="msg:1:5",
    ) is False
    assert await db.get_draft("d-zombie") is None


async def test_force_transition_rollback_restores_text_atomically(tmp_path):
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "исходный текст", "msg:1:5", phone_asked=True)
    pending, _ = await db.claim_pending_text("t1")
    owner = pending["claim_token"]
    assert await db.finalize_pending_to_draft(
        "t1", owner, "d1", 1, 1, "{}", "msg:1:5"
    ) is True

    assert await db.rollback_pending_draft(
        "t1", owner, "d1", 1, 1, "исходный текст", "msg:1:5", True
    ) is True
    assert await db.get_draft("d1") is None
    restored = await db.get_pending_text("t1")
    assert restored is not None
    assert restored["text"] == "исходный текст"
    assert restored["phone_asked"] is True


async def test_pending_text_expired_rows_physically_removed(tmp_path):
    """Просроченный текст удаляется из таблицы физически, а не просто прячется.

    Полный текст заявки содержит имена и телефоны: хранить его дольше TTL
    нельзя, поэтому чистка происходит в транзакциях захвата и сохранения.
    """
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "старый текст", "msg:1:5")

    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET created_at = datetime('now', '-31 minutes')"
        )
        await conn.commit()

    # захват просроченного токена не проходит («записи нет», а не «занято»)
    # и физически удаляет строку
    assert await db.claim_pending_text("t1") == (None, False)
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM pending_texts")
        row = await cur.fetchone()
    assert row[0] == 0

    # сохранение нового текста тоже вычищает просроченные строки
    await db.save_pending_text("t2", 1, 1, "текст", "msg:1:6")
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET created_at = datetime('now', '-31 minutes')"
        )
        await conn.commit()
    await db.save_pending_text("t3", 1, 1, "новый текст", "msg:1:7")
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute("SELECT token FROM pending_texts")
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["t3"]  # t2 удалён физически


async def test_purge_expired_removes_stale_rows_without_traffic(tmp_path):
    """Чистка по таймеру: просроченные PII-тексты и старые хэши исчезают сами.

    Ровно пробел прежних тестов: между старением и проверкой НЕ выполняется
    никакая попутная операция БД (клик, сохранение, рестарт) — только
    purge_expired, как его раз в час зовёт healthcheck_loop. Текст заявки
    с именем и телефоном не должен пережить TTL даже при полном отсутствии
    трафика в боте.
    """
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст с телефоном", "msg:1:5")
    assert await db.claim_content(1, "h", "msg:1:5") is None

    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET created_at = datetime('now', '-31 minutes')"
        )
        await conn.execute("UPDATE content_claims SET ts = datetime('now', '-25 hours')")
        await conn.commit()

    await db.purge_expired()  # единственная операция после старения

    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM pending_texts")
        pending_count = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM content_claims")
        claims_count = (await cur.fetchone())[0]
    assert pending_count == 0
    assert claims_count == 0


async def test_purge_expired_keeps_live_rows(tmp_path):
    """Чистка не трогает живые записи в пределах TTL и окна дедупа."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст", "msg:1:5")
    assert await db.claim_content(1, "h", "msg:1:5") is None

    await db.purge_expired()

    assert await db.get_pending_text("t1") is not None
    assert await db.claim_content(1, "h", "msg:1:6") == {"deal_id": None}  # захват жив


async def test_init_purges_expired_rows(tmp_path):
    """Перезапуск бота физически вычищает просроченные тексты и старые хэши."""
    db = await _make_content_db(tmp_path)
    await db.save_pending_text("t1", 1, 1, "текст с телефоном", "msg:1:5")
    assert await db.claim_content(1, "h", "msg:1:5") is None

    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE pending_texts SET created_at = datetime('now', '-31 minutes')"
        )
        await conn.execute("UPDATE content_claims SET ts = datetime('now', '-25 hours')")
        await conn.commit()

    await db.init()  # повторная инициализация = рестарт процесса

    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM pending_texts")
        pending_count = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM content_claims")
        claims_count = (await cur.fetchone())[0]
    assert pending_count == 0
    assert claims_count == 0
