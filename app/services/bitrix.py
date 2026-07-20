"""Сервис Bitrix24: нормализация телефона, поиск/создание контакта, сделки без дублей.

Работает поверх клиента fast-bitrix24 (BitrixAsync) через входящий вебхук.
Все функции принимают клиент первым аргументом — так их легко тестировать
на любом объекте с методами call()/get_all().

Семантика pinned-клиента fast-bitrix24==1.8.12, на которую опирается код
(зафиксирована контрактными тестами tests/test_bitrix_contract.py и живыми
запросами к порталу):
- get_all() НЕ принимает order/start в params (ViolationError до HTTP) и сам
  добавляет сортировку по ID ASC — поэтому нужный порядок делается на клиенте;
- call() для .list-методов разворачивает ответ до ПЕРВОЙ строки, а словарь
  из одного ключа ({"CONTACT": [...]}) — до содержимого: списки читаются
  только через get_all, а методы со «своей» формой ответа — через raw=True;
- словарь со многими ключами (crm.deal.get) call() возвращает обёрнутым в
  служебный ключ батча "orderNNNNNNNNNN" — обёртка снимается здесь.

Квирки самого портала: LIKE-фильтр по множественному PHONE игнорируется и
возвращает посторонние контакты (поэтому поиск по телефону — только точный
crm.duplicate.findbycomm), а список значений в точном фильтре тоже
игнорируется — для «ID из массива» обязателен оператор @.

Ограничение частоты запросов (2 rps) клиент выдерживает сам; повторы при
503/429 тоже на его стороне. Персональные данные (телефон, имя) в логи
уровня INFO не пишутся — только идентификаторы сущностей CRM.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, Protocol

import httpx
from aiohttp import ClientResponseError
from fast_bitrix24 import BitrixAsync
from fast_bitrix24.server_response import (
    ErrorInServerResponseException,
    ServerResponseParser,
)

log = logging.getLogger(__name__)

# Стадия новой сделки в стандартной воронке Bitrix24
STAGE_NEW = "NEW"

# Пользовательские поля сделки. Bitrix сам добавляет префикс UF_CRM_ к FIELD_NAME.
UF_TG_MSG_ID = "UF_CRM_TG_MSG_ID"
UF_TG_DRAFT_ID = "UF_CRM_TG_DRAFT_ID"
UF_EXPENSE = "UF_CRM_EXPENSE"
UF_PROFIT = "UF_CRM_PROFIT"
UF_SERVICE_CATEGORY = "UF_CRM_SERVICE_CATEGORY"
# Организация клиента. Штатного поля организации у контакта нет
# (COMPANY_TITLE — только readonly-поле лида, crm.contact.add молча
# игнорирует его), поэтому организация хранится в UF-поле контакта.
UF_ORG = "UF_CRM_ORG"

_UF_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "FIELD_NAME": "TG_MSG_ID",
        "USER_TYPE_ID": "string",
        "EDIT_FORM_LABEL": "Ключ сообщения Telegram",
        "LIST_COLUMN_LABEL": "Ключ сообщения Telegram",
    },
    {
        "FIELD_NAME": "EXPENSE",
        "USER_TYPE_ID": "double",
        "EDIT_FORM_LABEL": "Расход",
        "LIST_COLUMN_LABEL": "Расход",
    },
    {
        "FIELD_NAME": "PROFIT",
        "USER_TYPE_ID": "double",
        "EDIT_FORM_LABEL": "Прибыль",
        "LIST_COLUMN_LABEL": "Прибыль",
    },
    {
        "FIELD_NAME": "SERVICE_CATEGORY",
        "USER_TYPE_ID": "string",
        "EDIT_FORM_LABEL": "Категория услуги",
        "LIST_COLUMN_LABEL": "Категория услуги",
    },
)

_CONTACT_UF_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "FIELD_NAME": "TG_DRAFT_ID",
        "USER_TYPE_ID": "string",
        "EDIT_FORM_LABEL": {"ru": "Черновик TG"},
    },
    {
        "FIELD_NAME": "ORG",
        "USER_TYPE_ID": "string",
        "EDIT_FORM_LABEL": {"ru": "Организация"},
        "LIST_COLUMN_LABEL": {"ru": "Организация"},
    },
)

# Признаки ответа "такое поле уже существует" (текст зависит от языка портала)
_EXISTS_MARKERS = ("уже существует", "already exists", "already exist")

# Справочник источников сделки (crm.status, ENTITY_ID=SOURCE): STATUS_ID →
# человекочитаемое название, совпадающее со значениями Source в app/schemas.py.
# «Прочее» — это штатный OTHER (ensure_sources переименовывает его), остальные
# значения бот добавляет в справочник при старте.
DEAL_SOURCES: tuple[tuple[str, str], ...] = (
    ("AVITO", "Авито"),
    ("FORPOST", "Форпост"),
    ("SARAFAN", "Сарафанное радио"),
    ("OTHER", "Прочее"),
)
SOURCE_ID_BY_NAME = {name: status_id for status_id, name in DEAL_SOURCES}
SOURCE_NAME_BY_ID = {status_id: name for status_id, name in DEAL_SOURCES}
# Источник, который пишется в сделку, если сотрудник его не назвал.
DEFAULT_SOURCE_ID = "OTHER"

# Обязательные UF-поля и их ожидаемые типы: поле с чужим USER_TYPE_ID так же
# непригодно, как отсутствующее (например, TG_MSG_ID типа double молча
# исказил бы строковый ключ идемпотентности).
_REQUIRED_UF_BY_LIST_METHOD: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "crm.deal.userfield.list",
        {
            UF_TG_MSG_ID: "string",
            UF_EXPENSE: "double",
            UF_PROFIT: "double",
            UF_SERVICE_CATEGORY: "string",
        },
    ),
    ("crm.contact.userfield.list", {UF_TG_DRAFT_ID: "string", UF_ORG: "string"}),
)


class UFFieldsError(RuntimeError):
    """В CRM отсутствуют обязательные пользовательские поля."""


class MalformedBitrixResponse(RuntimeError):
    """Bitrix ответил HTTP 200, но не прислал валидный REST-конверт."""


def is_server_refusal(exc: BaseException) -> bool:
    """Сервер ЯВНО ответил ошибкой: запрос обработан и отвергнут.

    Такой исход однозначен — сущность (задача, сделка) точно не создана,
    честное «не удалось» и повтор безопасны. Таймаут, обрыв соединения и
    прочие транспортные сбои сюда не попадают: их исход неизвестен, запрос
    мог примениться без ответа. У pinned fast-bitrix24 ответ сервера с
    ошибкой поднимается как ErrorInServerResponseException (зафиксировано
    контрактными тестами), транспортные проблемы приходят другими типами
    (aiohttp, TimeoutError).
    """
    if isinstance(exc, ErrorInServerResponseException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500
    return isinstance(exc, ClientResponseError) and 400 <= exc.status < 500


class BitrixClient(Protocol):
    """Минимальный интерфейс клиента Bitrix24 (его реализует BitrixAsync)."""

    async def call(
        self, method: str, items: dict | None = None, raw: bool = False
    ) -> Any: ...

    async def get_all(self, method: str, params: dict | None = None) -> Any: ...

    async def call_once(self, method: str, items: dict | None = None) -> Any: ...


def _checked_raw_response(response: Any) -> dict[str, Any]:
    """Проверяет сырой конверт REST, который клиент сам не разбирает."""
    if not isinstance(response, dict):
        # После unsafe write такая форма ничего не говорит об исходе запроса:
        # сервер мог выполнить запись и потерять корректный JSON-ответ.
        raise MalformedBitrixResponse("Bitrix вернул ответ неверного формата")
    if "error" in response:
        code = response.get("error")
        description = response.get("error_description") or ""
        raise ErrorInServerResponseException(f"{code}: {description}")
    return response


def require_positive_id(value: Any, method: str) -> int:
    """Проверяет ID из method-level ответа Bitrix и приводит его к int."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MalformedBitrixResponse(f"Bitrix вернул неверный ID для {method}")
    if isinstance(value, str) and not value.isdecimal():
        raise MalformedBitrixResponse(f"Bitrix вернул неверный ID для {method}")
    entity_id = int(value)
    if entity_id <= 0:
        raise MalformedBitrixResponse(f"Bitrix вернул неверный ID для {method}")
    return entity_id


class NonRetryingBitrixAsync(BitrixAsync):
    """Клиент с одиночной отправкой для неидемпотентных REST-методов."""

    async def call_once(self, method: str, items: dict | None = None) -> Any:
        # request_attempt делает ровно один HTTP-запрос; single_request здесь
        # неприменим, потому что внутри него транспортные сбои повторяются.
        response = await self.srh.run_async(
            self.srh.request_attempt(method.strip().lower(), items)
        )
        checked = _checked_raw_response(response)
        return ServerResponseParser(checked).extract_results()


async def call_once(bx: BitrixClient, method: str, items: dict | None = None) -> Any:
    """Вызывает неидемпотентный метод без прозрачных повторных отправок."""
    one_shot = getattr(bx, "call_once", None)
    if one_shot is None:
        # Совместимость с минимальными тестовыми двойниками. Боевой клиент
        # всегда NonRetryingBitrixAsync и в эту ветку не попадает.
        return await bx.call(method, items)
    return await one_shot(method, items)


def get_bitrix(webhook_url: str) -> BitrixAsync:
    """Создаёт клиент Bitrix24 по URL входящего вебхука."""
    return NonRetryingBitrixAsync(webhook_url, verbose=False)


# Маркеры добавочного номера: всё после них к основному номеру не относится.
# «x» и «#» считаются маркерами, только если не являются частью слова
# (например, «fax» маркером не считается).
_EXTENSION_RE = re.compile(
    r"\bдоб\b|\bвн\b|\bextension\b|\bext\b|(?<![a-zа-яё])[x#]",
)


def normalize_phone(raw: str | None) -> str | None:
    """Приводит телефон к формату E.164 (+7XXXXXXXXXX для России).

    - отбрасывает добавочный номер (всё после «доб», «вн», «ext», «x», «#»);
    - убирает пробелы, скобки, дефисы и прочие нецифровые символы;
    - 11 цифр с 7 или 8 в начале -> +7 и последние 10 цифр;
    - ровно 10 цифр -> +7 и эти 10 цифр (регион по умолчанию — Россия);
    - 11-15 цифр с другим кодом страны -> как есть с префиксом + (E.164);
    - меньше 10 или больше 15 цифр — невалидный номер (None).
    """
    text = (raw or "").lower()
    text = _EXTENSION_RE.split(text, maxsplit=1)[0]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def _last10(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-10:]


# Символы, допустимые в «голом» номере телефона: цифры и телефонная
# пунктуация. Буквы и прочие знаки означают, что текст — фраза, а не номер.
_PHONE_CHARS_RE = re.compile(r"[\d\s()+.,;:/-]*")

# Допустимый хвост после маркера добавочного: пунктуация и короткий номер
# добавочного (до 7 цифр). Любые буквы или длинные цифры после «доб.»
# означают, что дальше идёт продолжение новой заявки, а не часть номера.
_EXTENSION_TAIL_RE = re.compile(r"[\s.,:;№()/-]*\d{0,7}[\s.,:;№()/-]*")

# Явно отделённый цифровой хвост (сумма, количество, второй номер) нельзя
# склеивать с международным номером только потому, что итог уложился в E.164.
_SEPARATED_NUMERIC_TAIL_RE = re.compile(
    r"^(?P<phone>.+?)(?:[,;:/]\s*|\.\s+)[()+-]*\s*"
    r"(?P<tail>\d(?:[\d\s().+-]*\d)?)[).!?]*\s*$"
)


def extract_bare_phone(raw: str | None) -> str | None:
    """Телефон из текста, который ЦЕЛИКОМ является номером, иначе None.

    Отличает ответ-номер на вопрос о телефоне от полноценной фразы с номером
    внутри («Мария, 89141234567, электрика, заменить розетку»): фраза
    содержит буквы, поглощать её как ответ-телефон нельзя — она должна
    переразбираться как новая заявка. Проверяется ВСЯ строка:
    - после маркера добавочного допустим только сам добавочный («доб. 12»,
      «x12»); «доб. 12, Мария, электрика» — уже новая заявка;
    - без явного «+» номером считается только российская форма (10–11 цифр):
      «8 (914) 123-45-67, 5000» — это номер и сумма, а не 15-значный телефон.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    main, *extension = _EXTENSION_RE.split(text, maxsplit=1)
    if extension and not _EXTENSION_TAIL_RE.fullmatch(extension[0]):
        return None
    if not _PHONE_CHARS_RE.fullmatch(main):
        return None
    separated_tail = _SEPARATED_NUMERIC_TAIL_RE.fullmatch(main)
    if separated_tail and normalize_phone(separated_tail.group("phone")) is not None:
        return None
    digits = re.sub(r"\D", "", main)
    if main.lstrip().startswith("+"):
        # Российский +7 допускает ровно десять цифр после кода страны.
        # Иначе сумма/добавочный хвост вроде «+7..., 5000» поглощался номером.
        if digits.startswith("7") and len(digits) != 11:
            return None
    elif len(digits) not in (10, 11):
        return None
    return normalize_phone(main)


async def find_contact_ids(bx: BitrixClient, phone: str) -> list[int]:
    """Все контакты с этим телефоном (crm.duplicate.findbycomm), новые не создаёт.

    Вызов идёт с raw=True: обычный call() у pinned fast-bitrix24 разворачивает
    ответ {"CONTACT": [...]} до первого ID, теряя тип сущности и остальные
    контакты (см. tests/test_bitrix_contract.py). Телефон может быть записан
    у нескольких контактов-дублей — возвращаются все.

    Запасного поиска подстрокой (%PHONE в crm.contact.list) нет НАМЕРЕННО:
    LIKE-фильтр по множественному PHONE портал игнорирует и возвращает
    произвольный контакт — новая сделка цеплялась бы к чужому клиенту.
    Ошибки CRM пробрасываются вызывающему: «не найден» и «недоступен» —
    разные исходы.
    """
    last10 = _last10(phone)
    resp = await bx.call(
        "crm.duplicate.findbycomm",
        {"entity_type": "CONTACT", "type": "PHONE", "values": [phone, last10]},
        raw=True,
    )
    resp = _checked_raw_response(resp)
    result = resp.get("result")
    if result == []:
        log.info("Контакт по телефону не найден")
        log.debug("Искали телефон %s (последние 10: %s)", phone, last10)
        return []
    if not isinstance(result, dict) or set(result) != {"CONTACT"}:
        raise MalformedBitrixResponse(
            "Bitrix вернул неверный result для crm.duplicate.findbycomm"
        )
    ids = result["CONTACT"]
    if not isinstance(ids, list):
        raise MalformedBitrixResponse(
            "Bitrix вернул неверный CONTACT для crm.duplicate.findbycomm"
        )
    contact_ids = [
        require_positive_id(contact_id, "crm.duplicate.findbycomm") for contact_id in ids
    ]
    if not contact_ids:
        log.info("Контакт по телефону не найден")
        return []
    log.info("Контакты найдены через findbycomm: %s", contact_ids)
    return contact_ids


async def find_contact(bx: BitrixClient, phone: str) -> int | None:
    """Первый контакт с этим телефоном или None (для привязки сделки)."""
    contact_ids = await find_contact_ids(bx, phone)
    return contact_ids[0] if contact_ids else None


async def find_contact_by_draft_id(bx: BitrixClient, draft_id: str) -> int | None:
    """Ищет контакт по постоянному ключу черновика без создания сущностей."""
    rows = await list_all_checked(
        bx,
        "crm.contact.list",
        {"filter": {UF_TG_DRAFT_ID: draft_id}, "select": ["ID"]},
    )
    return int(rows[0]["ID"]) if rows else None


async def create_or_update_contact(
    bx: BitrixClient,
    name: str,
    phone: str | None,
    org: str | None = None,
    comment: str | None = None,
    draft_id: str | None = None,
    before_unsafe_write: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """Возвращает ID контакта: существующего (дописав историю обращения) или нового.

    Без телефона (сотрудник ответил "нет" на уточняющий вопрос) поиск дублей
    невозможен — контакт создаётся сразу, только с именем.
    """
    contact_id, created = await resolve_contact(
        bx,
        name=name,
        phone=phone,
        org=org,
        comment=comment,
        draft_id=draft_id,
        before_contact_add=before_unsafe_write,
    )
    if comment and not created:
        if before_unsafe_write is not None:
            await before_unsafe_write()
        await add_contact_timeline_comment(bx, contact_id, comment)
    return contact_id


async def resolve_contact(
    bx: BitrixClient,
    name: str,
    phone: str | None,
    org: str | None = None,
    comment: str | None = None,
    draft_id: str | None = None,
    before_contact_add: Callable[[], Awaitable[None]] | None = None,
) -> tuple[int, bool]:
    """Находит либо создаёт контакт; возвращает ``(id, создан_сейчас)``.

    Timeline-комментарий существующего контакта сюда намеренно не входит:
    у него отдельная постоянная fence-фаза. Для нового контакта комментарий
    записывается полем COMMENTS в том же contact.add.
    """
    contact_id = None
    found_by_draft_id = False
    if draft_id:
        contact_id = await find_contact_by_draft_id(bx, draft_id)
        if contact_id is not None:
            found_by_draft_id = True
            log.info("Контакт с ключом черновика уже есть: id=%s", contact_id)
    if contact_id is None and phone:
        # Ошибка поиска не равна «контакт не найден»: при недоступной CRM
        # создавать новый контакт опасно — получится постоянный дубль.
        contact_id = await find_contact(bx, phone)
    if contact_id is not None:
        if draft_id and not found_by_draft_id:
            # Идемпотентный UF-ключ позволяет восстановить contact_id после
            # рестарта, но не доказывает доставку отдельного комментария.
            await bx.call(
                "crm.contact.update",
                {"id": contact_id, "fields": {UF_TG_DRAFT_ID: draft_id}},
            )
        if org:
            try:
                await bx.call(
                    "crm.contact.update",
                    {"id": contact_id, "fields": {UF_ORG: org}},
                )
                log.info("Контакту id=%s записана организация в UF", contact_id)
            except Exception as exc:  # noqa: BLE001 - потеря UF не должна ронять заявку
                log.warning(
                    "Организация контакту id=%s не записана (%s)",
                    contact_id,
                    type(exc).__name__,
                )
        return contact_id, False

    fields: dict[str, Any] = {"NAME": name}
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if org:
        fields[UF_ORG] = org
    if comment:
        fields["COMMENTS"] = comment
    if draft_id:
        fields[UF_TG_DRAFT_ID] = draft_id
    if before_contact_add is not None:
        await before_contact_add()
    result = await call_once(bx, "crm.contact.add", {"fields": fields})
    new_id = require_positive_id(result, "crm.contact.add")
    log.info("Создан контакт id=%s", new_id)
    log.debug("Новый контакт: name=%s phone=%s org=%s", name, phone, org)
    return new_id, True


async def add_contact_timeline_comment(
    bx: BitrixClient, contact_id: int, comment: str
) -> None:
    """Однократно дописывает обращение существующему контакту."""
    result = await call_once(
        bx,
        "crm.timeline.comment.add",
        {
            "fields": {
                "ENTITY_ID": contact_id,
                "ENTITY_TYPE": "contact",
                "COMMENT": comment,
            }
        },
    )
    require_positive_id(result, "crm.timeline.comment.add")
    log.info("Контакту id=%s дописан комментарий в таймлайн", contact_id)


async def find_deal_by_key(bx: BitrixClient, key: str) -> int | None:
    """Ищет сделку по идемпотентному ключу в UF_CRM_TG_MSG_ID.

    Используется и перед созданием сделки (create_deal_idempotent), и при
    сверке после таймаута: сервер мог принять crm.deal.add, не успев ответить.
    """
    rows = await list_all_checked(
        bx,
        "crm.deal.list",
        {"filter": {UF_TG_MSG_ID: key}, "select": ["ID"]},
    )
    if rows:
        return int(rows[0]["ID"])
    return None


async def create_deal(
    bx: BitrixClient,
    contact_id: int,
    fields: dict[str, Any],
    tg_msg_key: str,
) -> int:
    """Отправляет crm.deal.add с ключом идемпотентности в UF_CRM_TG_MSG_ID.

    Только сама отправка сделки, без предпроверки дубля: вызывающий обязан
    сначала проверить find_deal_by_key. Разделение нужно обработчику кнопки
    «Создать» — у ошибок предпроверки и ошибок самого deal.add разная
    семантика (после отправки add любой сбой неоднозначен: сделка могла
    записаться без ответа).
    """
    deal_fields = dict(fields)
    deal_fields.setdefault("STAGE_ID", STAGE_NEW)
    deal_fields["CONTACT_ID"] = contact_id
    deal_fields[UF_TG_MSG_ID] = tg_msg_key

    result = await call_once(bx, "crm.deal.add", {"fields": deal_fields})
    deal_id = require_positive_id(result, "crm.deal.add")
    log.info("Создана сделка id=%s (контакт id=%s)", deal_id, contact_id)
    return deal_id


async def create_deal_idempotent(
    bx: BitrixClient,
    contact_id: int,
    fields: dict[str, Any],
    tg_msg_key: str,
) -> int:
    """Создаёт сделку. Повторный вызов с тем же tg_msg_key сделку не дублирует.

    Ключ идемпотентности хранится в пользовательском поле UF_CRM_TG_MSG_ID:
    сначала ищем сделку с этим ключом и, если она есть, возвращаем её ID.
    """
    existing_id = await find_deal_by_key(bx, tg_msg_key)
    if existing_id is not None:
        log.info("Сделка с ключом сообщения уже есть: id=%s, дубль не создаю", existing_id)
        return existing_id
    return await create_deal(bx, contact_id, fields, tg_msg_key)


# Поля сделки для коротких списков (/find, /last)
DEAL_SUMMARY_SELECT = ["ID", "TITLE", "STAGE_ID", "DATE_CREATE", "CONTACT_ID"]

# Размер страницы списочных методов REST Bitrix24: больше 50 строк за один
# запрос сервер не отдаёт.
PAGE_SIZE = 50

# Ограничение размера @-фильтра по контактам в текстовом поиске: широкий
# запрос («/find а») не должен раздувать один REST-вызов сотнями ID.
MAX_CONTACTS_IN_FILTER = 50

# Сколько страниц контактов читается на один фильтр текстового поиска.
# Вместе с первой страницей на сделочный фильтр и почанковой выборкой сделок
# это даёт жёсткий потолок REST-вызовов одного поиска (см.
# search_deals_by_text) — get_all с его выгрузкой всех страниц не участвует.
MAX_CONTACT_PAGES = 2

# Признаки «сделка не найдена» в тексте ошибки crm.deal.get. Любая другая
# ошибка (нет прав, сеть, просроченный вебхук) пробрасывается: «не найдена»
# и «CRM недоступна» — разные ответы пользователю.
_DEAL_NOT_FOUND_CODES = ("error_not_found", "crm_deal_not_found")


def _is_deal_not_found(exc: Exception) -> bool:
    """Ошибка crm.deal.get означает именно «такой сделки нет»?"""
    text = str(exc).lower()
    return any(code in text for code in _DEAL_NOT_FOUND_CODES)


def _unwrap_batch_label(value: Any) -> Any:
    """Снимает служебную обёртку батча с результата call().

    Pinned-клиент заворачивает единичный вызов в батч, и словарь-результат
    со многими ключами (например, crm.deal.get) возвращается под служебным
    ключом вида "orderNNNNNNNNNN" (см. tests/test_bitrix_contract.py).
    """
    if isinstance(value, dict) and len(value) == 1:
        label = next(iter(value))
        if isinstance(label, str) and label.startswith("order"):
            return value[label]
    return value


def _by_id_desc(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сортировка сделок по ID по убыванию (новые сверху) на клиенте.

    get_all() у pinned-клиента не принимает order и сам сортирует по ID ASC,
    поэтому нужный порядок наводится здесь.
    """
    return sorted(rows, key=lambda row: int(row["ID"]), reverse=True)


async def get_deal(bx: BitrixClient, deal_id: int) -> dict[str, Any] | None:
    """Сделка по номеру; None — только «не найдена», прочие ошибки CRM пробрасываются."""
    try:
        deal = await bx.call("crm.deal.get", {"id": deal_id})
    except Exception as exc:
        if _is_deal_not_found(exc):
            log.info("Сделка id=%s не найдена", deal_id)
            return None
        # Не «не найдена», а сбой CRM (в том числе «метод не найден»):
        # вызывающий ответит «поиск не работает», а не соврёт «ничего не нашёл».
        raise
    deal = _unwrap_batch_label(deal)
    if not isinstance(deal, dict):
        raise MalformedBitrixResponse("Bitrix вернул неверный result для crm.deal.get")
    actual_id = require_positive_id(deal.get("ID"), "crm.deal.get")
    if actual_id != deal_id:
        raise MalformedBitrixResponse("Bitrix вернул чужой ID для crm.deal.get")
    return deal


async def _list_page(
    bx: BitrixClient, method: str, params: dict[str, Any], start: int = 0
) -> tuple[list[dict[str, Any]], int | None]:
    """Одна страница .list-метода (новые сверху): ровно один REST-запрос.

    get_all() у pinned-клиента выгружает ВСЕ страницы выдачи — широкий фильтр
    на большом портале превращается в сотни запросов и таймаут. Поэтому
    ограниченное чтение идёт raw-вызовом с явными order/start: get_all их
    запрещает (ViolationError), а raw пропускает на сервер как есть и отдаёт
    конверт ответа с next (зафиксировано контрактными тестами).

    Возвращает (строки страницы, start следующей страницы или None).
    """
    payload = dict(params)
    payload["order"] = {"ID": "DESC"}
    payload["start"] = start
    resp = await bx.call(method, payload, raw=True)
    resp = _checked_raw_response(resp)
    rows = resp.get("result")
    if not isinstance(rows, list):
        raise MalformedBitrixResponse(f"Bitrix вернул неверный result для {method}")
    if not all(isinstance(row, dict) for row in rows):
        raise MalformedBitrixResponse(f"Bitrix вернул неверные строки для {method}")
    for row in rows:
        require_positive_id(row.get("ID"), method)
    next_start = resp.get("next")
    if next_start is None:
        return rows, None
    try:
        parsed_next = int(next_start)
    except (TypeError, ValueError) as exc:
        raise MalformedBitrixResponse(f"Bitrix вернул неверный next для {method}") from exc
    if isinstance(next_start, bool) or parsed_next <= start:
        raise MalformedBitrixResponse(f"Bitrix вернул неверный next для {method}")
    return rows, parsed_next


async def list_all_checked(
    bx: BitrixClient, method: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Выгружает точный список постранично и валидирует каждый raw-конверт.

    Safety-critical проверки дублей не используют ``get_all``: pinned-клиент
    превращает верхнеуровневую JSON-ошибку Bitrix в ``None``. Прямой raw-вызов
    сохраняет envelope, поэтому «не найдено» нельзя спутать с отказом чтения.
    ``order`` и ``start`` передаются только raw-вызову, где они разрешены.
    """
    payload = dict(params or {})
    payload.setdefault("order", {"ID": "ASC"})
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        response = _checked_raw_response(
            await bx.call(method, {**payload, "start": start}, raw=True)
        )
        page = response.get("result")
        if method == "tasks.task.list":
            if not isinstance(page, dict) or set(page) != {"tasks"}:
                raise MalformedBitrixResponse(
                    f"Bitrix вернул неверный result для {method}"
                )
            page = page["tasks"]
        if not isinstance(page, list):
            raise MalformedBitrixResponse(
                f"Bitrix вернул неверный result для {method}"
            )
        if not all(isinstance(row, dict) for row in page):
            raise MalformedBitrixResponse(
                f"Bitrix вернул неверные строки для {method}"
            )
        id_field = "id" if method == "tasks.task.list" else "ID"
        for row in page:
            require_positive_id(row.get(id_field), method)
        rows.extend(page)
        next_start = response.get("next")
        if next_start is None:
            return rows
        try:
            start = int(next_start)
        except (TypeError, ValueError) as exc:
            raise MalformedBitrixResponse(
                f"Bitrix вернул неверный next для {method}"
            ) from exc


class SearchResult(NamedTuple):
    """Ограниченная поисковая выдача и признак непрочитанного хвоста."""

    deals: list[dict[str, Any]]
    truncated: bool


async def search_deals_by_phone(bx: BitrixClient, phone: str) -> SearchResult:
    """Сделки всех контактов с этим телефоном (новые сверху).

    Телефон может быть записан у нескольких контактов-дублей — сделки
    собираются по всем (find_contact_ids), выборка идёт оператором
    @CONTACT_ID (список в точном фильтре портал игнорирует) почанково,
    первой страницей самых новых на чанк: точный поиск по телефону не должен
    выгружать сотни древних сделок клиента.
    """
    contact_ids = await find_contact_ids(bx, phone)
    if not contact_ids:
        return SearchResult([], False)
    deals: dict[int, dict[str, Any]] = {}
    truncated = False
    for offset in range(0, len(contact_ids), MAX_CONTACTS_IN_FILTER):
        chunk = contact_ids[offset : offset + MAX_CONTACTS_IN_FILTER]
        rows, next_start = await _list_page(
            bx,
            "crm.deal.list",
            {"filter": {"@CONTACT_ID": chunk}, "select": DEAL_SUMMARY_SELECT},
        )
        truncated = truncated or next_start is not None
        for row in rows:
            deals[int(row["ID"])] = row
    return SearchResult(_by_id_desc(list(deals.values())), truncated)


TextSearchResult = SearchResult


async def search_deals_by_text(bx: BitrixClient, query: str) -> TextSearchResult:
    """Сделки по названию/комментарию, имени/фамилии и организации контакта.

    Организация ищется двумя путями: точным фильтром по UF-полю контакта
    (UF_CRM_ORG — штатного поля организации у контакта нет) и подстрокой
    «Организация: …» в COMMENTS сделки (см. build_deal_fields) — так
    «/find <организация>» находит и заявки бота, и сделки клиентов, у
    которых организация записана только в карточке контакта.

    Чтение жёстко ограничено, полного get_all здесь нет:
    - по сделочным фильтрам (%TITLE, %COMMENTS) — первая страница самых
      новых (PAGE_SIZE строк);
    - по контактным фильтрам — до MAX_CONTACT_PAGES страниц на фильтр,
      причём страницы ДОЧИТЫВАЮТСЯ, а не срезаются: однофамильцы со старыми
      ID не выпадают из выборки, пока лимит страниц не исчерпан;
    - сделки найденных контактов — первой страницей на чанк из
      MAX_CONTACTS_IN_FILTER ID.
    Итого не больше 2 + 3*MAX_CONTACT_PAGES + ceil(контакты/чанк) вызовов.
    Оборванная лимитом выборка помечается truncated.

    Результаты объединяются без дублей и сортируются по ID по убыванию.
    """
    deals: dict[int, dict[str, Any]] = {}
    truncated = False
    for field in ("%TITLE", "%COMMENTS"):
        rows, next_start = await _list_page(
            bx,
            "crm.deal.list",
            {"filter": {field: query}, "select": DEAL_SUMMARY_SELECT},
        )
        truncated = truncated or next_start is not None
        for row in rows:
            deals[int(row["ID"])] = row

    contact_ids: list[int] = []
    seen: set[int] = set()
    # Организация контакта ищется ТОЧНЫМ фильтром по UF-полю (надёжен),
    # LIKE по UF-полям не используется; подстрочное совпадение организации
    # дополнительно покрывает %COMMENTS сделки выше.
    for flt in ({"%NAME": query}, {"%LAST_NAME": query}, {UF_ORG: query}):
        start: int | None = 0
        for _ in range(MAX_CONTACT_PAGES):
            rows, start = await _list_page(
                bx, "crm.contact.list", {"filter": flt, "select": ["ID"]}, start
            )
            for row in rows:
                contact_id = int(row["ID"])
                if contact_id not in seen:
                    seen.add(contact_id)
                    contact_ids.append(contact_id)
            if start is None:
                break
        truncated = truncated or start is not None

    for offset in range(0, len(contact_ids), MAX_CONTACTS_IN_FILTER):
        chunk = contact_ids[offset : offset + MAX_CONTACTS_IN_FILTER]
        rows, next_start = await _list_page(
            bx,
            "crm.deal.list",
            {"filter": {"@CONTACT_ID": chunk}, "select": DEAL_SUMMARY_SELECT},
        )
        truncated = truncated or next_start is not None
        for row in rows:
            deals[int(row["ID"])] = row
    return TextSearchResult(_by_id_desc(list(deals.values())), truncated)


async def recent_deals(bx: BitrixClient, limit: int = 10) -> list[dict[str, Any]]:
    """Последние заявки одной страницей (сервер сортирует по ID DESC).

    Раньше get_all выгружал ВСЕ сделки портала ради топ-10. Теперь ровно
    один REST-вызов: первая страница DESC (PAGE_SIZE строк) заведомо
    покрывает limit, хвост не дочитывается. Порядок для надёжности всё
    равно наводится на клиенте.
    """
    rows, _ = await _list_page(bx, "crm.deal.list", {"select": DEAL_SUMMARY_SELECT})
    return _by_id_desc(rows)[:limit]


async def contact_names(bx: BitrixClient, contact_ids: list[int]) -> dict[int, str]:
    """Имена контактов по списку ID — подпись «клиент» в списках заявок."""
    ids = sorted({int(cid) for cid in contact_ids if cid})
    if not ids:
        return {}
    rows = await bx.get_all(
        "crm.contact.list",
        {"filter": {"@ID": ids}, "select": ["ID", "NAME", "LAST_NAME"]},
    )
    names: dict[int, str] = {}
    for row in rows:
        parts = (row.get("NAME"), row.get("LAST_NAME"))
        name = " ".join(str(part) for part in parts if part)
        names[int(row["ID"])] = name or f"контакт {row['ID']}"
    return names


async def stage_names(bx: BitrixClient) -> dict[str, str]:
    """Названия стадий сделок ({STATUS_ID: NAME}) для читаемых списков.

    Берутся стадии ВСЕХ воронок: у основной ENTITY_ID=DEAL_STAGE, у
    дополнительных — DEAL_STAGE_<n>, а их STATUS_ID уже содержит префикс
    воронки («C5:NEW») — ровно так стадия записана в сделке. Справочник
    статусов небольшой, поэтому берётся целиком и фильтруется на клиенте.
    При сбое возвращается пустой словарь: списки покажут коды стадий,
    поиск из-за названий не падает.
    """
    try:
        response = _checked_raw_response(await bx.call("crm.status.list", {}, raw=True))
        rows = response.get("result")
        if not isinstance(rows, list):
            rows = []
    except Exception as exc:  # noqa: BLE001 - имена стадий не критичны для поиска
        log.warning("Названия стадий не получены (%s), показываю коды", type(exc).__name__)
        return {}
    return {
        str(row["STATUS_ID"]): str(row["NAME"])
        for row in rows
        if isinstance(row, dict)
        and row.get("STATUS_ID")
        and str(row.get("ENTITY_ID") or "").startswith("DEAL_STAGE")
    }


# Тип владельца «сделка» в новом REST-API дел (crm.activity.todo.*).
TODO_OWNER_TYPE_DEAL = 2


def _extract_todo_id(result: Any) -> int:
    """ID дела из ответа crm.activity.todo.add (форма ответа плавает).

    Портал отвечает объектом дела; клиент может отдать его как есть, снять
    одноключевую обёртку («activity») или завернуть в служебный батч-ключ —
    все формы сводятся к полю id.
    """
    value = _unwrap_batch_label(result)
    if isinstance(value, dict) and len(value) == 1:
        value = next(iter(value.values()))
    if isinstance(value, dict):
        value = value.get("id", value.get("ID"))
    return require_positive_id(value, "crm.activity.todo.add")


async def create_deal_todo(
    bx: BitrixClient,
    deal_id: int,
    title: str,
    deadline_iso: str,
    responsible_id: int,
    description: str | None = None,
) -> int:
    """Создаёт в сделке ДЕЛО с напоминанием в момент срока.

    Именно дела (crm.activity.todo) мобильное приложение Bitrix24 показывает
    push-уведомлением с напоминанием; обычная задача приходит тихим
    колокольчиком. Ответственным обязан быть пользователь заказчика
    (settings.bitrix_responsible_id): push уходит ответственному, а не
    владельцу вебхука. pingOffsets=[0] — напоминание ровно в срок.
    """
    fields: dict[str, Any] = {
        "ownerTypeId": TODO_OWNER_TYPE_DEAL,
        "ownerId": deal_id,
        "title": title[:255],
        "deadline": deadline_iso,
        "responsibleId": responsible_id,
        "pingOffsets": [0],
    }
    if description:
        fields["description"] = description
    result = await call_once(bx, "crm.activity.todo.add", fields)
    todo_id = _extract_todo_id(result)
    log.info("В сделке id=%s создано дело-напоминание id=%s", deal_id, todo_id)
    return todo_id


async def update_deal_todo_deadline(
    bx: BitrixClient, todo_id: int, deal_id: int, deadline_iso: str
) -> None:
    """Переносит срок существующего дела-напоминания (правка заявки)."""
    await bx.call(
        "crm.activity.todo.update",
        {
            "id": todo_id,
            "ownerTypeId": TODO_OWNER_TYPE_DEAL,
            "ownerId": deal_id,
            "deadline": deadline_iso,
            "pingOffsets": [0],
        },
    )
    log.info("Срок дела id=%s перенесён", todo_id)


async def ensure_sources(bx: BitrixClient) -> None:
    """Идемпотентно доводит справочник источников до значений заказчика.

    Недостающие источники (Авито, Форпост, Сарафанное радио) добавляются в
    справочник crm.status (ENTITY_ID=SOURCE), а штатный OTHER («Другое»)
    переименовывается в «Прочее». Повторный запуск ничего не меняет.

    В отличие от ensure_uf_fields сбой здесь не отключает CRM: SOURCE_ID —
    не критичное поле, сделка запишется и без готового справочника (значение
    просто не будет подписано в карточке), поэтому вызывающий только логирует
    ошибку.
    """
    response = _checked_raw_response(
        await bx.call(
            "crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}}, raw=True
        )
    )
    rows = response.get("result")
    if not isinstance(rows, list):
        raise MalformedBitrixResponse("Bitrix вернул неверный result для crm.status.list")
    existing: dict[str, dict[str, Any]] = {
        str(row.get("STATUS_ID")): row
        for row in rows
        if isinstance(row, dict) and row.get("STATUS_ID")
    }
    for status_id, name in DEAL_SOURCES:
        row = existing.get(status_id)
        if row is None:
            await bx.call(
                "crm.status.add",
                {
                    "fields": {
                        "ENTITY_ID": "SOURCE",
                        "STATUS_ID": status_id,
                        "NAME": name,
                    }
                },
            )
            log.info("В справочник источников добавлен %s", status_id)
        elif str(row.get("NAME") or "") != name:
            await bx.call(
                "crm.status.update", {"id": row.get("ID"), "fields": {"NAME": name}}
            )
            log.info("Источник %s переименован в «%s»", status_id, name)


async def ensure_uf_fields(bx: BitrixClient) -> None:
    """Идемпотентно создаёт пользовательские поля сделок и контактов.

    Вызывается при старте. Если поле уже существует, Bitrix возвращает
    ошибку — она молча пропускается, любая другая ошибка пробрасывается.

    После создания обязательные поля сверяются через userfield.list по имени
    И по типу (USER_TYPE_ID): создание могло тихо не пройти (нет прав, тариф),
    а поле могли завести вручную с другим типом. В обоих случаях поднимается
    UFFieldsError — работать с CRM в таком состоянии нельзя, каждая запись
    заявки падала бы или молча портила данные.
    """
    fields_by_method = (
        ("crm.deal.userfield.add", _UF_FIELDS),
        ("crm.contact.userfield.add", _CONTACT_UF_FIELDS),
    )
    for method, fields in fields_by_method:
        for field in fields:
            try:
                await bx.call(method, {"fields": field})
                log.info("Создано поле UF_CRM_%s", field["FIELD_NAME"])
            except Exception as exc:  # noqa: BLE001 - фильтруем "поле уже существует"
                text = str(exc).lower()
                if any(marker in text for marker in _EXISTS_MARKERS):
                    log.debug("Поле UF_CRM_%s уже существует", field["FIELD_NAME"])
                    continue
                raise

    for list_method, required in _REQUIRED_UF_BY_LIST_METHOD:
        rows = await bx.get_all(list_method)
        actual_types: dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("FIELD_NAME") or "")
            # userfield.list отдаёт FIELD_NAME уже с префиксом UF_CRM_, но на
            # случай другой формы ответа принимается и имя без префикса.
            if not name.startswith("UF_CRM_"):
                name = "UF_CRM_" + name
            actual_types[name] = row.get("USER_TYPE_ID")
        problems = []
        for field, expected_type in required.items():
            if field not in actual_types:
                problems.append(f"{field} отсутствует")
            elif actual_types[field] != expected_type:
                problems.append(
                    f"{field} имеет тип {actual_types[field]!r} вместо {expected_type!r}"
                )
        if problems:
            raise UFFieldsError(
                f"Обязательные поля CRM не готовы: {'; '.join(sorted(problems))} "
                f"(проверка через {list_method})"
            )
