"""Захват черновиков: аренда с владельцем-токеном и перехват брошенной аренды.

Захват (claim_draft) — аренда черновика на время записи в CRM с уникальным
claim_token владельца. Если процесс убит между захватом и удалением черновика
(рестарт на деплое, OOM), claimed_at остаётся в базе, и без таймаута кнопка
«Создать» молчала бы до конца TTL. Продление/снятие/удаление аренды работают
только со своим токеном (compare-and-set): воркер, чью аренду перехватили,
не может испортить чужую.
"""

import asyncio

import aiosqlite

from app.db import (
    CLAIM_TIMEOUT_SECONDS,
    DRAFT_DONE,
    DRAFT_OPEN,
    DRAFT_UNKNOWN,
    Database,
)
from app.handlers import messages


async def make_db_with_draft(tmp_path) -> Database:
    db = Database(str(tmp_path / "drafts.db"))
    await db.init()
    await db.save_draft("d1", chat_id=1, user_id=1, parsed_json="{}")
    return db


async def age_claim(db: Database, seconds: int) -> None:
    """Состаривает захват прямо в базе (симуляция зависшего процесса)."""
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE drafts SET claimed_at = datetime('now', ?) WHERE draft_id = 'd1'",
            (f"-{seconds} seconds",),
        )
        await conn.commit()


async def test_claim_returns_owner_token(tmp_path):
    db = await make_db_with_draft(tmp_path)

    draft = await db.claim_draft("d1")

    assert draft is not None
    assert draft["chat_id"] == 1
    assert draft["claim_token"]  # владелец аренды получает токен


async def test_claim_recovers_stale_lease(tmp_path):
    """Брошенный захват старше CLAIM_TIMEOUT_SECONDS перехватывается с новым токеном."""
    db = await make_db_with_draft(tmp_path)
    first = await db.claim_draft("d1")
    assert first is not None

    # Процесс «умер» после захвата: черновик не удалён, claimed_at остался.
    await age_claim(db, CLAIM_TIMEOUT_SECONDS + 30)

    second = await db.claim_draft("d1")
    assert second is not None  # кнопка снова работает, ждать конца TTL не надо
    assert second["claim_token"] != first["claim_token"]  # владелец сменился


async def test_claim_blocks_fresh_lease(tmp_path):
    """Свежий захват (моложе таймаута) второму нажатию не отдаётся."""
    db = await make_db_with_draft(tmp_path)
    assert await db.claim_draft("d1") is not None
    assert await db.claim_draft("d1") is None


async def test_refresh_claim_requires_own_token(tmp_path):
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")

    assert await db.refresh_claim("d1", draft["claim_token"]) is True
    assert await db.refresh_claim("d1", "чужой-токен") is False


async def test_release_requires_own_token(tmp_path):
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")

    # чужой токен аренду не снимает: черновик остаётся захваченным
    assert await db.release_draft("d1", "чужой-токен") is False
    assert await db.claim_draft("d1") is None

    # свой токен снимает, и захват снова доступен
    assert await db.release_draft("d1", draft["claim_token"]) is True
    assert await db.claim_draft("d1") is not None


async def test_lost_lease_cannot_touch_foreign_claim(tmp_path):
    """Проснувшийся после перехвата воркер не трогает чужую аренду."""
    db = await make_db_with_draft(tmp_path)
    first = await db.claim_draft("d1")
    old_token = first["claim_token"]

    # первый воркер завис, аренду перехватил второй
    await age_claim(db, CLAIM_TIMEOUT_SECONDS + 30)
    second = await db.claim_draft("d1")
    assert second is not None

    # все операции первого воркера со старым токеном отклоняются
    assert await db.refresh_claim("d1", old_token) is False
    assert await db.release_draft("d1", old_token) is False
    assert await db.delete_draft("d1", old_token) is False
    assert await db.set_draft_contact("d1", 15, old_token) is False

    # черновик цел и по-прежнему принадлежит второму воркеру
    assert await db.get_draft("d1") is not None
    assert await db.refresh_claim("d1", second["claim_token"]) is True


async def test_delete_and_set_contact_without_token(tmp_path):
    """Пути cancel/edit токена не имеют и работают по draft_id.

    Тест обновлён: раньше он закреплял небезопасное поведение — delete_draft
    без токена удалял и активно захваченный черновик, из-за чего «Отмена»
    могла соврать во время идущей записи в CRM. Теперь удаление без токена
    проходит только для незахваченного черновика.
    """
    db = await make_db_with_draft(tmp_path)

    assert await db.set_draft_contact("d1", 15) is True
    assert (await db.get_draft("d1"))["contact_id"] == 15
    assert await db.delete_draft("d1") is True
    assert await db.get_draft("d1") is None


async def test_tokenless_delete_rejects_active_claim(tmp_path):
    """«Отмена» не удаляет черновик, который прямо сейчас пишется в CRM."""
    db = await make_db_with_draft(tmp_path)
    assert await db.claim_draft("d1") is not None

    assert await db.delete_draft("d1") is False  # захвачен — отмена отклонена
    assert await db.get_draft("d1") is not None  # черновик цел

    # просроченный захват (владелец мёртв) удалению не мешает
    await age_claim(db, CLAIM_TIMEOUT_SECONDS + 30)
    assert await db.delete_draft("d1") is True


async def test_deal_fence_is_permanent_and_shared_by_key(tmp_path):
    db = Database(str(tmp_path / "fences.db"))
    await db.init()
    await db.save_draft("d1", 1, 1, "{}", "msg:1:1")
    claimed = await db.claim_draft("d1")
    first = await db.claim_deal_fence("msg:1:1", "d1")
    assert first["owned"] is True and first["status"] == "reserved"
    assert await db.mark_deal_fence_contact_sent("msg:1:1", "d1")
    assert await db.settle_deal_fence_contact(
        "msg:1:1", "d1", claimed["claim_token"], 15, True
    )
    assert await db.mark_deal_fence_sent("msg:1:1", "d1") is True

    # Новый объект Database имитирует рестарт процесса: второй черновик с
    # тем же idempotency key всё равно не получает право на add.
    restarted = Database(db.path)
    second = await restarted.claim_deal_fence("msg:1:1", "d2")
    assert second == {
        "owned": False,
        "draft_id": "d1",
        "status": "sent",
        "deal_id": None,
    }


async def test_reserved_deal_fence_moves_to_replacement_draft(tmp_path):
    """Удалённый draft не удерживает безопасную reserved-фазу навсегда."""
    db = Database(str(tmp_path / "reserved-fence.db"))
    await db.init()
    await db.save_draft("d1", 1, 1, "{}", "msg:1:1")
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["owned"] is True
    assert await db.delete_draft("d1") is True
    await db.save_draft("d2", 1, 1, "{}", "msg:1:1")

    replacement = await db.claim_deal_fence("msg:1:1", "d2")

    assert replacement["owned"] is True
    assert replacement["draft_id"] == "d2"
    assert replacement["status"] == "reserved"


async def test_crm_phases_are_invalidated_for_replacement_draft(tmp_path):
    """Contact/comment-фазы относятся только к неизменяемому снимку draft."""
    db = Database(str(tmp_path / "replacement-phases.db"))
    await db.init()
    await db.save_draft("d1", 1, 1, "{}", "msg:1:1")
    claimed = await db.claim_draft("d1")
    await db.claim_deal_fence("msg:1:1", "d1")
    assert await db.mark_deal_fence_contact_sent("msg:1:1", "d1")
    assert await db.settle_deal_fence_contact(
        "msg:1:1", "d1", claimed["claim_token"], 15, True
    )
    assert await db.delete_draft("d1", claimed["claim_token"])
    await db.save_draft("d2", 1, 1, "{}", "msg:1:1")

    replacement = await db.claim_deal_fence("msg:1:1", "d2")

    assert replacement["owned"] is True
    assert replacement["status"] == "reserved"


async def test_deal_fence_tracks_contact_and_deal_boundaries(tmp_path):
    """Каждая неидемпотентная add-фаза имеет постоянную границу отправки."""
    db = Database(str(tmp_path / "phased-fence.db"))
    await db.init()
    await db.save_draft("d1", 1, 1, "{}", "msg:1:1")
    await db.claim_deal_fence("msg:1:1", "d1")

    assert await db.mark_deal_fence_contact_sent("msg:1:1", "d1") is True
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "contact_sent"
    assert await db.settle_deal_fence_contact("msg:1:1", "d1", "owner", 15, True) is False
    claimed = await db.claim_draft("d1")
    assert claimed is not None
    owner = claimed["claim_token"]
    assert await db.settle_deal_fence_contact("msg:1:1", "d1", owner, 15, True) is True
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "comment_done"
    assert await db.mark_deal_fence_sent("msg:1:1", "d1") is True
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "sent"


async def test_deal_fence_separates_existing_contact_and_comment(tmp_path):
    """Явный отказ комментария откатывает только его границу."""
    db = Database(str(tmp_path / "comment-phases.db"))
    await db.init()
    await db.save_draft("d1", 1, 1, "{}", "msg:1:1")
    claimed = await db.claim_draft("d1")
    owner = claimed["claim_token"]
    await db.claim_deal_fence("msg:1:1", "d1")

    assert await db.settle_deal_fence_contact("msg:1:1", "d1", owner, 23, False)
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "contact_ready"
    assert await db.mark_deal_fence_comment_sent("msg:1:1", "d1")
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "comment_sent"
    assert await db.reset_deal_fence("msg:1:1", "d1", "comment_sent")
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "contact_ready"
    assert await db.mark_deal_fence_comment_sent("msg:1:1", "d1")
    assert await db.settle_deal_fence_comment("msg:1:1", "d1")
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "comment_done"


async def test_deal_fence_reset_is_scoped_to_exact_unsafe_phase(tmp_path):
    db = Database(str(tmp_path / "scoped-reset.db"))
    await db.init()
    await db.claim_deal_fence("msg:1:1", "d1")

    assert await db.reset_deal_fence("msg:1:1", "d1", "contact_sent") is False
    assert await db.mark_deal_fence_contact_sent("msg:1:1", "d1")
    assert await db.reset_deal_fence("msg:1:1", "d1", "sent") is False
    assert (await db.claim_deal_fence("msg:1:1", "d1"))["status"] == "contact_sent"


async def test_task_fence_allows_only_one_add_boundary(tmp_path):
    db = Database(str(tmp_path / "task-fences.db"))
    await db.init()
    await db.get_or_create_task_fence("msg:1:2")
    results = await asyncio.gather(*(db.mark_task_fence_sent("msg:1:2") for _ in range(5)))
    assert results.count(True) == 1
    assert results.count(False) == 4


# -- begin_edit: атомарное изъятие черновика в редактирование ---------------
# Заменяет пару is_actively_claimed + вход в FSM: раньше между проверкой
# аренды и входом в редактирование оставался зазор, в котором конкурентный
# claim_draft успевал захватить черновик (TOCTOU edit против create).


async def test_begin_edit_takes_draft_atomically(tmp_path):
    """begin_edit возвращает данные и изымает строку одной транзакцией."""
    db = await make_db_with_draft(tmp_path)

    data = await db.begin_edit("d1")

    assert data is not None
    assert data["chat_id"] == 1 and data["parsed_json"] == "{}"
    assert await db.get_draft("d1") is None  # строка изъята
    assert await db.claim_draft("d1") is None  # опоздавший «Создать» не проходит


async def test_begin_edit_rejects_active_claim(tmp_path):
    """Черновик с живой арендой «Создать» в редактирование не отдаётся."""
    db = await make_db_with_draft(tmp_path)
    assert await db.claim_draft("d1") is not None

    assert await db.begin_edit("d1") is None
    assert await db.get_draft("d1") is not None  # черновик цел

    # просроченный захват (владелец мёртв) редактированию не мешает
    await age_claim(db, CLAIM_TIMEOUT_SECONDS + 30)
    assert await db.begin_edit("d1") is not None


async def test_begin_edit_rejects_non_open_draft(tmp_path):
    """Терминальные черновики (done, creation_unknown) не редактируются."""
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")
    await db.mark_draft_unknown("d1", draft["claim_token"], "msg:1:1")
    assert await db.begin_edit("d1") is None

    await db.complete_draft("d1", "msg:1:1", 154)
    assert await db.begin_edit("d1") is None


async def test_begin_edit_vs_claim_single_winner(tmp_path):
    """Конкурентные begin_edit и claim_draft: побеждает ровно один путь."""
    db = Database(str(tmp_path / "race.db"))
    await db.init()
    for i in range(10):
        draft_id = f"d{i}"
        await db.save_draft(draft_id, chat_id=1, user_id=1, parsed_json="{}")
        edited, claimed = await asyncio.gather(
            db.begin_edit(draft_id), db.claim_draft(draft_id)
        )
        assert (edited is not None) != (claimed is not None)  # строгий XOR


# -- Машина состояний черновика: open / creation_unknown / done -------------


async def test_new_draft_is_open(tmp_path):
    db = await make_db_with_draft(tmp_path)
    draft = await db.get_draft("d1")
    assert draft["status"] == DRAFT_OPEN and draft["deal_id"] is None


async def test_mark_draft_unknown_freezes_draft(tmp_path):
    """creation_unknown навсегда запрещает новый захват, правку и отмену."""
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")

    assert await db.mark_draft_unknown("d1", "чужой-токен", "k") is False  # не наш
    assert await db.mark_draft_unknown("d1", draft["claim_token"], "msg:1:1") is True

    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_UNKNOWN
    assert stored["dedup_key"] == "msg:1:1"  # ключ сверки сохранён

    # даже после истечения аренды черновик не перехватывается под новый add
    await age_claim(db, CLAIM_TIMEOUT_SECONDS + 30)
    assert await db.claim_draft("d1") is None
    assert await db.begin_edit("d1") is None
    assert await db.delete_draft("d1") is False  # отмена тоже отклоняется


async def test_complete_draft_writes_processed_and_tombstone(tmp_path):
    """Успех фиксируется одной транзакцией: processed.deal_id + черновик done."""
    db = await make_db_with_draft(tmp_path)
    await db.claim_processing("msg:1:100")  # запись processed, как у сообщения
    draft = await db.claim_draft("d1")

    assert await db.complete_draft("d1", "msg:1:100", 154, draft["claim_token"]) is True

    assert await db.get_deal_id("msg:1:100") == 154  # дубль сообщения ответит номером
    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_DONE and stored["deal_id"] == 154
    assert await db.claim_draft("d1") is None  # tombstone не захватывается
    assert await db.delete_draft("d1") is False  # и не удаляется отменой


async def test_complete_draft_upserts_missing_processed_key(tmp_path):
    """Ключ processed удалён (middleware освободил его после сбоя): UPSERT воссоздаёт.

    Раньше запись в processed шла обычным UPDATE без проверки rowcount: при
    отсутствующем ключе deal_id молча терялся — черновик-tombstone знал номер
    сделки, а дедуп повторной доставки сообщения нет.
    """
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")
    # ключа msg:1:100 в processed нет вовсе (unmark_processed удалил его)

    assert await db.complete_draft("d1", "msg:1:100", 154, draft["claim_token"]) is True

    assert await db.get_deal_id("msg:1:100") == 154  # строка создана заново
    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_DONE and stored["deal_id"] == 154
    # новая строка — done-факт без владельца обработки: перехватывать нечего
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT status, proc_token FROM processed WHERE key = 'msg:1:100'"
        )
        row = await cur.fetchone()
    assert row == ("done", None)


async def test_mark_draft_unknown_does_not_demote_done(tmp_path):
    """Запоздавшая заморозка не понижает уже зафиксированный done.

    Гонка отмены с фиксацией: complete_draft под shield успел закоммитить
    done, а страховка в finally следом пытается заморозить черновик тем же
    токеном — заморозка обязана стать no-op, иначе созданная сделка
    «разфиксировалась» бы обратно в creation_unknown.
    """
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")
    token = draft["claim_token"]
    assert await db.complete_draft("d1", "msg:1:1", 154, token) is True

    assert await db.mark_draft_unknown("d1", token, "msg:1:1") is False

    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_DONE and stored["deal_id"] == 154


async def test_complete_draft_requires_own_token(tmp_path):
    """Чужой токен не закрывает черновик (перехваченная аренда).

    Транзакция откатывается целиком: processed.deal_id тоже не пишется,
    иначе номер сделки в дедупе разошёлся бы с живым open-черновиком.
    """
    db = await make_db_with_draft(tmp_path)
    await db.claim_processing("msg:1:100")
    await db.claim_draft("d1")

    assert await db.complete_draft("d1", "msg:1:100", 154, "чужой-токен") is False
    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_OPEN and stored["deal_id"] is None
    assert await db.get_deal_id("msg:1:100") is None  # processed не тронут


async def test_complete_draft_without_token_resolves_only_unknown(tmp_path):
    """Путь сверки (без токена) закрывает только creation_unknown черновик."""
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")
    await db.mark_draft_unknown("d1", draft["claim_token"], "msg:1:1")

    assert await db.complete_draft("d1", "msg:1:1", 154) is True
    stored = await db.get_draft("d1")
    assert stored["status"] == DRAFT_DONE and stored["deal_id"] == 154

    # обычный open черновик без токена не закрывается, processed не пишется
    await db.save_draft("d2", chat_id=1, user_id=1, parsed_json="{}")
    await db.claim_processing("msg:1:2")
    assert await db.complete_draft("d2", "msg:1:2", 155) is False
    assert (await db.get_draft("d2"))["status"] == DRAFT_OPEN
    assert await db.get_deal_id("msg:1:2") is None  # processed не тронут


async def test_init_migrates_old_drafts_table(tmp_path):
    """База старой схемы (без status/deal_id) получает колонки при init."""
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE drafts ("
            "draft_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, parsed_json TEXT NOT NULL, "
            "dedup_key TEXT NOT NULL DEFAULT '', contact_id INTEGER, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await conn.execute(
            "INSERT INTO drafts (draft_id, chat_id, user_id, parsed_json) "
            "VALUES ('d1', 1, 1, '{}')"
        )
        await conn.commit()

    db = Database(path)
    await db.init()

    draft = await db.get_draft("d1")
    assert draft["status"] == DRAFT_OPEN and draft["deal_id"] is None
    assert await db.claim_draft("d1") is not None  # старый черновик работает


async def test_heartbeat_refreshes_lease_and_detects_takeover(tmp_path):
    """Heartbeat продлевает живую аренду, а при перехвате ставит lease_lost.

    Раньше тест держал аренду реальные 90 секунд; теперь старение аренды
    симулируется в базе, а heartbeat крутится с коротким интервалом.
    """
    db = await make_db_with_draft(tmp_path)
    draft = await db.claim_draft("d1")
    token = draft["claim_token"]

    stop, lease_lost = asyncio.Event(), asyncio.Event()
    heartbeat = asyncio.create_task(
        messages._heartbeat(db, "d1", token, stop, lease_lost, interval=0.05)
    )
    try:
        # аренда почти истекла — очередной удар heartbeat возвращает её к «сейчас»
        await age_claim(db, CLAIM_TIMEOUT_SECONDS - 5)
        await asyncio.sleep(0.3)
        async with aiosqlite.connect(db.path) as conn:
            cur = await conn.execute(
                "SELECT claimed_at > datetime('now', '-5 seconds') "
                "FROM drafts WHERE draft_id = 'd1'"
            )
            row = await cur.fetchone()
        assert row[0] == 1  # claimed_at продлён
        assert await db.claim_draft("d1") is None  # второй claim не проходит
        assert not lease_lost.is_set()

        # аренду перехватил другой воркер (токен сменился): heartbeat замечает
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute("UPDATE drafts SET claim_token = 'другой' WHERE draft_id = 'd1'")
            await conn.commit()
        await asyncio.wait_for(lease_lost.wait(), timeout=2)
        await asyncio.wait_for(heartbeat, timeout=2)  # heartbeat завершился сам
    finally:
        stop.set()
        if not heartbeat.done():
            await heartbeat
