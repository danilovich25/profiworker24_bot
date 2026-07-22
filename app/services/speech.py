"""Распознавание голосовых сообщений (SpeechKit v1, синхронный REST).

Синхронное распознавание: POST байтов OggOpus на stt:recognize. Лимиты
метода — до 30 секунд звука и до 1 МБ данных на запрос. Бот принимает
голосовые до минуты (просьба заказчика): длинное аудио режется ffmpeg на
куски короче лимита (recognize_voice), каждый распознаётся отдельно, текст
склеивается. Размер проверяется до похода в сеть (длительность проверяет
хендлер по метаданным Telegram).

Любая проблема (не заданы ключи, сеть, не-200 ответ, битый JSON, сбой
нарезки) превращается в SpeechUnavailable — хендлер отвечает мягкой
подсказкой «пришлите текстом» и не роняет апдейт. Пустой результат (тишина)
возвращается пустой строкой.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger("bot.speech")

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

# Лимиты ОДНОГО запроса синхронного распознавания SpeechKit v1
MAX_DURATION_SECONDS = 30
MAX_SIZE_BYTES = 1024 * 1024

# Лимиты голосового для БОТА: длиннее лимита SpeechKit — режем на куски.
# Минута опуса Telegram весит сотни килобайт, 2 МБ хватает с запасом;
# жёсткий предел нужен, чтобы гигантский файл не оседал в памяти.
BOT_MAX_DURATION_SECONDS = 60
BOT_MAX_SIZE_BYTES = 2 * 1024 * 1024

# Длина куска нарезки: короче лимита SpeechKit с запасом на неточность
# сегментации по границам opus-пакетов.
SEGMENT_SECONDS = 25

REQUEST_TIMEOUT = 30  # секунд на один запрос распознавания

# Дедлайн нарезки ffmpeg: минутный опус кодируется за доли секунды, зависший
# процесс не должен держать обработчик.
FFMPEG_TIMEOUT = 30


class SpeechUnavailable(Exception):
    """Распознавание речи недоступно."""


async def _split_ogg(data: bytes) -> list[bytes]:
    """Режет OggOpus на куски по SEGMENT_SECONDS (ffmpeg, перекодирование).

    Сегменты перекодируются в opus заново (-c:a libopus): копирование потока
    режет только по границам страниц Ogg и может отдать кусок с битым
    началом. Любой сбой — нет бинаря, ненулевой код выхода, пустой результат
    — превращается в SpeechUnavailable: хендлер ответит мягкой подсказкой.
    """
    with tempfile.TemporaryDirectory(prefix="voice-split-") as tmp:
        source = Path(tmp) / "in.ogg"
        source.write_bytes(data)
        pattern = Path(tmp) / "out_%03d.ogg"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-f",
                "segment",
                "-segment_time",
                str(SEGMENT_SECONDS),
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(pattern),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(FFMPEG_TIMEOUT):
                _, stderr = await proc.communicate()
        except TimeoutError as exc:
            proc.kill()
            raise SpeechUnavailable("ffmpeg завис на нарезке") from exc
        except Exception as exc:  # noqa: BLE001 - нет бинаря, ОС отказала
            raise SpeechUnavailable(f"нарезка недоступна: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            log.warning(
                "ffmpeg не разрезал голосовое (код %s): %s",
                proc.returncode,
                (stderr or b"")[:200],
            )
            raise SpeechUnavailable("ffmpeg не разрезал голосовое")
        chunks = [path.read_bytes() for path in sorted(Path(tmp).glob("out_*.ogg"))]
    if not chunks or not all(chunks):
        raise SpeechUnavailable("нарезка вернула пустые куски")
    return chunks


async def recognize_voice(data: bytes, duration: int) -> str:
    """Распознаёт голосовое до BOT_MAX_DURATION_SECONDS.

    Короткое (в лимитах одного запроса SpeechKit) уходит как есть; длинное
    режется на куски и распознаётся по частям, куски склеиваются пробелом.
    Куски распознаются последовательно: их максимум три, а параллельные
    запросы упирались бы в квоту SpeechKit без выигрыша во времени.
    """
    if len(data) > BOT_MAX_SIZE_BYTES:
        raise SpeechUnavailable(f"файл больше {BOT_MAX_SIZE_BYTES} байт")
    # Граница «без нарезки» строгая: Telegram округляет длительность ВНИЗ,
    # и файл с duration=30 может реально длиться 30.9с — целиком SpeechKit
    # его отверг бы.
    if duration < MAX_DURATION_SECONDS and len(data) <= MAX_SIZE_BYTES:
        return await recognize_ogg(data)
    parts = []
    for chunk in await _split_ogg(data):
        recognized = await recognize_ogg(chunk)
        if recognized:
            parts.append(recognized)
    return " ".join(parts)


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
