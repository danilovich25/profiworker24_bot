"""SQLite-хранилище: обработанные сообщения (защита от дублей) и черновики заявок.

Черновик (drafts) — это карточка-превью, показанная сотруднику. Кнопки
карточки несут draft_id, поэтому «Создать/Изменить/Отмена» применяются
именно к той карточке, на которой нажаты, а не к последней. Черновик живёт
DRAFT_TTL_SECONDS, потом карточка считается устаревшей. В contact_id
запоминается контакт, созданный при неудачной записи сделки, чтобы повторное
«Создать» не плодило контакты без телефона (их не найти поиском по номеру).

У черновика явная машина состояний (колонка status):
- open — обычный черновик, с ним работают все кнопки;
- creation_unknown — неоднозначный исход deal.add (таймаут, обрыв связи,
  отмена): запрос мог пройти без ответа. Новый deal.add по такой карточке
  запрещён навсегда, повторное «Создать» только сверяется с CRM по
  сохранённому ключу;
- done — терминальный tombstone: сделка создана, её номер лежит в deal_id.
  Черновик на успехе не удаляется, а переводится в done, поэтому повторное
  нажатие отвечает номером сделки даже после сбоя подтверждения.
Старые терминальные записи отдельно не чистятся: get_draft отсекает их по
общему TTL, объём таблицы при реальном трафике незначителен.

Захват черновика кнопкой «Создать» — аренда с владельцем: claim_draft пишет
claimed_at и уникальный claim_token. Продление, снятие и удаление аренды
работают по принципу compare-and-set (AND claim_token = ?): воркер, чью
аренду перехватили после зависания, не может снять или удалить ЧУЖУЮ аренду
и создать вторую сделку.

По той же схеме защищена таблица processed: claim_processing выдаёт
proc_token владельца обработки, а mark_done/unmark_processed проходят только
со своим токеном. Обработчик, чью брошенную запись перехватили по
DEDUP_STALE_SECONDS, не может стереть или завершить запись нового владельца.

Второй уровень дедупа — по содержимому (таблица content_claims):
claim_content атомарно, одной транзакцией, проверяет и занимает
нормализованный хэш текста в чате на окно 24 часов — два одинаковых текста,
пришедших почти одновременно, не могут пройти проверку оба. Номер созданной
сделки дописывается в занятый хэш той же транзакцией, что и complete_draft,
а release_content освобождает хэш, если заявка не состоялась («не заявка»,
напоминание, сбой обработки). Таблица pending_texts хранит текст,
отложенный мягким контент-дедупом, под кнопку «Создать всё равно»: запись
арендуется по образцу drafts (claim_token, CAS) и физически удаляется после
успешной обработки либо по TTL — полный текст заявки (имена, телефоны)
не должен лежать в базе дольше необходимого.
"""

from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

DRAFT_TTL_SECONDS = 30 * 60
# Захват черновика — аренда на время записи в CRM. Если процесс убит между
# захватом и удалением черновика (рестарт, OOM), claimed_at остаётся в базе
# навсегда и кнопка «Создать» перестаёт работать до конца TTL. Поэтому захват
# старше этого таймаута считается брошенным и перехватывается заново.
# Значение согласовано с handlers/messages.py: общий дедлайн записи в CRM
# (CRM_DEADLINE) меньше таймаута, а интервал heartbeat — меньше трети.
CLAIM_TIMEOUT_SECONDS = 120

# Запись в processed со статусом in_progress и без deal_id, которая старше
# этого срока, считается брошенной (процесс убит посреди обработки) и
# перехватывается как новое сообщение.
DEDUP_STALE_SECONDS = 6 * 3600

# Окно контент-дедупа: повтор того же (нормализованного) текста в одном чате
# в течение этого срока считается вероятным дублем заявки.
CONTENT_DUP_WINDOW_SECONDS = 24 * 3600

# Текст, отложенный мягким контент-дедупом, живёт столько же, сколько
# карточка-превью: нажатие «Создать всё равно» по истечении срока отвечает,
# что карточка устарела.
PENDING_TEXT_TTL_SECONDS = DRAFT_TTL_SECONDS

# Состояния черновика (drafts.status). Терминальные состояния (done,
# creation_unknown) не захватываются, не редактируются и не отменяются.
DRAFT_OPEN = "open"
DRAFT_UNKNOWN = "creation_unknown"
DRAFT_DONE = "done"

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    key        TEXT PRIMARY KEY,
    deal_id    INTEGER,
    status     TEXT NOT NULL DEFAULT 'done',
    proc_token TEXT DEFAULT NULL,
    ts         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_processed_ts ON processed (ts);
CREATE TABLE IF NOT EXISTS content_claims (
    chat_id      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    dedup_key    TEXT NOT NULL DEFAULT '',
    deal_id      INTEGER,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_content_claims_key ON content_claims (dedup_key);
CREATE TABLE IF NOT EXISTS pending_texts (
    token       TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    dedup_key   TEXT NOT NULL DEFAULT '',
    phone_asked INTEGER NOT NULL DEFAULT 0,
    claimed_at  TEXT DEFAULT NULL,
    claim_token TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_texts (created_at);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id    TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    parsed_json TEXT NOT NULL,
    dedup_key   TEXT NOT NULL DEFAULT '',
    contact_id  INTEGER,
    claimed_at  TEXT DEFAULT NULL,
    claim_token TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    deal_id     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts (created_at);
CREATE TABLE IF NOT EXISTS deal_fences (
    idempotency_key TEXT PRIMARY KEY,
    draft_id       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'reserved',
    deal_id        INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_fences (
    idempotency_key TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'reserved',
    task_id         INTEGER,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """Обёртка над aiosqlite. Трафик небольшой, соединение открывается на операцию."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        # Каталог базы (например /data на свежей машине без тома) может не
        # существовать — без него SQLite падает с "unable to open database file".
        parent = Path(self.path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(
                f"Нет прав создать каталог базы {parent}. "
                f"Создайте его вручную или укажите доступный путь в DB_PATH."
            ) from exc
        async with aiosqlite.connect(self.path) as conn:
            await conn.executescript(SCHEMA)
            # Миграция старых баз: у SQLite нет ADD COLUMN IF NOT EXISTS,
            # поэтому колонки проверяются через PRAGMA table_info.
            cur = await conn.execute("PRAGMA table_info(drafts)")
            draft_columns = {row[1] for row in await cur.fetchall()}
            if "claimed_at" not in draft_columns:
                await conn.execute("ALTER TABLE drafts ADD COLUMN claimed_at TEXT DEFAULT NULL")
            if "claim_token" not in draft_columns:
                await conn.execute("ALTER TABLE drafts ADD COLUMN claim_token TEXT DEFAULT NULL")
            if "status" not in draft_columns:
                # Существующие черновики получают обычный статус open.
                await conn.execute(
                    "ALTER TABLE drafts ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
                )
            if "deal_id" not in draft_columns:
                await conn.execute("ALTER TABLE drafts ADD COLUMN deal_id INTEGER")
            cur = await conn.execute("PRAGMA table_info(processed)")
            processed_columns = {row[1] for row in await cur.fetchall()}
            if "status" not in processed_columns:
                # Старые записи получают status='done': они никогда не
                # перехватываются, поведение существующих ключей не меняется.
                await conn.execute(
                    "ALTER TABLE processed ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"
                )
            if "proc_token" not in processed_columns:
                # Записи без токена принадлежат «никому»: compare-and-set по
                # владельцу их не трогает, а брошенные in_progress со временем
                # перехватываются с новым токеном как обычно.
                await conn.execute(
                    "ALTER TABLE processed ADD COLUMN proc_token TEXT DEFAULT NULL"
                )
            cur = await conn.execute("PRAGMA table_info(pending_texts)")
            pending_columns = {row[1] for row in await cur.fetchall()}
            if "claimed_at" not in pending_columns:
                # Аренда отложенного текста добавлена позже первой версии
                # таблицы: старые записи получают свободный захват.
                await conn.execute(
                    "ALTER TABLE pending_texts ADD COLUMN claimed_at TEXT DEFAULT NULL"
                )
            if "claim_token" not in pending_columns:
                await conn.execute(
                    "ALTER TABLE pending_texts ADD COLUMN claim_token TEXT DEFAULT NULL"
                )
            if "phone_asked" not in pending_columns:
                # Признак «вопрос о телефоне уже задавался» добавлен позже:
                # старые записи считаются обычным свежим текстом.
                await conn.execute(
                    "ALTER TABLE pending_texts ADD COLUMN "
                    "phone_asked INTEGER NOT NULL DEFAULT 0"
                )
            # В старой схеме успешный контакт и комментарий возвращали fence
            # в reserved. Наличие contact_id доказывает завершение обеих фаз:
            # после обновления нельзя повторно дописывать timeline-комментарий.
            await conn.execute(
                "UPDATE deal_fences SET status = 'comment_done' "
                "WHERE status = 'reserved' AND EXISTS ("
                "SELECT 1 FROM drafts WHERE drafts.draft_id = deal_fences.draft_id "
                "AND drafts.contact_id IS NOT NULL)"
            )
            await conn.commit()
        # Физическая чистка на старте — той же функцией, что и по таймеру
        # (healthcheck_loop в app/main.py): логика чистки живёт в одном месте.
        await self.purge_expired()

    async def purge_expired(self) -> None:
        """Физически удаляет просроченные данные (одна транзакция).

        Отложенный текст старше TTL содержит имена и телефоны клиентов и
        обязан исчезать из базы даже при полном отсутствии трафика — когда
        никто не жмёт кнопки, не шлёт сообщения и бот не перезапускается.
        Поэтому чистка вызывается не только попутно (захват, сохранение,
        init), но и по таймеру из healthcheck_loop. Контент-хэши старше окна
        дедупа удаляются заодно: PII в них нет (хэш односторонний), это
        просто отработавший мусор.
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "DELETE FROM pending_texts WHERE created_at <= datetime('now', ?)",
                (f"-{PENDING_TEXT_TTL_SECONDS} seconds",),
            )
            await conn.execute(
                "DELETE FROM content_claims WHERE ts <= datetime('now', ?)",
                (f"-{CONTENT_DUP_WINDOW_SECONDS} seconds",),
            )
            await conn.commit()

    async def try_mark_processed(self, key: str, deal_id: int | None = None) -> bool:
        """Атомарно помечает ключ обработанным (статус done, без владельца).

        Возвращает True, если ключ новый, и False, если он уже есть.
        Используется для ключей без этапа «идёт обработка» (например,
        whitelist_deny): токена владельца им не нужно, и со временем такие
        записи не перехватываются никогда. Для обработки сообщений с этапом
        in_progress см. claim_processing.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO processed (key, deal_id, status) VALUES (?, ?, 'done')",
                (key, deal_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def claim_processing(self, key: str) -> str | None:
        """Захватывает ключ под обработку сообщения, возвращает токен владельца.

        None означает дубль: ключ уже обрабатывается или обработан. Запись,
        застрявшая в in_progress без deal_id дольше DEDUP_STALE_SECONDS
        (процесс убит посреди обработки), перехватывается с НОВЫМ токеном:
        прежний владелец после этого не проходит ни один compare-and-set
        (mark_done/unmark_processed) и не может тронуть запись нового.
        """
        token = uuid4().hex
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO processed (key, status, proc_token) "
                "VALUES (?, 'in_progress', ?)",
                (key, token),
            )
            if cur.rowcount == 0:
                # Ключ уже есть: перехватываем его, только если обработка
                # была брошена (in_progress без deal_id и старше порога).
                cur = await conn.execute(
                    "UPDATE processed SET ts = datetime('now'), status = 'in_progress', "
                    "proc_token = ? "
                    "WHERE key = ? AND status = 'in_progress' AND deal_id IS NULL "
                    "AND ts < datetime('now', ?)",
                    (token, key, f"-{DEDUP_STALE_SECONDS} seconds"),
                )
            await conn.commit()
            return token if cur.rowcount > 0 else None

    async def mark_done(self, key: str, proc_token: str) -> bool:
        """Помечает обработку завершённой, только если ключ всё ещё наш.

        False означает, что запись перехвачена новым владельцем (или удалена):
        завершать чужую обработку нельзя.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE processed SET status = 'done' WHERE key = ? AND proc_token = ?",
                (key, proc_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def unmark_processed(self, key: str, proc_token: str) -> bool:
        """Освобождает ключ после сбоя обработки (compare-and-set по владельцу).

        Чужую запись (токен не совпал) не трогает. Ключ с записанной сделкой
        не освобождается никогда: иначе потерялись бы её номер и защита от
        дубля сообщения.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "DELETE FROM processed WHERE key = ? AND proc_token = ? AND deal_id IS NULL",
                (key, proc_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def set_deal_id(self, key: str, deal_id: int, proc_token: str | None = None) -> bool:
        """Записывает номер созданной сделки (терминальный факт по ключу).

        С токеном — compare-and-set владельца обработки. Без токена пишет
        просто по ключу: так делает поток «Создать», который защищён арендой
        черновика, а не proc_token; запись с deal_id никогда не перехватывается
        и не удаляется, поэтому затереть нового владельца этот путь не может.
        """
        async with aiosqlite.connect(self.path) as conn:
            if proc_token is None:
                cur = await conn.execute(
                    "UPDATE processed SET deal_id = ?, status = 'done' WHERE key = ?",
                    (deal_id, key),
                )
            else:
                cur = await conn.execute(
                    "UPDATE processed SET deal_id = ?, status = 'done' "
                    "WHERE key = ? AND proc_token = ?",
                    (deal_id, key, proc_token),
                )
            await conn.commit()
            return cur.rowcount == 1

    async def get_deal_id(self, key: str) -> int | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute("SELECT deal_id FROM processed WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else None

    # -- Контент-дедуп: тот же текст в окне 24 часов ------------------------

    async def claim_content(
        self, chat_id: int, content_hash: str, dedup_key: str
    ) -> dict[str, Any] | None:
        """Атомарно проверяет и занимает контент-хэш чата под обработку заявки.

        Возвращает None, если хэш свободен и занят нами — обработка
        продолжается. Живой чужой захват (тот же текст уже присылали в окне
        24 часов) возвращается как {"deal_id": номер сделки или None}.
        Проверка и захват — одна транзакция (BEGIN IMMEDIATE): два одинаковых
        текста, пришедших почти одновременно, не могут пройти проверку оба,
        второй получит предупреждение о дубле. Захват старше окна считается
        просроченным и перезанимается заново.
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT deal_id FROM content_claims "
                "WHERE chat_id = ? AND content_hash = ? AND ts > datetime('now', ?)",
                (chat_id, content_hash, f"-{CONTENT_DUP_WINDOW_SECONDS} seconds"),
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.rollback()
                return {"deal_id": row[0]}
            await conn.execute(
                "INSERT OR REPLACE INTO content_claims "
                "(chat_id, content_hash, dedup_key, deal_id) VALUES (?, ?, ?, NULL)",
                (chat_id, content_hash, dedup_key),
            )
            await conn.commit()
        return None

    async def release_content(self, chat_id: int, content_hash: str, dedup_key: str) -> bool:
        """Освобождает контент-хэш, если заявка не состоялась (CAS по ключу).

        Вызывается, когда текст не начал заявку: «не заявка», напоминание или
        сбой обработки — повтор того же текста не должен считаться дублем.
        Чужой захват (ключ не совпал) не трогается; хэш с записанной сделкой
        не освобождается никогда — иначе потерялись бы её номер и окно дедупа.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "DELETE FROM content_claims "
                "WHERE chat_id = ? AND content_hash = ? AND dedup_key = ? "
                "AND deal_id IS NULL",
                (chat_id, content_hash, dedup_key),
            )
            await conn.commit()
            return cur.rowcount == 1

    # -- Отложенные тексты (кнопка «Создать всё равно») ----------------------

    async def save_pending_text(
        self,
        token: str,
        chat_id: int,
        user_id: int,
        text: str,
        dedup_key: str,
        phone_asked: bool = False,
    ) -> None:
        """Откладывает текст вероятного дубля под кнопку «Создать всё равно».

        phone_asked=True запоминает, что вопрос о телефоне по этому диалогу
        уже задавался: обработка по кнопке не спросит номер второй раз.
        Той же транзакцией физически удаляются просроченные записи: полный
        текст заявки (имена, телефоны) не должен храниться дольше TTL.
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "DELETE FROM pending_texts WHERE created_at <= datetime('now', ?)",
                (f"-{PENDING_TEXT_TTL_SECONDS} seconds",),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO pending_texts "
                "(token, chat_id, user_id, text, dedup_key, phone_asked) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, chat_id, user_id, text, dedup_key, int(phone_asked)),
            )
            await conn.commit()

    async def get_pending_text(self, token: str) -> dict[str, Any] | None:
        """Отложенный текст по токену или None, если его нет либо он старше TTL.

        Только чтение (диагностика и тесты): аренду не берёт и запись не
        меняет, захватом и чисткой занимается claim_pending_text.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "SELECT chat_id, user_id, text, dedup_key, phone_asked FROM pending_texts "
                "WHERE token = ? AND created_at > datetime('now', ?)",
                (token, f"-{PENDING_TEXT_TTL_SECONDS} seconds",),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "chat_id": row[0],
            "user_id": row[1],
            "text": row[2],
            "dedup_key": row[3],
            "phone_asked": bool(row[4]),
        }

    async def claim_pending_text(self, token: str) -> tuple[dict[str, Any] | None, bool]:
        """Атомарно арендует отложенный текст под обработку (кнопка нажата).

        Возвращает пару (запись, занято):
        - (запись с claim_token владельца, False) — захват удался;
        - (None, True) — запись есть, но её прямо сейчас обрабатывает
          параллельное нажатие той же кнопки (живая аренда);
        - (None, False) — записи нет либо она была просрочена и только что
          физически удалена. Чистка просроченных строк выполняется в этой же
          транзакции, поэтому даже поздний клик по устаревшей кнопке не
          оставляет текст с именами и телефонами лежать в базе.

        Запись при захвате НЕ удаляется — удаление происходит только после
        успешной обработки (delete_pending_text), а сбой освобождает аренду
        (release_pending_text), и кнопка срабатывает повторно. Аренда старше
        CLAIM_TIMEOUT_SECONDS считается брошенной и перехватывается заново.
        """
        owner = uuid4().hex
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "DELETE FROM pending_texts WHERE created_at <= datetime('now', ?)",
                (f"-{PENDING_TEXT_TTL_SECONDS} seconds",),
            )
            cur = await conn.execute(
                "UPDATE pending_texts SET claimed_at = datetime('now'), claim_token = ? "
                "WHERE token = ? "
                "AND (claimed_at IS NULL OR claim_token IS NULL "
                "OR claimed_at < datetime('now', ?))",
                (owner, token, f"-{CLAIM_TIMEOUT_SECONDS} seconds"),
            )
            if cur.rowcount != 1:
                # Захват не прошёл: запись либо вычищена/отсутствует, либо её
                # держит живая аренда — вызывающему нужно различать эти исходы
                # («карточка устарела» против «обрабатываю»).
                cur = await conn.execute(
                    "SELECT 1 FROM pending_texts WHERE token = ?", (token,)
                )
                busy = await cur.fetchone() is not None
                await conn.commit()  # чистка просроченных сохраняется в любом случае
                return None, busy
            cur = await conn.execute(
                "SELECT chat_id, user_id, text, dedup_key, phone_asked "
                "FROM pending_texts WHERE token = ?",
                (token,),
            )
            row = await cur.fetchone()
            await conn.commit()
        if row is None:
            return None, False
        return {
            "chat_id": row[0],
            "user_id": row[1],
            "text": row[2],
            "dedup_key": row[3],
            "phone_asked": bool(row[4]),
            "claim_token": owner,
        }, False

    async def release_pending_text(self, token: str, claim_token: str) -> bool:
        """Снимает аренду после сбоя обработки (CAS): кнопка сработает повторно."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE pending_texts SET claimed_at = NULL, claim_token = NULL "
                "WHERE token = ? AND claim_token = ?",
                (token, claim_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def refresh_pending_claim(self, token: str, claim_token: str) -> bool:
        """Продлевает аренду отложенного текста и подтверждает владение (CAS).

        Вызывается сразу после разбора модели, до ответов пользователю и
        записей FSM. False означает, что аренда потеряна: её перехватили
        после зависания либо запись уже изъял победитель — обработчик обязан
        выйти тихо, ничего не отправляя и не меняя. Успешное продление
        сдвигает claimed_at к «сейчас»: до конца дедлайна force-flow
        (меньше таймаута аренды) перехват невозможен.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE pending_texts SET claimed_at = datetime('now') "
                "WHERE token = ? AND claim_token = ?",
                (token, claim_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def delete_pending_text(self, token: str, claim_token: str) -> bool:
        """Удаляет обработанный текст (CAS по владельцу аренды).

        Вызывается только после устойчивого перехода (пользователю отправлен
        итог обработки): до этого запись живёт под арендой и сбой её не теряет.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "DELETE FROM pending_texts WHERE token = ? AND claim_token = ?",
                (token, claim_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def finalize_pending_to_draft(
        self,
        token: str,
        claim_token: str,
        draft_id: str,
        chat_id: int,
        user_id: int,
        parsed_json: str,
        dedup_key: str,
    ) -> bool:
        """Fencing-переход: отложенный текст превращается в единственный черновик.

        Проверка владения арендой, удаление pending-строки и создание
        черновика — одна транзакция (BEGIN IMMEDIATE): compare-and-set по
        claim_token и DELETE совмещены в одном запросе. Обработчик, чью
        аренду перехватили после зависания (claim_pending_text по таймауту),
        CAS не пройдёт и получит False — черновик и карточку создаёт только
        текущий владелец, два конкурирующих нажатия физически не могут
        породить два черновика и две сделки, независимо от таймингов.
        Тот же принцип владения, что у drafts (claim_token + CAS).
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "DELETE FROM pending_texts WHERE token = ? AND claim_token = ?",
                (token, claim_token),
            )
            if cur.rowcount != 1:
                await conn.rollback()
                return False
            await conn.execute(
                "INSERT INTO drafts (draft_id, chat_id, user_id, parsed_json, dedup_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, chat_id, user_id, parsed_json, dedup_key),
            )
            await conn.commit()
            return True

    async def rollback_pending_draft(
        self,
        token: str,
        claim_token: str,
        draft_id: str,
        chat_id: int,
        user_id: int,
        text: str,
        dedup_key: str,
        phone_asked: bool,
    ) -> bool:
        """Одной транзакцией откатывает force-переход либо снимает аренду.

        Если pending уже превратился в ещё не захваченный open-черновик,
        черновик удаляется и исходный текст восстанавливается. Если переход
        не состоялся, освобождается только исходная pending-строка. Между
        удалением и восстановлением нет окна, в котором падение теряет оба.
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "DELETE FROM drafts WHERE draft_id = ? AND status = ? "
                "AND (claimed_at IS NULL OR claim_token IS NULL)",
                (draft_id, DRAFT_OPEN),
            )
            if cur.rowcount == 1:
                await conn.execute(
                    "INSERT OR IGNORE INTO pending_texts "
                    "(token, chat_id, user_id, text, dedup_key, phone_asked) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (token, chat_id, user_id, text, dedup_key, int(phone_asked)),
                )
                await conn.commit()
                return True
            cur = await conn.execute(
                "UPDATE pending_texts SET claimed_at = NULL, claim_token = NULL "
                "WHERE token = ? AND claim_token = ?",
                (token, claim_token),
            )
            await conn.commit()
            return cur.rowcount == 1

    # -- Постоянный fence записи сделки ------------------------------------

    async def claim_deal_fence(self, key: str, draft_id: str) -> dict[str, Any]:
        """Занимает общий idempotency key навсегда либо читает его владельца."""
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "INSERT OR IGNORE INTO deal_fences (idempotency_key, draft_id) "
                "VALUES (?, ?)",
                (key, draft_id),
            )
            # CRM-фазы относятся к неизменяемому снимку удалённого черновика.
            # При edit новый draft получает тот же ключ сделки, но начинает
            # contact/comment-путь заново; неоднозначные sent-фазы не
            # передаются никогда.
            await conn.execute(
                "UPDATE deal_fences SET draft_id = ?, status = 'reserved', "
                "deal_id = NULL, updated_at = datetime('now') "
                "WHERE idempotency_key = ? "
                "AND status IN ('reserved', 'contact_ready', 'comment_done') "
                "AND draft_id <> ? AND NOT EXISTS ("
                "SELECT 1 FROM drafts WHERE drafts.draft_id = deal_fences.draft_id)",
                (draft_id, key, draft_id),
            )
            cur = await conn.execute(
                "SELECT draft_id, status, deal_id FROM deal_fences "
                "WHERE idempotency_key = ?",
                (key,),
            )
            row = await cur.fetchone()
            await conn.commit()
        return {
            "owned": bool(row and row[0] == draft_id),
            "draft_id": row[0] if row else None,
            "status": row[1] if row else None,
            "deal_id": row[2] if row else None,
        }

    async def mark_deal_fence_contact_sent(self, key: str, draft_id: str) -> bool:
        """Фиксирует границу непосредственно перед contact.add."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = 'contact_sent', "
                "updated_at = datetime('now') WHERE idempotency_key = ? "
                "AND draft_id = ? AND status = 'reserved'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def settle_deal_fence_contact(
        self,
        key: str,
        draft_id: str,
        claim_token: str,
        contact_id: int,
        created: bool,
    ) -> bool:
        """Атомарно сохраняет контакт и отдельную фазу будущего комментария."""
        expected = "contact_sent" if created else "reserved"
        target = "comment_done" if created else "contact_ready"
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            draft_cur = await conn.execute(
                "UPDATE drafts SET contact_id = ? WHERE draft_id = ? "
                "AND claim_token = ? AND status = 'open'",
                (contact_id, draft_id, claim_token),
            )
            fence_cur = await conn.execute(
                "UPDATE deal_fences SET status = ?, updated_at = datetime('now') "
                "WHERE idempotency_key = ? AND draft_id = ? AND status = ?",
                (target, key, draft_id, expected),
            )
            if draft_cur.rowcount != 1 or fence_cur.rowcount != 1:
                await conn.rollback()
                return False
            await conn.commit()
            return True

    async def mark_deal_fence_comment_sent(self, key: str, draft_id: str) -> bool:
        """Ставит постоянную границу перед timeline.comment.add."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = 'comment_sent', "
                "updated_at = datetime('now') WHERE idempotency_key = ? "
                "AND draft_id = ? AND status = 'contact_ready'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def settle_deal_fence_comment(self, key: str, draft_id: str) -> bool:
        """Фиксирует успешный timeline-комментарий отдельной фазой."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = 'comment_done', "
                "updated_at = datetime('now') WHERE idempotency_key = ? "
                "AND draft_id = ? AND status = 'comment_sent'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def skip_deal_fence_comment(self, key: str, draft_id: str) -> bool:
        """Закрывает фазу, когда для контакта нет текста комментария."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = 'comment_done', "
                "updated_at = datetime('now') WHERE idempotency_key = ? "
                "AND draft_id = ? AND status = 'contact_ready'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def mark_deal_fence_sent(self, key: str, draft_id: str) -> bool:
        """Необратимо отмечает точку непосредственно перед deal.add."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = 'sent', updated_at = datetime('now') "
                "WHERE idempotency_key = ? AND draft_id = ? AND status = 'comment_done'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def release_deal_fence(self, key: str, draft_id: str) -> bool:
        """Освобождает fence только пока deal.add заведомо не отправлялся."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "DELETE FROM deal_fences WHERE idempotency_key = ? "
                "AND draft_id = ? AND status = 'reserved'",
                (key, draft_id),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def reset_deal_fence(
        self, key: str, draft_id: str, rejected_phase: str
    ) -> bool:
        """Откатывает только конкретную доказанно отклонённую unsafe-запись."""
        targets = {
            "contact_sent": "reserved",
            "comment_sent": "contact_ready",
            "sent": "comment_done",
        }
        target = targets.get(rejected_phase)
        if target is None:
            raise ValueError(f"Неизвестная unsafe-фаза: {rejected_phase}")
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE deal_fences SET status = ?, updated_at = datetime('now') "
                "WHERE idempotency_key = ? AND draft_id = ? "
                "AND status = ?",
                (target, key, draft_id, rejected_phase),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def get_or_create_task_fence(self, key: str) -> dict[str, Any]:
        """Создаёт постоянный fence напоминания и возвращает его фазу."""
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO task_fences (idempotency_key) VALUES (?)",
                (key,),
            )
            cur = await conn.execute(
                "SELECT status, task_id FROM task_fences WHERE idempotency_key = ?",
                (key,),
            )
            row = await cur.fetchone()
            await conn.commit()
        return {"status": row[0], "task_id": row[1]}

    async def mark_task_fence_sent(self, key: str) -> bool:
        """Ровно один обработчик получает право отправить task.add."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE task_fences SET status = 'sent', updated_at = datetime('now') "
                "WHERE idempotency_key = ? AND status = 'reserved'",
                (key,),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def reset_task_fence(self, key: str) -> bool:
        """Разрешает повтор после явного отказа сервера на task.add."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE task_fences SET status = 'reserved', updated_at = datetime('now') "
                "WHERE idempotency_key = ? AND status = 'sent'",
                (key,),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def complete_task_fence(self, key: str, task_id: int) -> None:
        """Фиксирует найденную или созданную задачу по постоянному ключу."""
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT INTO task_fences (idempotency_key, status, task_id) "
                "VALUES (?, 'done', ?) ON CONFLICT(idempotency_key) DO UPDATE SET "
                "status = 'done', task_id = excluded.task_id, updated_at = datetime('now')",
                (key, task_id),
            )
            await conn.commit()

    # -- Черновики заявок (карточки-превью) --------------------------------

    async def save_draft(
        self,
        draft_id: str,
        chat_id: int,
        user_id: int,
        parsed_json: str,
        dedup_key: str = "",
    ) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO drafts "
                "(draft_id, chat_id, user_id, parsed_json, dedup_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, chat_id, user_id, parsed_json, dedup_key),
            )
            await conn.commit()

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Черновик по ID или None, если его нет либо он старше TTL.

        Возвращает и status с deal_id: обработчик кнопок ветвится по
        состоянию черновика (open / creation_unknown / done).
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "SELECT chat_id, user_id, parsed_json, dedup_key, contact_id, status, deal_id "
                "FROM drafts WHERE draft_id = ? "
                "AND created_at > datetime('now', ?)",
                (draft_id, f"-{DRAFT_TTL_SECONDS} seconds"),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "draft_id": draft_id,
            "chat_id": row[0],
            "user_id": row[1],
            "parsed_json": row[2],
            "dedup_key": row[3],
            "contact_id": row[4],
            "status": row[5],
            "deal_id": row[6],
        }

    async def claim_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Атомарно захватывает черновик под запись в CRM.

        Возвращает черновик с полем claim_token (владелец аренды), если
        захват удался. None означает, что черновик уже захвачен параллельным
        нажатием «Создать», удалён или старше TTL — второе нажатие ничего
        не пишет в CRM и дублей сделок не будет.

        Захват старше CLAIM_TIMEOUT_SECONDS считается брошенным (процесс
        убили между захватом и завершением черновика) и перехватывается
        заново с новым токеном: старый владелец после этого не пройдёт ни
        одну compare-and-set-операцию.

        Захватываются только черновики в статусе open: по терминальным
        (done, creation_unknown) новый deal.add невозможен ни при каких
        обстоятельствах, даже после истечения аренды.
        """
        token = uuid4().hex
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE drafts SET claimed_at = datetime('now'), claim_token = ? "
                "WHERE draft_id = ? AND status = ? "
                "AND (claimed_at IS NULL OR claim_token IS NULL "
                "OR claimed_at < datetime('now', ?)) "
                "AND created_at > datetime('now', ?)",
                (
                    token,
                    draft_id,
                    DRAFT_OPEN,
                    f"-{CLAIM_TIMEOUT_SECONDS} seconds",
                    f"-{DRAFT_TTL_SECONDS} seconds",
                ),
            )
            if cur.rowcount != 1:
                await conn.commit()
                return None
            cur = await conn.execute(
                "SELECT chat_id, user_id, parsed_json, dedup_key, contact_id "
                "FROM drafts WHERE draft_id = ?",
                (draft_id,),
            )
            row = await cur.fetchone()
            await conn.commit()
        if row is None:
            return None
        return {
            "draft_id": draft_id,
            "chat_id": row[0],
            "user_id": row[1],
            "parsed_json": row[2],
            "dedup_key": row[3],
            "contact_id": row[4],
            "claim_token": token,
        }

    async def release_draft(self, draft_id: str, token: str) -> bool:
        """Снимает захват после ошибки CRM, только если аренда всё ещё наша.

        Возвращает False, если аренду уже перехватил другой воркер —
        снимать чужую аренду нельзя.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE drafts SET claimed_at = NULL, claim_token = NULL "
                "WHERE draft_id = ? AND claim_token = ?",
                (draft_id, token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def refresh_claim(self, draft_id: str, token: str) -> bool:
        """Продлевает захват на время долгой записи в CRM (compare-and-set).

        False означает, что аренда потеряна (перехвачена другим воркером
        или черновик удалён) — владелец должен прекратить запись в CRM.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE drafts SET claimed_at = datetime('now') "
                "WHERE draft_id = ? AND claim_token = ?",
                (draft_id, token),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def set_draft_contact(
        self, draft_id: str, contact_id: int, token: str | None = None
    ) -> bool:
        """Запоминает контакт черновика, чтобы retry не создавал второй.

        С токеном пишет только в свою аренду (compare-and-set), без токена —
        по draft_id, как раньше.
        """
        async with aiosqlite.connect(self.path) as conn:
            if token is None:
                cur = await conn.execute(
                    "UPDATE drafts SET contact_id = ? WHERE draft_id = ?",
                    (contact_id, draft_id),
                )
            else:
                cur = await conn.execute(
                    "UPDATE drafts SET contact_id = ? WHERE draft_id = ? AND claim_token = ?",
                    (contact_id, draft_id, token),
                )
            await conn.commit()
            return cur.rowcount == 1

    async def begin_edit(self, draft_id: str) -> dict[str, Any] | None:
        """Атомарно изымает черновик в редактирование (кнопка «Изменить»).

        Проверка аренды и изъятие строки идут одной транзакцией
        (BEGIN IMMEDIATE): гонку с параллельным claim_draft решает блокировка
        записи SQLite, побеждает ровно один. Отдельная проверка «активно ли
        захвачен» с последующим входом в FSM оставляла бы зазор, в котором
        «Создать» успевает начать запись в CRM по уже редактируемым данным.

        Возвращает данные черновика, строка при этом удаляется: карточка
        больше не действует, заявка продолжается в опроснике и завершится
        новой карточкой. None — черновик захвачен «Создать», не в статусе
        open, устарел или его нет: редактирование отклоняется.
        """
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT chat_id, user_id, parsed_json, dedup_key, contact_id "
                "FROM drafts WHERE draft_id = ? AND status = ? "
                "AND created_at > datetime('now', ?) "
                "AND (claimed_at IS NULL OR claim_token IS NULL "
                "OR claimed_at < datetime('now', ?))",
                (
                    draft_id,
                    DRAFT_OPEN,
                    f"-{DRAFT_TTL_SECONDS} seconds",
                    f"-{CLAIM_TIMEOUT_SECONDS} seconds",
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.rollback()
                return None
            await conn.execute("DELETE FROM drafts WHERE draft_id = ?", (draft_id,))
            await conn.commit()
        return {
            "draft_id": draft_id,
            "chat_id": row[0],
            "user_id": row[1],
            "parsed_json": row[2],
            "dedup_key": row[3],
            "contact_id": row[4],
        }

    async def mark_draft_unknown(self, draft_id: str, token: str, key: str) -> bool:
        """Замораживает черновик после неоднозначного исхода deal.add.

        Неоднозначен любой сбой самого deal.add: таймаут, обрыв соединения,
        отмена задачи — во всех случаях запрос мог дойти до сервера.
        Сделка могла быть создана без ответа и ещё не видна в поиске, поэтому
        аренда НЕ освобождается для нового deal.add: статус creation_unknown
        навсегда исключает захват черновика, повторные «Создать» только
        сверяются с CRM. Ключ идемпотентности запоминается в dedup_key, чтобы
        сверка шла по тому же ключу, с которым мог пройти deal.add.

        Осознанный размен: если deal.add на самом деле не прошёл, карточка
        «залипает» и заявку придётся отправить заново новым сообщением, зато
        дубль сделки исключён. Работает только со своим токеном аренды и
        только по open-черновику: черновик, уже зафиксированный как done
        (гонка отмены с complete_draft под shield), обратно не понижается.
        """
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "UPDATE drafts SET status = ?, dedup_key = ? "
                "WHERE draft_id = ? AND claim_token = ? AND status = ?",
                (DRAFT_UNKNOWN, key, draft_id, token, DRAFT_OPEN),
            )
            await conn.commit()
            return cur.rowcount == 1

    async def complete_draft(
        self, draft_id: str, key: str, deal_id: int, token: str | None = None
    ) -> bool:
        """Фиксирует созданную сделку: tombstone-черновик и processed.deal_id.

        Обе записи идут одной транзакцией, подтверждение пользователю шлётся
        только после её коммита: если отправка упадёт, факт создания уже
        персистентен — повторное нажатие по карточке ответит номером сделки,
        а дубль исходного сообщения — «уже создана заявка №N».

        Первым выполняется compare-and-set черновика: с токеном — по владельцу
        аренды (путь «Создать»), без токена закрывается только черновик в
        creation_unknown (путь сверки). Если CAS не прошёл (аренда потеряна
        или статус чужой), транзакция откатывается целиком и processed не
        трогается — половинчатого состояния (deal_id в processed при живом
        open-черновике) не бывает. True — оба обновления зафиксированы.

        Запись в processed — UPSERT: если ключ был удалён (middleware
        освободил его после сбоя в потоке обработки сообщения), строка
        создаётся заново с deal_id — дедуп повторной доставки того же
        сообщения не теряет номер сделки. proc_token у новой строки
        остаётся NULL: это уже done-факт, перехватывать в нём нечего.
        """
        async with aiosqlite.connect(self.path) as conn:
            if token is None:
                cur = await conn.execute(
                    "UPDATE drafts SET status = ?, deal_id = ? "
                    "WHERE draft_id = ? AND status = ?",
                    (DRAFT_DONE, deal_id, draft_id, DRAFT_UNKNOWN),
                )
            else:
                cur = await conn.execute(
                    "UPDATE drafts SET status = ?, deal_id = ? "
                    "WHERE draft_id = ? AND claim_token = ?",
                    (DRAFT_DONE, deal_id, draft_id, token),
                )
            if cur.rowcount != 1:
                await conn.rollback()
                return False
            await conn.execute(
                "INSERT INTO processed (key, deal_id, status) VALUES (?, ?, 'done') "
                "ON CONFLICT(key) DO UPDATE SET deal_id = excluded.deal_id, status = 'done'",
                (key, deal_id),
            )
            # Номер сделки дописывается и в контент-дедуп: повтор того же
            # текста в окне 24 часов сможет назвать номер заявки. Захвата
            # может не быть (кнопка «Создать всё равно» его не держит) —
            # тогда обновлять нечего, rowcount здесь не проверяется.
            await conn.execute(
                "UPDATE content_claims SET deal_id = ? WHERE dedup_key = ?",
                (deal_id, key),
            )
            await conn.execute(
                "INSERT INTO deal_fences (idempotency_key, draft_id, status, deal_id) "
                "VALUES (?, ?, 'done', ?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET status = 'done', "
                "deal_id = excluded.deal_id, updated_at = datetime('now')",
                (key, draft_id, deal_id),
            )
            await conn.commit()
            return True

    async def delete_draft(self, draft_id: str, token: str | None = None) -> bool:
        """Удаляет черновик (compare-and-set).

        С токеном — только свою аренду. Без токена (путь «Отмена») — только
        обычный (open) не захваченный либо просроченный черновик: пока
        «Создать» активно пишет в CRM, отмена черновик не удаляет, иначе
        «Отменено» соврало бы — сделка всё равно записалась бы. Терминальные
        черновики (done, creation_unknown) отмена тоже не трогает: по ним
        сделка создана или её судьба ещё выясняется. Проверка и удаление —
        один атомарный DELETE, зазора между ними нет.
        """
        async with aiosqlite.connect(self.path) as conn:
            if token is None:
                cur = await conn.execute(
                    "DELETE FROM drafts WHERE draft_id = ? AND status = ? "
                    "AND (claimed_at IS NULL OR claim_token IS NULL "
                    "OR claimed_at < datetime('now', ?))",
                    (draft_id, DRAFT_OPEN, f"-{CLAIM_TIMEOUT_SECONDS} seconds"),
                )
            else:
                cur = await conn.execute(
                    "DELETE FROM drafts WHERE draft_id = ? AND claim_token = ?",
                    (draft_id, token),
                )
            await conn.commit()
            return cur.rowcount == 1
