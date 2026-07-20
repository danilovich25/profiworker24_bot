"""Настройки приложения. Все значения берутся из переменных окружения или .env."""

import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    bitrix_webhook: str = ""
    yc_api_key: str = ""
    yc_folder_id: str = ""
    sentry_dsn: str = ""
    db_path: str = "/data/bot.db"
    allowed_tg_ids: str = ""
    # Явный флаг «пускать всех» для staging/отладки. Без него пустой
    # ALLOWED_TG_IDS означает «не пускать никого» (fail-closed на проде).
    allow_all_users: bool = False
    tz: str = "Asia/Vladivostok"
    # Ответственный за задачи и дела-напоминания в Bitrix24. Должен быть
    # пользователем ЗАКАЗЧИКА (mobile-push уходит ответственному), на портале
    # заказчика это пользователь id=1 — он же владелец вебхука.
    bitrix_responsible_id: int = 1

    @property
    def allowed_ids(self) -> set[int]:
        """Разрешённые Telegram ID из ALLOWED_TG_IDS.

        Разделители — запятая, точка с запятой или пробелы. Непустая строка
        без единого числового ID (например, опечатка "abc") — ошибка
        конфигурации: лучше упасть на старте, чем молча закрыть доступ
        нужным людям или открыть его не тем.
        """
        raw = self.allowed_tg_ids.strip()
        if not raw:
            return set()
        ids = {int(part) for part in re.split(r"[,;\s]+", raw) if part.isdigit()}
        if not ids:
            raise ValueError(
                f"ALLOWED_TG_IDS не содержит ни одного числового Telegram ID: {raw!r}. "
                "Укажите ID через запятую или очистите переменную."
            )
        return ids


settings = Settings()
