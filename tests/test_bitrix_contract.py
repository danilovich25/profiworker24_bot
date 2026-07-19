"""Контракт клиента fast-bitrix24==1.8.12 и паритет тестовых фейков.

Первая часть пинит фактическое поведение НАСТОЯЩЕГО pinned-клиента офлайн:
подменяется только транспорт (ServerRequestHandler.single_request), вся
клиентская обвязка боевая — icontract-проверки параметров, батч-обёртка
call() и разворачивание ответов. Часть форм подтверждена и живыми read-only
запросами к реальному порталу (get_all+order -> ViolationError; get_all без
order -> полный список; call('crm.deal.list') -> dict первой строки;
@CONTACT_ID работает; %PHONE возвращает чужой контакт).

Вторая часть прогоняет те же сценарии через тестовые фейки и требует того же
видимого поведения: фикс, «прошедший» на более мягком фейке, в проде упал бы
на настоящем клиенте — именно так 279 зелёных тестов пропустили реальные
дефекты /find и /last.
"""

import httpx
import pytest
from fast_bitrix24 import BitrixAsync
from fast_bitrix24.server_response import ErrorInServerResponseException
from icontract import ViolationError

from app.services.bitrix import MalformedBitrixResponse, NonRetryingBitrixAsync
from tests.conftest import (
    WEBHOOK,
    HttpBitrix,
    SemanticBitrixFake,
    call_json,
    method_url,
    request_json,
)

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

DEAL_ROWS = [{"ID": "26"}, {"ID": "40"}]


async def test_non_idempotent_call_has_one_transport_attempt(monkeypatch):
    bx = NonRetryingBitrixAsync(WEBHOOK, verbose=False)
    attempts = 0

    async def fail_once(method, params=None):
        nonlocal attempts
        attempts += 1
        raise ConnectionResetError("ответ потерян")

    monkeypatch.setattr(bx.srh, "request_attempt", fail_once)
    with pytest.raises(ConnectionResetError):
        await bx.call_once("crm.deal.add", {"fields": {"TITLE": "Тест"}})
    assert attempts == 1


async def test_non_idempotent_call_rejects_empty_error_code(monkeypatch):
    """call_once проверяет наличие error, а не truthiness его значения."""
    bx = NonRetryingBitrixAsync(WEBHOOK, verbose=False)

    async def empty_error(method, params=None):
        return {"error": "", "error_description": "Явный отказ"}

    monkeypatch.setattr(bx.srh, "request_attempt", empty_error)
    with pytest.raises(ErrorInServerResponseException, match="Явный отказ"):
        await bx.call_once("crm.contact.add", {"fields": {"NAME": "Иван"}})


@pytest.mark.parametrize("payload", [{}, {"result": None}, {"result": False}])
async def test_real_and_http_fake_call_once_parse_malformed_method_result(
    monkeypatch, respx_mock, payload
):
    """HTTP-фейк повторяет parser реального call_once для пустого result."""
    real = NonRetryingBitrixAsync(WEBHOOK, verbose=False)

    async def canned(method, params=None):
        return payload

    monkeypatch.setattr(real.srh, "request_attempt", canned)
    fake = HttpBitrix()
    respx_mock.post(method_url("crm.timeline.comment.add")).mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert await fake.call_once("crm.timeline.comment.add") == await real.call_once(
        "crm.timeline.comment.add"
    )


@pytest.mark.parametrize("payload", [None, [], "broken"])
async def test_real_non_idempotent_call_rejects_malformed_as_ambiguous(monkeypatch, payload):
    """HTTP 200 неправильной формы имеет отдельный, неоднозначный тип ошибки."""
    bx = NonRetryingBitrixAsync(WEBHOOK, verbose=False)

    async def malformed(method, params=None):
        return payload

    monkeypatch.setattr(bx.srh, "request_attempt", malformed)
    with pytest.raises(MalformedBitrixResponse):
        await bx.call_once("crm.deal.add", {"fields": {"TITLE": "Тест"}})


def batch_envelope(cmd_result, errors: dict | None = None) -> dict:
    """Конверт ответа сервера на батч-запрос (так call() ходит в Bitrix)."""
    return {
        "result": {
            "result": {} if errors else {"order0000000000": cmd_result},
            "result_error": errors or [],
            "result_total": {},
            "result_next": {},
            "result_time": {},
        },
        "time": {},
    }


def plain_envelope(result, total: int | None = None) -> dict:
    """Конверт обычного (не батч) ответа: так ходят get_all и call(raw=True)."""
    envelope: dict = {"result": result, "time": {}}
    if total is not None:
        envelope["total"] = total
    return envelope


@pytest.fixture
def real_bx():
    """Настоящий BitrixAsync с канированным транспортом.

    Возвращает (клиент, canned, sent): canned — очередь ответов сервера,
    sent — список (метод, параметры) фактически отправленных запросов.
    """
    bx = BitrixAsync(WEBHOOK, verbose=False)
    canned: list[dict] = []
    sent: list[tuple[str, dict | None]] = []

    async def fake_single_request(method, params=None):
        sent.append((method, params))
        return canned.pop(0)

    bx.srh.single_request = fake_single_request
    return bx, canned, sent


# ---------------------------------------------------------------------------
# Настоящий клиент: параметры get_all
# ---------------------------------------------------------------------------


async def test_real_get_all_rejects_order(real_bx):
    """get_all с order падает ViolationError ДО обращения к серверу."""
    bx, _, sent = real_bx
    with pytest.raises(ViolationError):
        await bx.get_all("crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}})
    assert sent == []  # до транспорта дело не дошло


async def test_real_get_all_rejects_start(real_bx):
    bx, _, sent = real_bx
    with pytest.raises(ViolationError):
        await bx.get_all("crm.deal.list", {"select": ["ID"], "start": 50})
    assert sent == []


async def test_real_get_all_returns_full_list_and_adds_asc_order(real_bx):
    """get_all без order отдаёт полный список и сам сортирует по ID ASC."""
    bx, canned, sent = real_bx
    canned.append(plain_envelope(DEAL_ROWS, total=2))

    rows = await bx.get_all("crm.deal.list", {"select": ["ID"]})

    assert rows == DEAL_ROWS
    method, params = sent[-1]
    assert method == "crm.deal.list"
    assert params["order"] == {"ID": "ASC"}


async def test_real_get_all_unwraps_nested_result(real_bx):
    """tasks.task.list отвечает {"tasks": [...]} — клиент отдаёт сразу список."""
    bx, canned, _ = real_bx
    canned.append(plain_envelope({"tasks": [{"id": "77", "title": "x"}]}, total=1))

    rows = await bx.get_all("tasks.task.list", {"filter": {"TAG": "k"}, "select": ["ID"]})

    assert rows == [{"id": "77", "title": "x"}]


PAGED_ROWS = [{"ID": str(i)} for i in range(1, 76)]  # 75 сделок — больше страницы


async def test_real_get_all_paginates_over_50(real_bx):
    """Сервер отдал 50 строк и total=75 — get_all дочитывает вторую страницу.

    Каждая страница — отдельный сетевой запрос: широкий фильтр на большом
    портале превращается в десятки запросов, поэтому боевой код обязан
    ограничивать чтение сам (raw-вызов первой страницей), а фейки — вести
    себя так же постранично.
    """
    bx, canned, sent = real_bx
    canned.append(plain_envelope(PAGED_ROWS[:50], total=75))
    canned.append(batch_envelope(PAGED_ROWS[50:]))

    rows = await bx.get_all("crm.deal.list", {"select": ["ID"]})

    # клиент вернул все 75 строк (порядок после дедупликации не гарантирован)
    assert {row["ID"] for row in rows} == {row["ID"] for row in PAGED_ROWS}
    assert len(sent) == 2  # первая страница + дочитывание остатка
    method, params = sent[1]
    assert method == "batch"  # остаток запрашивается батчем со start=50
    assert "start=50" in str(params["cmd"])


async def test_real_get_all_returns_none_for_top_level_application_error(real_bx):
    """Закрепляем опасную особенность pinned-клиента, которую prod-код обходит."""
    bx, canned, _ = real_bx
    canned.append({"error": "ACCESS_DENIED", "error_description": "Access denied"})

    assert await bx.get_all("crm.deal.list", {"select": ["ID"]}) is None


async def test_real_call_raw_accepts_order_and_start(real_bx):
    """raw-вызов пропускает order/start на сервер и отдаёт конверт с next.

    На этом держится ограниченное чтение: первая страница .list-метода
    запрашивается напрямую (в обход get_all, который тянет все страницы).
    """
    bx, canned, sent = real_bx
    envelope = plain_envelope(PAGED_ROWS[:50], total=75)
    envelope["next"] = 50
    canned.append(envelope)

    resp = await bx.call(
        "crm.deal.list",
        {"select": ["ID"], "order": {"ID": "DESC"}, "start": 0},
        raw=True,
    )

    assert resp["result"] == PAGED_ROWS[:50]
    assert resp["total"] == 75 and resp["next"] == 50
    method, params = sent[-1]
    assert method == "crm.deal.list"
    assert params["order"] == {"ID": "DESC"} and params["start"] == 0


# ---------------------------------------------------------------------------
# Настоящий клиент: разворачивание call()
# ---------------------------------------------------------------------------


async def test_real_call_list_returns_first_row_only(real_bx):
    """call('crm.deal.list') разворачивает список до ПЕРВОЙ строки."""
    bx, canned, _ = real_bx
    canned.append(batch_envelope(DEAL_ROWS))

    row = await bx.call("crm.deal.list", {"select": ["ID"]})

    assert row == {"ID": "26"}  # не список: остальные строки потеряны


async def test_real_call_findbycomm_loses_entity_and_ids(real_bx):
    """call('crm.duplicate.findbycomm') разворачивает {"CONTACT": [...]} до
    первого ID: и тип сущности, и остальные контакты теряются — поэтому
    боевой код обязан ходить в findbycomm только с raw=True."""
    bx, canned, _ = real_bx
    canned.append(batch_envelope({"CONTACT": [23, 25]}))

    result = await bx.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": ["+7"]})

    assert result == 23


async def test_real_call_findbycomm_empty(real_bx):
    bx, canned, _ = real_bx
    canned.append(batch_envelope([]))

    result = await bx.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": ["+7"]})

    assert result == []


async def test_real_call_raw_returns_envelope(real_bx):
    """raw=True отдаёт весь конверт ответа сервера без преобразований."""
    bx, canned, _ = real_bx
    canned.append(plain_envelope({"CONTACT": [23, 25]}))

    resp = await bx.call(
        "crm.duplicate.findbycomm", {"type": "PHONE", "values": ["+7"]}, raw=True
    )

    assert resp["result"] == {"CONTACT": [23, 25]}


async def test_real_call_get_wraps_dict_result(real_bx):
    """call('crm.deal.get') возвращает словарь сделки В ОБЁРТКЕ служебного
    ключа батча — вызывающий обязан её снимать."""
    bx, canned, _ = real_bx
    deal = {"ID": "26", "TITLE": "t", "STAGE_ID": "NEW"}
    canned.append(batch_envelope(deal))

    result = await bx.call("crm.deal.get", {"id": 26})

    assert result == {"order0000000000": deal}


async def test_real_call_scalar_result(real_bx):
    """crm.contact.add и подобные возвращают скаляр как есть."""
    bx, canned, _ = real_bx
    canned.append(batch_envelope(15))

    assert await bx.call("crm.contact.add", {"fields": {"NAME": "И"}}) == 15


async def test_real_call_task_add_unwraps_single_key(real_bx):
    """tasks.task.add отвечает {"task": {...}} — клиент отдаёт внутренний словарь."""
    bx, canned, _ = real_bx
    canned.append(batch_envelope({"task": {"id": 77, "title": "x"}}))

    result = await bx.call("tasks.task.add", {"fields": {"TITLE": "x"}})

    assert result == {"id": 77, "title": "x"}


async def test_real_call_error_carries_description(real_bx):
    """Ошибка команды в батче доносит error_description (например, Not found)."""
    bx, canned, _ = real_bx
    canned.append(
        batch_envelope(
            None, errors={"order0000000000": {"error": "", "error_description": "Not found"}}
        )
    )

    with pytest.raises(ErrorInServerResponseException, match="Not found"):
        await bx.call("crm.deal.get", {"id": 999})


# ---------------------------------------------------------------------------
# Паритет фейка HttpBitrix с настоящим клиентом
# ---------------------------------------------------------------------------


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json={"result": payload})


@pytest.fixture
def bx() -> HttpBitrix:
    return HttpBitrix()


async def test_fake_get_all_rejects_order_and_start(bx):
    with pytest.raises(ViolationError):
        await bx.get_all("crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}})
    with pytest.raises(ViolationError):
        await bx.get_all("crm.deal.list", {"select": ["ID"], "start": 50})


async def test_fake_get_all_full_list_and_asc_order(bx, respx_mock):
    route = respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok(DEAL_ROWS))

    rows = await bx.get_all("crm.deal.list", {"select": ["ID"]})

    assert rows == DEAL_ROWS
    assert request_json(route)["order"] == {"ID": "ASC"}


async def test_fake_get_all_unwraps_nested_result(bx, respx_mock):
    respx_mock.post(method_url("tasks.task.list")).mock(
        return_value=_ok({"tasks": [{"id": "77", "title": "x"}]})
    )

    rows = await bx.get_all("tasks.task.list", {"filter": {"TAG": "k"}, "select": ["ID"]})

    assert rows == [{"id": "77", "title": "x"}]


async def test_fake_get_all_paginates_over_50(bx, respx_mock):
    """HttpBitrix дочитывает страницы по next, как настоящий get_all."""
    route = respx_mock.post(method_url("crm.deal.list")).mock(
        side_effect=[
            httpx.Response(
                200, json={"result": PAGED_ROWS[:50], "total": 75, "next": 50}
            ),
            httpx.Response(200, json={"result": PAGED_ROWS[50:], "total": 75}),
        ]
    )

    rows = await bx.get_all("crm.deal.list", {"select": ["ID"]})

    assert {row["ID"] for row in rows} == {row["ID"] for row in PAGED_ROWS}
    assert route.call_count == 2  # страница + дочитывание
    assert call_json(route.calls[1])["start"] == 50


async def test_fake_get_all_matches_real_top_level_application_error(bx, respx_mock):
    """Http fake воспроизводит None настоящего get_all на application error."""
    respx_mock.post(method_url("crm.deal.list")).mock(
        return_value=httpx.Response(
            200, json={"error": "ACCESS_DENIED", "error_description": "Access denied"}
        )
    )

    assert await bx.get_all("crm.deal.list", {"select": ["ID"]}) is None


@pytest.mark.parametrize("payload", [None, [], "broken"])
async def test_fake_non_idempotent_call_matches_real_malformed_response(
    bx, respx_mock, payload
):
    respx_mock.post(method_url("crm.deal.add")).mock(
        return_value=(
            httpx.Response(200, content=b"null")
            if payload is None
            else httpx.Response(200, json=payload)
        )
    )

    with pytest.raises(MalformedBitrixResponse):
        await bx.call_once("crm.deal.add", {"fields": {"TITLE": "Тест"}})


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_fake_rejects_http_error_statuses(bx, respx_mock, status):
    """HTTP fake не разбирает тело 4xx/5xx как успешный result."""
    respx_mock.post(method_url("crm.deal.add")).mock(
        return_value=httpx.Response(status, json={"result": 154})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await bx.call_once("crm.deal.add", {"fields": {"TITLE": "Тест"}})


async def test_fake_call_list_returns_first_row_only(bx, respx_mock):
    respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok(DEAL_ROWS))

    assert await bx.call("crm.deal.list", {"select": ["ID"]}) == {"ID": "26"}


async def test_fake_call_findbycomm_mangles_like_real(bx, respx_mock):
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        side_effect=[_ok({"CONTACT": [23, 25]}), _ok([])]
    )

    assert await bx.call("crm.duplicate.findbycomm", {"values": ["+7"]}) == 23
    assert await bx.call("crm.duplicate.findbycomm", {"values": ["+7"]}) == []


async def test_fake_call_raw_returns_envelope(bx, respx_mock):
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23, 25]})
    )

    resp = await bx.call("crm.duplicate.findbycomm", {"values": ["+7"]}, raw=True)

    assert resp["result"] == {"CONTACT": [23, 25]}


async def test_fake_call_get_wraps_dict_result(bx, respx_mock):
    deal = {"ID": "26", "TITLE": "t", "STAGE_ID": "NEW"}
    respx_mock.post(method_url("crm.deal.get")).mock(return_value=_ok(deal))

    assert await bx.call("crm.deal.get", {"id": 26}) == {"order0000000000": deal}


async def test_fake_call_task_add_unwraps_single_key(bx, respx_mock):
    respx_mock.post(method_url("tasks.task.add")).mock(
        return_value=_ok({"task": {"id": 77, "title": "x"}})
    )

    assert await bx.call("tasks.task.add", {"fields": {"TITLE": "x"}}) == {
        "id": 77,
        "title": "x",
    }


async def test_fake_call_error_carries_description(bx, respx_mock):
    """Ошибка сервера у фейка — тот же тип, что у настоящего клиента."""
    respx_mock.post(method_url("crm.deal.get")).mock(
        return_value=httpx.Response(
            200, json={"error": "", "error_description": "Not found"}
        )
    )

    with pytest.raises(ErrorInServerResponseException, match="Not found"):
        await bx.call("crm.deal.get", {"id": 999})


# ---------------------------------------------------------------------------
# Паритет фейков в памяти (портал с квирками реального Bitrix24)
# ---------------------------------------------------------------------------


async def test_inmemory_fakes_reject_order_in_get_all():
    from tests.test_handlers_messages import FakeBitrix
    from tests.test_search import FakeSearchBitrix

    for fake in (FakeBitrix(), FakeSearchBitrix()):
        with pytest.raises(ViolationError):
            await fake.get_all("crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}})


async def test_inmemory_fake_call_list_first_row_only():
    from tests.test_search import FakeSearchBitrix

    fake = FakeSearchBitrix()
    row = await fake.call("crm.deal.list", {"select": ["ID"]})

    assert isinstance(row, dict)  # не список: как у настоящего клиента
    assert row["ID"] == "154"


async def test_inmemory_fake_findbycomm_raw_vs_plain():
    from tests.test_search import FakeSearchBitrix

    fake = FakeSearchBitrix()
    raw = await fake.call(
        "crm.duplicate.findbycomm", {"type": "PHONE", "values": ["+79141234567"]}, raw=True
    )
    plain = await fake.call(
        "crm.duplicate.findbycomm", {"type": "PHONE", "values": ["+79141234567"]}
    )

    assert raw["result"] == {"CONTACT": [15]}
    assert plain == 15  # без raw тип сущности и остальные ID теряются


async def test_inmemory_fake_percent_phone_returns_garbage():
    """Квирк портала: LIKE-фильтр по множественному PHONE игнорируется и
    возвращает посторонние контакты (подтверждено живым запросом)."""
    from tests.test_search import FakeSearchBitrix

    fake = FakeSearchBitrix()
    rows = await fake.get_all(
        "crm.contact.list", {"filter": {"%PHONE": "0000000"}, "select": ["ID"]}
    )

    assert rows  # несуществующий номер «нашёл» чужой контакт


async def test_inmemory_fake_exact_filter_with_list_is_garbage():
    """Квирк портала: список значений в ТОЧНОМ фильтре игнорируется (нужен @)."""
    from tests.test_search import FakeSearchBitrix

    fake = FakeSearchBitrix()
    rows = await fake.get_all(
        "crm.deal.list",
        {"filter": {"CONTACT_ID": ["999999"]}, "select": ["ID"]},
    )

    assert rows  # фильтр не применился: вернулись посторонние сделки


async def test_inmemory_fake_at_operator_filters_by_list():
    from tests.test_search import FakeSearchBitrix

    fake = FakeSearchBitrix()
    rows = await fake.get_all(
        "crm.deal.list", {"filter": {"@CONTACT_ID": [15]}, "select": ["ID"]}
    )

    assert {row["ID"] for row in rows} == {"154", "155"}
    empty = await fake.get_all(
        "crm.deal.list", {"filter": {"@CONTACT_ID": [999999]}, "select": ["ID"]}
    )
    assert empty == []


class PagingFake(SemanticBitrixFake):
    """Портал с 75 сделками: проверка постраничной выдачи фейков в памяти."""

    def __init__(self) -> None:
        self.dispatch_calls = 0

    async def _dispatch(self, method: str, params: dict):
        self.dispatch_calls += 1
        return [dict(row) for row in PAGED_ROWS]


async def test_inmemory_fake_get_all_paginates_over_50():
    """get_all фейка постраничен: 75 строк — два «сетевых» запроса."""
    fake = PagingFake()

    rows = await fake.get_all("crm.deal.list", {"select": ["ID"]})

    assert {row["ID"] for row in rows} == {row["ID"] for row in PAGED_ROWS}
    assert fake.dispatch_calls == 2  # две страницы — два запроса, как в бою


async def test_inmemory_fake_raw_list_pages_with_order_and_start():
    """raw-вызов .list у фейка: сервер сортирует по order, режет по start.

    Ровно так ограниченное чтение забирает «самую новую» первую страницу.
    """
    fake = PagingFake()

    first = await fake.call(
        "crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}, "start": 0}, raw=True
    )
    assert [row["ID"] for row in first["result"][:2]] == ["75", "74"]
    assert len(first["result"]) == 50
    assert first["total"] == 75 and first["next"] == 50

    second = await fake.call(
        "crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}, "start": 50}, raw=True
    )
    assert len(second["result"]) == 25
    assert second["result"][-1]["ID"] == "1"
    assert "next" not in second  # последняя страница
