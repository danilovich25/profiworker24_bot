"""Точка входа: сборка бота и запуск long polling."""

import asyncio
import logging

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from app.config import settings
from app.db import Database
from app.handlers import routers
from app.middlewares.dedup import DedupMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.pruning_isolation import PruningEventIsolation
from app.middlewares.rate_limit import RateLimitMiddleware
from app.middlewares.whitelist import WhitelistMiddleware
from app.sentry_setup import _scrub_string, init_sentry
from app.services.bitrix import ensure_sources, ensure_uf_fields, get_bitrix
from app.services.tasks import reminder_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")


class _PiiFormatter(logging.Formatter):
    """Форматер логов: маскирует телефоны и URL-секреты в message и traceback."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return _scrub_string(formatted)


_pii_formatter = _PiiFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_pii_formatter)

HEALTHCHECK_INTERVAL = 3600

# Главное меню команд (кнопка «Меню» в Telegram)
BOT_COMMANDS = [
    BotCommand(command="new", description="Новая заявка"),
    BotCommand(command="find", description="Найти заявку"),
    BotCommand(command="last", description="Последние заявки"),
    BotCommand(command="help", description="Помощь"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует команды главного меню. Сбой не мешает запуску бота."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Не удалось установить команды меню")


async def healthcheck_loop(db: Database) -> None:
    """Периодическая самопроверка и чистка просроченных данных.

    Чистка по таймеру обязательна: просроченный отложенный текст заявки
    (таблица pending_texts) содержит имена и телефоны клиентов и должен
    физически исчезать из базы, даже когда в боте нет никакого трафика —
    никто не жмёт кнопки, не пишет и бот не перезапускается. Ошибка чистки
    не роняет цикл: следующая попытка через HEALTHCHECK_INTERVAL.
    """
    while True:
        try:
            await db.purge_expired()
        except Exception:
            logger.exception("Не удалось вычистить просроченные записи")
        logger.info("healthcheck: alive")
        await asyncio.sleep(HEALTHCHECK_INTERVAL)


async def init_bitrix(webhook: str, sentry_active: bool = False):
    """Клиент Bitrix24 или None, если CRM не настроена либо её поля не готовы.

    Ошибка подготовки UF-полей означает, что каждая запись заявки будет
    падать. В этом случае CRM отключается целиком: бот продолжает работать,
    а на «Создать» честно отвечает «CRM не подключена», не плодя битые сделки.
    """
    if not webhook or webhook == "PENDING":
        logger.warning("BITRIX_WEBHOOK не задан, работаю без CRM")
        return None
    bx = get_bitrix(webhook)
    try:
        await ensure_uf_fields(bx)
    except Exception:
        logger.exception(
            "Поля CRM в Bitrix24 не готовы — отключаю CRM, заявки записываться не будут"
        )
        if sentry_active:
            sentry_sdk.capture_message(
                "CRM отключена: поля Bitrix24 не готовы", level="warning"
            )
        return None
    try:
        # Справочник источников не критичен: сделка запишется и без него,
        # поэтому сбой только логируется и CRM не отключает.
        await ensure_sources(bx)
    except Exception:
        logger.exception("Справочник источников Bitrix24 не обновлён — продолжаю без него")
    return bx


def create_dispatcher(
    db: Database,
    bitrix=None,
    allowed_ids: set[int] | None = None,
    allow_all: bool | None = None,
) -> Dispatcher:
    """Собирает диспетчер."""
    # Один чат меняет FSM строго последовательно. Иначе быстрый следующий
    # ответ успевает попасть в старый state, пока предыдущий обработчик ждёт
    # отправку вопроса, а долгий голос или поиск поздно перезаписывает новый flow.
    dp = Dispatcher(events_isolation=PruningEventIsolation())
    whitelist = WhitelistMiddleware(db, allowed_ids, allow_all)
    dp.message.outer_middleware(whitelist)
    dp.callback_query.outer_middleware(whitelist)
    dp.message.outer_middleware(LoggingMiddleware())
    dp.message.outer_middleware(RateLimitMiddleware())
    dp.message.outer_middleware(DedupMiddleware(db))
    for router in routers:
        dp.include_router(router)
    dp["db"] = db
    dp["bitrix"] = bitrix
    return dp


async def main() -> None:
    sentry_active = init_sentry(settings.sentry_dsn)
    if sentry_active:
        logger.info("Sentry инициализирован")

    token = settings.bot_token
    if not token or token == "PENDING":
        logger.warning("BOT_TOKEN не задан, жду настройки окружения")
        while True:
            await asyncio.sleep(HEALTHCHECK_INTERVAL)

    # Проверка списка доступа на старте: битый ALLOWED_TG_IDS (непустой, но
    # без единого числового ID) роняет бота сразу — это ошибка конфигурации.
    allowed_ids = settings.allowed_ids
    if not allowed_ids:
        if settings.allow_all_users:
            logger.warning("ALLOW_ALL_USERS=true: доступ открыт всем (режим staging/отладки)")
        else:
            logger.warning(
                "ALLOWED_TG_IDS пуст: доступ закрыт для всех. "
                "Заполните список ID или включите ALLOW_ALL_USERS=true для отладки."
            )

    db = Database(settings.db_path)
    await db.init()

    bx = await init_bitrix(settings.bitrix_webhook, sentry_active)

    dp = create_dispatcher(db, bitrix=bx)

    @dp.error()
    async def on_error(event) -> bool:
        """Глобальный хендлер исключений: пишет в Sentry, возвращает True чтобы поллинг не падал."""
        exception = getattr(event, "exception", None)
        if exception is not None and sentry_active:
            sentry_sdk.capture_exception(exception)
        if exception is not None:
            logger.error(
                "Необработанное исключение в хендлере",
                exc_info=(type(exception), exception, exception.__traceback__),
            )
        else:
            logger.error("Необработанное исключение в хендлере (без exception)")
        return True

    bot = Bot(token=token)
    await setup_bot_commands(bot)
    if sentry_active:
        sentry_sdk.capture_message("bot startup", level="info")
    asyncio.create_task(healthcheck_loop(db))
    # Telegram-напоминания: очередь в SQLite, поэтому цикл спокойно
    # переживает рестарт контейнера — неотправленное уйдёт после подъёма.
    asyncio.create_task(reminder_loop(bot, db))
    logger.info("Запускаю polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
