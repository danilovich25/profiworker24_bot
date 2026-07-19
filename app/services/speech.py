"""Распознавание голосовых сообщений (SpeechKit v1, синхронный REST).

Синхронное распознавание: POST байтов OggOpus на stt:recognize. Лимиты
метода — до 30 секунд звука и до 1 МБ данных, поэтому размер проверяется
до похода в сеть (длительность проверяет хендлер по метаданным Telegram).

Любая проблема (не заданы ключи, сеть, не-200 ответ, битый JSON)
превращается в SpeechUnavailable — хендлер отвечает мягкой подсказкой
«пришлите текстом» и не роняет апдейт. Пустой результат (тишина)
возвращается пустой строкой.
"""

import logging

import httpx

from app.config import settings

log = logging.getLogger("bot.speech")

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

# Лимиты синхронного распознавания SpeechKit v1
MAX_DURATION_SECONDS = 30
MAX_SIZE_BYTES = 1024 * 1024

REQUEST_TIMEOUT = 30  # секунд на один запрос распознавания


class SpeechUnavailable(Exception):
    """Распознавание речи недоступно."""


async def recognize_ogg(data: bytes) -> str:
    """Распознаёт голосовое сообщение (OggOpus) в текст."""
    if not settings.yc_api_key or not settings.yc_folder_id:
        raise SpeechUnavailable("не заданы YC_API_KEY/YC_FOLDER_ID")
    if len(data) > MAX_SIZE_BYTES:
        raise SpeechUnavailable("файл больше 1 МБ")

    params = {"folderId": settings.yc_folder_id, "lang": "ru-RU", "format": "oggopus"}
    headers = {"Authorization": f"Api-Key {settings.yc_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(STT_URL, params=params, headers=headers, content=data)
    except Exception as exc:  # noqa: BLE001 - сеть/таймаут = недоступность
        log.warning("SpeechKit недоступен: %s", type(exc).__name__)
        raise SpeechUnavailable(str(exc)) from exc

    if response.status_code != 200:
        # Тело ошибки не логируется целиком: достаточно статуса.
        log.warning("SpeechKit ответил статусом %s", response.status_code)
        raise SpeechUnavailable(f"статус {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpeechUnavailable("невалидный ответ распознавания") from exc
    return str(payload.get("result") or "").strip()
