"""Настройка Sentry с очисткой PII."""

import logging
import re
from typing import Any

import sentry_sdk

PHONE_MASK = "[PHONE]"
PII_MASK = "[PII]"
# Телефон: ран из 10+ цифр, разделённых пробелами, скобками, точками, запятыми,
# слэшами, точками с запятой и разными видами тире (U+2010..U+2015, минус U+2212).
# Верхней границы намеренно нет: склеенные или сверхдлинные цепочки должны
# схлопываться в одну маску целиком, чтобы ни одна цифра не пережила скраб.
PHONE_RE = re.compile(r"\+?\d(?:[\s\\/().,;\[\]\-‐-―−]*\d){9,}")
BITRIX_URL_RE = re.compile(r"/rest/\d+/[^/\s]+")
TG_BOT_URL_RE = re.compile(r"/bot[^/\s]+")
PII_KEYS = frozenset({
    "phone", "client_name", "address", "org",
    "телефон", "тел", "клиент", "клиент_имя", "имя_клиента",
    "фио", "имя", "адрес", "организация", "компания",
})
# Точные пути системной телеметрии, где телефонный скраб отключён.
# Исключение получает ТОЛЬКО скалярная строка, стоящая непосредственно
# значением dict-ключа на таком пути (см. _scrub_dict). URL-скраб действует везде.
RAW_PATHS = frozenset({
    ("event_id",),
    ("timestamp",),
    ("start_timestamp",),
    ("level",),
    ("logger",),
    ("platform",),
    ("release",),
    ("environment",),
    ("dist",),
    ("server_name",),
    ("modules",),
    ("sdk", "name"),
    ("sdk", "version"),
    ("sdk", "packages", "name"),
    ("sdk", "packages", "version"),
    ("contexts", "trace", "trace_id"),
    ("contexts", "trace", "span_id"),
    ("contexts", "trace", "parent_span_id"),
    ("contexts", "trace", "op"),
    ("contexts", "trace", "status"),
    ("contexts", "runtime", "name"),
    ("contexts", "runtime", "version"),
    ("contexts", "runtime", "build"),
    ("contexts", "browser", "name"),
    ("contexts", "browser", "version"),
    ("contexts", "os", "name"),
    ("contexts", "os", "version"),
    ("contexts", "os", "kernel_version"),
    ("contexts", "os", "build"),
    ("contexts", "device", "family"),
    ("contexts", "device", "model"),
    ("contexts", "device", "manufacturer"),
    ("contexts", "device", "arch"),
    ("contexts", "app", "build_type"),
    ("contexts", "app", "app_version"),
    ("contexts", "app", "app_build"),
    ("debug_meta", "images", "debug_id"),
    ("debug_meta", "images", "image_addr"),
    ("debug_meta", "images", "image_vmaddr"),
    ("debug_meta", "images", "image_size"),
    ("debug_meta", "images", "instruction_addr"),
    ("debug_meta", "images", "code_id"),
    ("debug_meta", "images", "arch"),
    ("debug_meta", "images", "type"),
})
# Невидимые символы (soft hyphen, bidi-маркеры, zero-width и т.п.):
# вырезаются из значений и ключей ДО всех остальных проверок.
_ZERO_WIDTH = str.maketrans("", "", (
    "­؜᠎"
    "​‌‍‎‏"
    "⁠⁡⁢⁣⁤"
    "⁦⁧⁨⁩"
    "﻿"
))


def _is_pii_key(key: object) -> bool:
    """PII-ключ вне зависимости от регистра и невидимых символов."""
    if not isinstance(key, str):
        return False
    return key.translate(_ZERO_WIDTH).casefold() in PII_KEYS


def _scrub_string(value: str, phone_exempt: bool = False) -> str:
    """Очищает строку: URL-секреты режутся всегда, телефоны — если нет исключения.

    phone_exempt=True выставляет только _scrub_dict для скалярной строки,
    лежащей точно на whitelist-пути из RAW_PATHS.
    """
    value = value.translate(_ZERO_WIDTH)
    value = BITRIX_URL_RE.sub("/rest/[REDACTED]", value)
    value = TG_BOT_URL_RE.sub("/bot[REDACTED]", value)
    if phone_exempt:
        return value
    return PHONE_RE.sub(PHONE_MASK, value)


def _unique_key(result: dict, key: str) -> str:
    """Возвращает уникальный ключ на случай коллизий после скраба."""
    if key not in result:
        return key
    n = 1
    while f"{key}#{n}" in result:
        n += 1
    return f"{key}#{n}"


def _scrub_value(
    value: Any,
    path: tuple = (),
    _visited: set[int] | None = None,
) -> Any:
    """Рекурсивно очищает значение с защитой от циклов.

    Строка, до которой добрались здесь (в том числе элемент списка или кортежа),
    скрабится ВСЕГДА: исключение по whitelist-пути даёт только _scrub_dict.
    Путь при этом протягивается сквозь списки, чтобы dict-элементы вроде
    sdk.packages[].version сохраняли легитимные значения.
    """
    if _visited is None:
        _visited = set()

    if isinstance(value, (dict, list, tuple)):
        if id(value) in _visited:
            return "<cycle>"
        _visited = _visited | {id(value)}

    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return _scrub_dict(value, path, _visited)
    if isinstance(value, list):
        return [_scrub_value(item, path, _visited) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, path, _visited) for item in value)
    return value


def _scrub_dict(data: dict, path: tuple, _visited: set[int]) -> dict:
    """Очищает словарь: PII-ключи маскируются целиком, коллизии ключей разводятся.

    Исключение из телефонного скраба выдаётся здесь и только здесь: на стыке
    dict-ключа и скалярной строки, чей полный путь точно входит в RAW_PATHS.
    Нестроковый ключ продолжает путь собой и потому никогда не совпадёт
    с whitelist-путём (там только строки).
    """
    result = {}
    for key, value in data.items():
        scrubbed_key = _scrub_string(key) if isinstance(key, str) else key
        child_path = path + (key,)
        unique = _unique_key(result, scrubbed_key)
        if _is_pii_key(key):
            result[unique] = PII_MASK
        elif isinstance(value, str):
            result[unique] = _scrub_string(value, phone_exempt=child_path in RAW_PATHS)
        else:
            result[unique] = _scrub_value(value, child_path, _visited)
    return result


def scrub_pii(event: dict, hint: dict | None) -> dict | None:
    """Очищает событие Sentry от PII перед отправкой."""
    if not isinstance(event, dict):
        return event
    return _scrub_dict(event, (), {id(event)})


def init_sentry(dsn: str) -> bool:
    """Инициализирует Sentry SDK с заданным DSN."""
    if not dsn or not dsn.strip():
        return False
    logger = logging.getLogger("sentry_setup")
    try:
        sentry_sdk.init(
            dsn=dsn.strip(),
            send_default_pii=False,
            include_local_variables=False,
            before_send=scrub_pii,
            before_send_transaction=scrub_pii,
            traces_sample_rate=0.1,
        )
    except Exception as err:
        logger.warning("Sentry init failed: %s", err)
        return False
    return True
