"""Общие фикстуры тестов.

Тесты сервиса Bitrix24 работают полностью офлайн: HTTP-запросы перехватывает
respx. Боевой клиент fast-bitrix24 ходит в сеть через aiohttp, поэтому для
тестов используется маленький httpx-клиент HttpBitrix с тем же интерфейсом
call/get_all/raw и — важно — с ТОЙ ЖЕ клиентской семантикой, что у pinned
fast-bitrix24==1.8.12 (см. tests/test_bitrix_contract.py, где эта семантика
зафиксирована на настоящем клиенте):

- get_all() не принимает order/start в params (ViolationError до HTTP)
  и сам добавляет к запросу сортировку {"ID": "ASC"};
- call() заворачивает запрос в батч и разворачивает ответ: список — до
  ПЕРВОЙ строки, словарь из одного ключа — до содержимого (снова до первой
  строки, если внутри список), словарь со многими ключами возвращается
  обёрнутым в служебный ключ "orderNNNNNNNNNN";
- call(raw=True) отдаёт весь конверт ответа сервера без преобразований.

Фейк, который ведёт себя мягче настоящего клиента, пропускал бы в прод код,
падающий на реальном Bitrix24 — поэтому семантика вынесена в разделяемые
помощники (mangle_call_result и другие) и используется всеми фейками.

Тесты Telegram-обработчиков тоже офлайн: бот собирается на RecordingSession,
которая не ходит в сеть, а записывает исходящие вызовы Bot API. Апдейты
скармливаются диспетчеру как обычные словари Bot API (make_message_update /
make_callback_update), так что отрабатывает вся цепочка мидлварей и FSM.
"""

import itertools
import json
import time
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, File, InlineKeyboardMarkup, Message, Update
from fast_bitrix24.server_response import ErrorInServerResponseException
from icontract import ViolationError

from app.services.bitrix import _checked_raw_response

WEBHOOK = "https://portal.example.bitrix24.ru/rest/1/testcode/"


def method_url(method: str) -> str:
    """URL REST-метода Bitrix24 для регистрации respx-маршрута."""
    return WEBHOOK + method + ".json"


def request_json(route) -> dict:
    """Тело последнего запроса respx-маршрута."""
    return json.loads(route.calls.last.request.content)


def call_json(call) -> dict:
    """Тело конкретного вызова respx-маршрута."""
    return json.loads(call.request.content)


# ---------------------------------------------------------------------------
# Семантика клиента fast-bitrix24==1.8.12, разделяемая всеми фейками.
# Контракт зафиксирован на настоящем клиенте в tests/test_bitrix_contract.py.
# ---------------------------------------------------------------------------

# Методы, к которым настоящий get_all() НЕ добавляет order (EXCLUDED_METHODS
# из fast_bitrix24.user_request).
GET_ALL_NO_ORDER_METHODS = {
    "crm.address.list",
    "documentgenerator.template.list",
    "userfieldconfig.list",
    "voximplant.statistic.get",
    "crm.deal.userfield.list",
    "task.elapseditem.getlist",
}


def check_get_all_params(params: dict | None) -> None:
    """Проверка параметров как в GetAllUserRequest: order/start запрещены."""
    keys = {str(key).upper().strip() for key in (params or {})}
    if keys & {"ORDER", "START"}:
        raise ViolationError("get_all() doesn't support parameters 'start' or 'order'")


def add_default_order(method: str, params: dict | None) -> dict:
    """Копия параметров с сортировкой {"ID": "ASC"}, как делает настоящий get_all."""
    payload = dict(params or {})
    if method.lower().strip() not in GET_ALL_NO_ORDER_METHODS:
        payload.setdefault("order", {"ID": "ASC"})
    return payload


def unwrap_get_all_result(result: Any) -> Any:
    """Результат get_all(): вложенный словарь из одного ключа разворачивается.

    Например, tasks.task.list отвечает {"tasks": [...]} — настоящий клиент
    возвращает вызывающему сразу внутренний список.
    """
    if isinstance(result, dict) and len(result) == 1:
        return next(iter(result.values()))
    return result


def mangle_call_result(result: Any) -> Any:
    """Преобразование результата call() как у настоящего CallUserRequest.

    Клиент отправляет единичный вызов батчем с меткой "order0000000000"
    и разворачивает ответ (см. extract_from_batch_response + run):
    - список -> первая строка (пустой список остаётся пустым);
    - словарь из одного ключа -> содержимое (список внутри — до первой
      строки: так crm.duplicate.findbycomm {"CONTACT": [23, 25]} превращается
      в 23, теряя и тип сущности, и остальные ID);
    - словарь со многими ключами -> обёртка {"order0000000000": словарь}
      (так отвечает, например, crm.deal.get);
    - скаляр -> скаляр.
    """
    first = result
    if isinstance(first, dict) and len(first) == 1:
        first = next(iter(first.values()))
    elif isinstance(first, dict):
        return {"order0000000000": first}
    if isinstance(first, list):
        return first[0] if first else []
    return first


# Размер страницы списочных методов REST Bitrix24: сервер отдаёт максимум
# 50 строк за запрос и сообщает total/next для дочитывания остатка.
BITRIX_PAGE_SIZE = 50


def apply_server_order(rows: Any, order: Any) -> Any:
    """Серверная сортировка списка по параметру order (поддержан ключ ID).

    Реальный портал сортирует выдачу .list-методов по запрошенному order ДО
    нарезки на страницы — фейки обязаны делать так же, иначе «первая страница
    DESC» в тестах не совпадала бы с боевой выдачей.
    """
    if not isinstance(rows, list) or not isinstance(order, dict):
        return rows
    direction = ""
    for key, value in order.items():
        if str(key).strip().upper() == "ID":
            direction = str(value).strip().upper()
    if direction not in ("ASC", "DESC"):
        return rows

    def row_id(row: Any) -> int:
        if not isinstance(row, dict):
            return 0
        try:
            return int(row.get("ID") or row.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(rows, key=row_id, reverse=direction == "DESC")


def paginate_result(result: Any, order: Any, start: Any) -> tuple[Any, dict]:
    """Серверная нарезка списка на страницу: (страница, служебные поля).

    Возвращает страницу по start с учётом order и поля конверта ответа
    (total и, если остались строки, next) — как их шлёт реальный портал.
    """
    if not isinstance(result, list):
        return result, {}
    ordered = apply_server_order(result, order)
    offset = int(start or 0)
    page = ordered[offset : offset + BITRIX_PAGE_SIZE]
    extra: dict = {"total": len(ordered)}
    if offset + BITRIX_PAGE_SIZE < len(ordered):
        extra["next"] = offset + BITRIX_PAGE_SIZE
    return page, extra


class SemanticBitrixFake:
    """База фейков Bitrix24 в памяти с клиентской семантикой fast-bitrix24.

    Наследники реализуют _dispatch(method, params) — «серверную» часть
    портала. call/get_all поверх неё повторяют поведение настоящего клиента:
    разворачивание ответов, raw-режим, запрет order/start в get_all и
    постраничную выдачу (сервер отдаёт максимум 50 строк за запрос; get_all
    дочитывает страницы отдельными «сетевыми» вызовами _dispatch, как
    настоящий клиент — отдельными запросами).
    """

    async def _dispatch(self, method: str, params: dict) -> Any:
        raise NotImplementedError

    async def call(self, method: str, items: dict | None = None, raw: bool = False):
        params = items or {}
        result = await self._dispatch(method, params)
        if raw:
            page, extra = paginate_result(result, params.get("order"), params.get("start"))
            return {"result": page, **extra}
        return mangle_call_result(result)

    async def call_once(self, method: str, items: dict | None = None):
        """Одна прямая отправка без batch и внутренних повторов."""
        return mangle_call_result(await self._dispatch(method, items or {}))

    async def get_all(self, method: str, params: dict | None = None):
        check_get_all_params(params)
        payload = add_default_order(method, params)
        rows: list = []
        start = 0
        while True:
            # одна итерация цикла = один «сетевой» запрос настоящего get_all
            result = unwrap_get_all_result(
                await self._dispatch(method, {**payload, "start": start})
            )
            page, extra = paginate_result(result, payload.get("order"), start)
            if not isinstance(page, list):
                return page
            rows.extend(page)
            next_start = extra.get("next")
            if next_start is None:
                return rows
            start = next_start


class HttpBitrix:
    """Тестовый httpx-клиент Bitrix24 с семантикой fast-bitrix24 (call/get_all).

    Транспорт — HTTP (перехватывается respx), а видимое поведение call() и
    get_all() повторяет настоящий клиент: см. помощники семантики выше.
    get_all дочитывает страницы по next из ответа сервера — отдельными
    HTTP-запросами, как настоящий клиент.
    """

    def __init__(self, url: str = WEBHOOK) -> None:
        self.url = url.rstrip("/") + "/"

    async def _request(self, method: str, payload: dict | None) -> Any:
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.url + method + ".json", json=payload or {})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _raise_application_error(data: dict) -> None:
        if "error" in data:
            # Настоящий клиент доносит ошибку Bitrix (например, "Not found")
            # исключением ErrorInServerResponseException — фейк делает так же,
            # чтобы код, различающий «сервер отказал» и «сеть упала»,
            # тестировался на боевом типе ошибки.
            raise ErrorInServerResponseException(
                f"{data['error']}: {data.get('error_description', '')}"
            )

    async def call(self, method: str, items: dict | None = None, raw: bool = False):
        data = await self._request(method, items)
        if raw:
            return data
        self._raise_application_error(data)
        return mangle_call_result(data["result"])

    async def call_once(self, method: str, items: dict | None = None):
        data = _checked_raw_response(await self._request(method, items))
        self._raise_application_error(data)
        return mangle_call_result(data.get("result"))

    async def get_all(self, method: str, params: dict | None = None):
        check_get_all_params(params)
        payload = add_default_order(method, params)
        rows: list = []
        start: Any = None
        while True:
            page_params = payload if start is None else {**payload, "start": start}
            data = await self._request(method, page_params)
            # Pinned fast-bitrix24==1.8.12 теряет top-level application error
            # в get_all и возвращает None. Фейк закрепляет именно этот контракт;
            # safety-critical код обязан ходить raw-вызовом и проверять envelope.
            if isinstance(data, dict) and "error" in data:
                return None
            result = unwrap_get_all_result(data["result"])
            if not isinstance(result, list):
                return result
            rows.extend(result)
            start = data.get("next")
            if start is None:
                return rows


@pytest.fixture
def bx() -> HttpBitrix:
    return HttpBitrix()


# ---------------------------------------------------------------------------
# Обвязка Telegram: офлайн-бот и билдеры апдейтов
# ---------------------------------------------------------------------------

_ids = itertools.count(1000)


class RecordingSession(BaseSession):
    """Сессия Bot API без сети: записывает исходящие методы.

    На send_message возвращает правдоподобный Message (нужен для edit/reply),
    на get_file — описание файла (нужно для скачивания голосовых),
    на остальные методы — True (валидно для answerCallbackQuery и т.п.).
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # Размер файла в ответе getFile: тесты лимита голосовых подставляют
        # сюда «настоящий» размер, который Telegram сообщает до скачивания.
        self.get_file_size: int | None = 100
        # Сколько раз скачивалось содержимое файла (download_file).
        self.downloads = 0
        # Содержимое «скачиваемого» файла кусками: тесты лимита подставляют
        # большой поток и считают, сколько кусков реально прочитано.
        self.stream_chunks: list[bytes] = [b""]
        self.streamed_chunks = 0

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            markup = method.reply_markup
            return Message(
                message_id=next(_ids),
                date=int(time.time()),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
            )
        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id,
                file_unique_id="u" + method.file_id,
                file_size=self.get_file_size,
                file_path="voice/audio.ogg",
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        self.downloads += 1
        for chunk in self.stream_chunks:
            self.streamed_chunks += 1
            yield chunk

    @property
    def sent_messages(self) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage)]

    @property
    def sent_texts(self) -> list[str]:
        return [m.text for m in self.sent_messages]


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
async def bot(session: RecordingSession):
    tg_bot = Bot(token="12345:TESTTOKEN", session=session)
    yield tg_bot
    await tg_bot.session.close()


def user_dict(user_id: int = 1) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": "Тест"}


def message_dict(
    text: str | None = None,
    user_id: int = 1,
    chat_id: int | None = None,
    **extra: Any,
) -> dict:
    return {
        "message_id": next(_ids),
        "date": int(time.time()),
        "chat": {"id": chat_id if chat_id is not None else user_id, "type": "private"},
        "from": user_dict(user_id),
        "text": text,
        **extra,
    }


def make_message_update(
    bot: Bot, text: str | None = None, user_id: int = 1, **extra: Any
) -> Update:
    """Апдейт с текстовым сообщением, привязанный к боту (как из сети)."""
    return Update.model_validate(
        {"update_id": next(_ids), "message": message_dict(text, user_id=user_id, **extra)},
        context={"bot": bot},
    )


def make_voice_update(
    bot: Bot,
    duration: int = 5,
    file_size: int | None = 1024,
    user_id: int = 1,
    **extra: Any,
) -> Update:
    """Апдейт с голосовым сообщением. file_size=None — размера в апдейте нет."""
    voice: dict[str, Any] = {
        "file_id": f"voice{next(_ids)}",
        "file_unique_id": f"uv{next(_ids)}",
        "duration": duration,
        "mime_type": "audio/ogg",
    }
    if file_size is not None:
        voice["file_size"] = file_size
    return Update.model_validate(
        {
            "update_id": next(_ids),
            "message": message_dict(None, user_id=user_id, voice=voice, **extra),
        },
        context={"bot": bot},
    )


def make_callback_update(
    bot: Bot, data: str, user_id: int = 1, message: dict | None = None
) -> Update:
    """Апдейт с нажатием inline-кнопки."""
    return Update.model_validate(
        {
            "update_id": next(_ids),
            "callback_query": {
                "id": str(next(_ids)),
                "from": user_dict(user_id),
                "chat_instance": "test",
                "data": data,
                "message": message or message_dict("карточка", user_id=user_id),
            },
        },
        context={"bot": bot},
    )
