"""Тесты сервиса Bitrix24. Полностью офлайн: HTTP перехватывает respx."""

import httpx
import pytest
from fast_bitrix24.server_response import ErrorInServerResponseException
from fast_bitrix24.srh import ServerError

from app.services import bitrix
from app.services.tasks import create_reminder_task, find_reminder_task
from tests.conftest import call_json, method_url, request_json

PHONE = "+79141234567"


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json={"result": payload})


def _error(code: str, description: str) -> httpx.Response:
    # Application error Bitrix приходит JSON-конвертом; HTTP-статусы
    # проверяются отдельно и не должны маскироваться этим помощником.
    return httpx.Response(200, json={"error": code, "error_description": description})


def _deal_uf_list() -> httpx.Response:
    """Ответ crm.deal.userfield.list со всеми обязательными полями.

    USER_TYPE_ID теперь тоже сверяется, поэтому фикстура отдаёт полную
    форму ответа Bitrix (имя с префиксом UF_CRM_ + тип).
    """
    return _ok(
        [
            {"FIELD_NAME": "UF_CRM_TG_MSG_ID", "USER_TYPE_ID": "string"},
            {"FIELD_NAME": "UF_CRM_EXPENSE", "USER_TYPE_ID": "double"},
            {"FIELD_NAME": "UF_CRM_PROFIT", "USER_TYPE_ID": "double"},
            {"FIELD_NAME": "UF_CRM_SERVICE_CATEGORY", "USER_TYPE_ID": "string"},
        ]
    )


def _contact_uf_list() -> httpx.Response:
    """Ответ crm.contact.userfield.list со всеми обязательными полями."""
    return _ok(
        [
            {"FIELD_NAME": "UF_CRM_TG_DRAFT_ID", "USER_TYPE_ID": "string"},
            {"FIELD_NAME": "UF_CRM_ORG", "USER_TYPE_ID": "string"},
        ]
    )


@pytest.mark.parametrize("payload", [None, False, True, 0, -1, "broken", {}, []])
@pytest.mark.parametrize(
    ("method", "writer"),
    [
        (
            "crm.contact.add",
            lambda bx: bitrix.resolve_contact(bx, "Иван", None),
        ),
        (
            "crm.deal.add",
            lambda bx: bitrix.create_deal(bx, 15, {"TITLE": "Тест"}, "msg:1:1"),
        ),
        (
            "crm.timeline.comment.add",
            lambda bx: bitrix.add_contact_timeline_comment(bx, 15, "Тест"),
        ),
    ],
)
async def test_unsafe_crm_writes_require_positive_scalar_id(
    bx, respx_mock, payload, method, writer
):
    """HTTP 200 без положительного ID остаётся неоднозначным исходом add."""
    if method == "crm.contact.add":
        respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    respx_mock.post(method_url(method)).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse) as caught:
        await writer(bx)

    assert bitrix.is_server_refusal(caught.value) is False


@pytest.mark.parametrize(
    "payload",
    [None, False, True, 0, -1, "77", {}, {"task": {}}, {"task": {"id": False}}],
)
async def test_task_add_requires_nested_task_with_positive_id(bx, respx_mock, payload):
    """tasks.task.add принимается только в штатной method-level форме."""
    respx_mock.post(method_url("tasks.task.add")).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse) as caught:
        await create_reminder_task(bx, "Позвонить")

    assert bitrix.is_server_refusal(caught.value) is False


def test_normalize_phone_various():
    assert bitrix.normalize_phone("89141234567") == PHONE
    assert bitrix.normalize_phone("79141234567") == PHONE
    assert bitrix.normalize_phone("+79141234567") == PHONE
    assert bitrix.normalize_phone("8 (914) 123-45-67") == PHONE
    assert bitrix.normalize_phone("12345") is None


@pytest.mark.parametrize("status, expected", [(400, True), (404, True), (500, False), (503, False)])
def test_http_status_refusal_classification(status, expected):
    request = httpx.Request("POST", "https://portal.example/rest/method.json")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("status", request=request, response=response)

    assert bitrix.is_server_refusal(error) is expected


def test_server_error_is_ambiguous_but_application_error_is_refusal():
    assert bitrix.is_server_refusal(ServerError("сервер не ответил однозначно")) is False
    assert bitrix.is_server_refusal(ErrorInServerResponseException("METHOD_NOT_FOUND")) is True


@pytest.mark.parametrize("payload", [None, [], "broken"])
def test_malformed_raw_response_is_ambiguous(payload):
    """Невалидная форма HTTP 200 не доказывает отказ unsafe-записи."""
    with pytest.raises(bitrix.MalformedBitrixResponse) as caught:
        bitrix._checked_raw_response(payload)

    assert bitrix.is_server_refusal(caught.value) is False


@pytest.mark.parametrize(
    ("reader", "method"),
    [
        (lambda bx: bitrix.find_contact_by_draft_id(bx, "draft-1"), "crm.contact.list"),
        (lambda bx: bitrix.find_deal_by_key(bx, "msg:1:1"), "crm.deal.list"),
        (lambda bx: find_reminder_task(bx, "msg:1:1"), "tasks.task.list"),
    ],
)
async def test_idempotency_reads_reject_application_error(bx, respx_mock, reader, method):
    """Ошибка list не превращается в «сущность не найдена»."""
    respx_mock.post(method_url(method)).mock(
        return_value=_error("ACCESS_DENIED", "Access denied")
    )

    with pytest.raises(ErrorInServerResponseException, match="ACCESS_DENIED"):
        await reader(bx)


async def test_find_contact_by_findbycomm_hit(bx, respx_mock):
    dup = respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23]})
    )
    listing = respx_mock.post(method_url("crm.contact.list")).mock(return_value=_ok([]))

    assert await bitrix.find_contact(bx, PHONE) == 23
    # значения для поиска: полный номер и последние 10 цифр
    body = request_json(dup)
    assert body["entity_type"] == "CONTACT"
    assert body["type"] == "PHONE"
    assert body["values"] == [PHONE, "9141234567"]
    assert not listing.called  # подстрочного поиска по contact.list нет


async def test_find_contact_ids_returns_all_duplicates(bx, respx_mock):
    """Телефон у нескольких контактов-дублей: возвращаются все ID."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23, 25]})
    )

    assert await bitrix.find_contact_ids(bx, PHONE) == [23, 25]
    assert await bitrix.find_contact(bx, PHONE) == 23


@pytest.mark.parametrize(
    "payload",
    [None, False, "23", {"CONTACT": "23"}, {"CONTACT": [True]}, {"garbage": []}],
)
async def test_find_contact_ids_rejects_malformed_method_result(bx, respx_mock, payload):
    """Повреждённая схема поиска дублей не означает «контакта нет»."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse):
        await bitrix.find_contact_ids(bx, PHONE)


@pytest.mark.parametrize(
    ("reader", "method", "payload"),
    [
        (lambda bx: bitrix.find_contact_by_draft_id(bx, "draft-1"), "crm.contact.list", [{}]),
        (lambda bx: bitrix.find_deal_by_key(bx, "msg:1:1"), "crm.deal.list", {"garbage": []}),
        (lambda bx: find_reminder_task(bx, "msg:1:1"), "tasks.task.list", {"tasks": [{}]}),
    ],
)
async def test_idempotency_reads_require_method_schema_and_id(
    bx, respx_mock, reader, method, payload
):
    """Невалидная строка safety-read не превращается в «не найдено»."""
    respx_mock.post(method_url(method)).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse):
        await reader(bx)


async def test_find_contact_no_substring_fallback(bx, respx_mock):
    """Пустой findbycomm НЕ ведёт в %PHONE: LIKE по множественному PHONE портал
    игнорирует и возвращает чужой контакт — сделка цеплялась бы не к тому клиенту."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    listing = respx_mock.post(method_url("crm.contact.list")).mock(
        return_value=_ok([{"ID": "42"}])  # «чужой» контакт, который вернул бы %PHONE
    )

    assert await bitrix.find_contact(bx, PHONE) is None
    assert not listing.called  # к contact.list вообще не ходили


async def test_find_contact_error_propagates(bx, respx_mock):
    """Сбой findbycomm — ошибка, а не «не найден»: решает вызывающий."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_error("INTERNAL_SERVER_ERROR", "Server error")
    )

    with pytest.raises(ErrorInServerResponseException, match="INTERNAL_SERVER_ERROR"):
        await bitrix.find_contact(bx, PHONE)


async def test_find_contact_empty_error_code_still_propagates(bx, respx_mock):
    """Наличие error делает raw-конверт ошибочным даже при пустом коде."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=httpx.Response(
            200, json={"error": "", "error_description": "Ошибка поиска дублей"}
        )
    )

    with pytest.raises(ErrorInServerResponseException, match="Ошибка поиска дублей"):
        await bitrix.find_contact(bx, PHONE)


async def test_create_or_update_contact_creates_new(bx, respx_mock):
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(15))

    contact_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=PHONE, org="ООО Ромашка", comment="замена крана"
    )

    assert contact_id == 15
    fields = request_json(add)["fields"]
    assert fields["NAME"] == "Иван"
    assert fields["PHONE"] == [{"VALUE": PHONE, "VALUE_TYPE": "WORK"}]
    # у контакта нет поля COMPANY_TITLE (contact.add молча игнорирует его):
    # организация хранится в UF-поле
    assert fields[bitrix.UF_ORG] == "ООО Ромашка"
    assert "COMPANY_TITLE" not in fields


async def test_create_or_update_contact_dup_error_does_not_create(bx, respx_mock):
    """Сбой поиска дублей не маскируется созданием нового контакта."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_error("INTERNAL_SERVER_ERROR", "Server error")
    )
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(16))

    with pytest.raises(ErrorInServerResponseException, match="INTERNAL_SERVER_ERROR"):
        await bitrix.create_or_update_contact(bx, name="Иван", phone=PHONE)
    assert not add.called


async def test_create_or_update_contact_without_phone(bx, respx_mock):
    # сотрудник ответил "нет" на вопрос про телефон: поиск дублей невозможен,
    # контакт создаётся сразу и без поля PHONE
    dup = respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    listing = respx_mock.post(method_url("crm.contact.list")).mock(return_value=_ok([]))
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(16))

    contact_id = await bitrix.create_or_update_contact(bx, name="Иван", phone=None)

    assert contact_id == 16
    assert not dup.called and not listing.called
    fields = request_json(add)["fields"]
    assert fields["NAME"] == "Иван"
    assert "PHONE" not in fields


async def test_phoneless_contact_dedup_via_draft_id(bx, respx_mock):
    listing = respx_mock.post(method_url("crm.contact.list")).mock(
        side_effect=[_ok([]), _ok([{"ID": "16"}])]
    )
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(16))

    first_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=None, draft_id="draft-123"
    )
    second_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=None, draft_id="draft-123"
    )

    assert (first_id, second_id) == (16, 16)
    assert add.call_count == 1
    assert request_json(add)["fields"][bitrix.UF_TG_DRAFT_ID] == "draft-123"
    assert request_json(listing)["filter"] == {bitrix.UF_TG_DRAFT_ID: "draft-123"}


async def test_create_or_update_contact_updates_existing_with_comment(bx, respx_mock):
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23]})
    )
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(99))
    update = respx_mock.post(method_url("crm.contact.update")).mock(return_value=_ok(True))
    comment = respx_mock.post(method_url("crm.timeline.comment.add")).mock(return_value=_ok(33))

    contact_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=PHONE, comment="повторное обращение"
    )

    assert contact_id == 23
    assert not add.called  # новый контакт не создаётся
    assert not update.called  # организации в заявке нет — UF не трогается
    fields = request_json(comment)["fields"]
    assert fields["ENTITY_ID"] == 23
    assert fields["ENTITY_TYPE"] == "contact"
    assert fields["COMMENT"] == "повторное обращение"


async def test_timeline_comment_uses_one_shot_call(bx, respx_mock, monkeypatch):
    """Неидемпотентный комментарий не проходит через retrying call()."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23]})
    )
    respx_mock.post(method_url("crm.timeline.comment.add")).mock(return_value=_ok(33))
    methods: list[str] = []
    original_call_once = bx.call_once

    async def tracked_call_once(method, items=None):
        methods.append(method)
        return await original_call_once(method, items)

    monkeypatch.setattr(bx, "call_once", tracked_call_once)
    await bitrix.create_or_update_contact(
        bx, name="Иван", phone=PHONE, comment="повторное обращение"
    )

    assert methods == ["crm.timeline.comment.add"]


async def test_existing_contact_gets_org_into_uf(bx, respx_mock):
    """Организация из заявки дописывается СУЩЕСТВУЮЩЕМУ контакту (update).

    Раньше UF_CRM_ORG заполнялся только при создании контакта: у найденного
    по телефону клиента поле оставалось пустым, и «/find <организация>» его
    не находил.
    """
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23]})
    )
    add = respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(99))
    update = respx_mock.post(method_url("crm.contact.update")).mock(return_value=_ok(True))
    respx_mock.post(method_url("crm.timeline.comment.add")).mock(return_value=_ok(33))

    contact_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=PHONE, org="ООО Ромашка", comment="замена крана"
    )

    assert contact_id == 23
    assert not add.called
    body = request_json(update)
    assert body["id"] == 23
    assert body["fields"] == {bitrix.UF_ORG: "ООО Ромашка"}


async def test_existing_contact_org_update_failure_keeps_order(bx, respx_mock):
    """Сбой записи организации не роняет заявку: контакт возвращается."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [23]})
    )
    respx_mock.post(method_url("crm.contact.update")).mock(
        return_value=_error("ACCESS_DENIED", "Access denied")
    )
    comment = respx_mock.post(method_url("crm.timeline.comment.add")).mock(return_value=_ok(33))

    contact_id = await bitrix.create_or_update_contact(
        bx, name="Иван", phone=PHONE, org="ООО Ромашка", comment="замена крана"
    )

    assert contact_id == 23
    assert comment.called  # история обращения дописана несмотря на сбой UF


async def test_create_deal_idempotent_duplicate(bx, respx_mock):
    respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok([{"ID": "154"}]))
    add = respx_mock.post(method_url("crm.deal.add")).mock(return_value=_ok(999))

    deal_id = await bitrix.create_deal_idempotent(
        bx, contact_id=23, fields={"TITLE": "заявка"}, tg_msg_key="msg:1:100"
    )

    assert deal_id == 154
    assert not add.called  # дубль сделки не создан


async def test_create_deal_idempotent_new(bx, respx_mock):
    listing = respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok([]))
    add = respx_mock.post(method_url("crm.deal.add")).mock(return_value=_ok(155))

    deal_id = await bitrix.create_deal_idempotent(
        bx,
        contact_id=23,
        fields={"TITLE": "сантехника: замена крана", "OPPORTUNITY": 5000},
        tg_msg_key="msg:1:101",
    )

    assert deal_id == 155
    assert request_json(listing)["filter"] == {bitrix.UF_TG_MSG_ID: "msg:1:101"}
    fields = request_json(add)["fields"]
    assert fields["TITLE"] == "сантехника: замена крана"
    assert fields["OPPORTUNITY"] == 5000
    assert fields["STAGE_ID"] == bitrix.STAGE_NEW
    assert fields["CONTACT_ID"] == 23
    assert fields[bitrix.UF_TG_MSG_ID] == "msg:1:101"


async def test_ensure_uf_fields_new_and_existing(bx, respx_mock):
    deal_route = respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        side_effect=[
            _ok(101),
            _error("ERROR_CORE", "Поле с таким названием уже существует"),
            _ok(102),
            _ok(104),
        ]
    )
    contact_route = respx_mock.post(method_url("crm.contact.userfield.add")).mock(
        return_value=_ok(103)
    )
    respx_mock.post(method_url("crm.deal.userfield.list")).mock(return_value=_deal_uf_list())
    respx_mock.post(method_url("crm.contact.userfield.list")).mock(
        return_value=_contact_uf_list()
    )

    await bitrix.ensure_uf_fields(bx)  # "уже существует" не считается ошибкой

    assert deal_route.call_count == 4
    names = [call_json(call)["fields"]["FIELD_NAME"] for call in deal_route.calls]
    assert names == ["TG_MSG_ID", "EXPENSE", "PROFIT", "SERVICE_CATEGORY"]
    category_field = call_json(deal_route.calls[-1])["fields"]
    assert category_field["USER_TYPE_ID"] == "string"
    assert category_field["EDIT_FORM_LABEL"] == "Категория услуги"
    contact_names = [call_json(call)["fields"]["FIELD_NAME"] for call in contact_route.calls]
    assert contact_names == ["TG_DRAFT_ID", "ORG"]
    org_field = call_json(contact_route.calls[-1])["fields"]
    assert org_field["USER_TYPE_ID"] == "string"
    assert org_field["EDIT_FORM_LABEL"] == {"ru": "Организация"}


async def test_ensure_sources_adds_missing_and_renames_other(bx, respx_mock):
    """Справочник источников доводится до значений заказчика идемпотентно.

    «Авито» уже есть — не трогается; «Форпост» и «Сарафанное радио»
    добавляются; штатный OTHER («Другое») переименовывается в «Прочее».
    """
    respx_mock.post(method_url("crm.status.list")).mock(
        return_value=_ok(
            [
                {"ID": "1", "ENTITY_ID": "SOURCE", "STATUS_ID": "CALL", "NAME": "Звонок"},
                {"ID": "8", "ENTITY_ID": "SOURCE", "STATUS_ID": "OTHER", "NAME": "Другое"},
                {"ID": "20", "ENTITY_ID": "SOURCE", "STATUS_ID": "AVITO", "NAME": "Авито"},
            ]
        )
    )
    add = respx_mock.post(method_url("crm.status.add")).mock(return_value=_ok(21))
    update = respx_mock.post(method_url("crm.status.update")).mock(return_value=_ok(True))

    await bitrix.ensure_sources(bx)

    added = [call_json(call)["fields"] for call in add.calls]
    assert [f["STATUS_ID"] for f in added] == ["FORPOST", "SARAFAN"]
    assert all(f["ENTITY_ID"] == "SOURCE" for f in added)
    assert [f["NAME"] for f in added] == ["Форпост", "Сарафанное радио"]
    renamed = call_json(update.calls.last)
    assert renamed["id"] == "8"
    assert renamed["fields"]["NAME"] == "Прочее"


async def test_ensure_sources_noop_when_directory_is_ready(bx, respx_mock):
    respx_mock.post(method_url("crm.status.list")).mock(
        return_value=_ok(
            [
                {"ID": "8", "ENTITY_ID": "SOURCE", "STATUS_ID": "OTHER", "NAME": "Прочее"},
                {"ID": "20", "ENTITY_ID": "SOURCE", "STATUS_ID": "AVITO", "NAME": "Авито"},
                {"ID": "21", "ENTITY_ID": "SOURCE", "STATUS_ID": "FORPOST", "NAME": "Форпост"},
                {
                    "ID": "22",
                    "ENTITY_ID": "SOURCE",
                    "STATUS_ID": "SARAFAN",
                    "NAME": "Сарафанное радио",
                },
            ]
        )
    )
    add = respx_mock.post(method_url("crm.status.add")).mock(return_value=_ok(30))
    update = respx_mock.post(method_url("crm.status.update")).mock(return_value=_ok(True))

    await bitrix.ensure_sources(bx)

    assert add.call_count == 0
    assert update.call_count == 0


async def test_ensure_uf_fields_unexpected_error_raises(bx, respx_mock):
    respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        return_value=_error("ACCESS_DENIED", "Access denied")
    )

    try:
        await bitrix.ensure_uf_fields(bx)
    except ErrorInServerResponseException as exc:
        assert "ACCESS_DENIED" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка ACCESS_DENIED")


async def test_ensure_uf_fields_missing_deal_field_raises(bx, respx_mock):
    """Создание «прошло», но обязательного поля сделки в CRM нет — ошибка."""
    respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.contact.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.deal.userfield.list")).mock(
        return_value=_ok(
            [
                {"FIELD_NAME": "UF_CRM_TG_MSG_ID", "USER_TYPE_ID": "string"},
                {"FIELD_NAME": "UF_CRM_EXPENSE", "USER_TYPE_ID": "double"},
            ]
        )
    )
    respx_mock.post(method_url("crm.contact.userfield.list")).mock(
        return_value=_contact_uf_list()
    )

    with pytest.raises(bitrix.UFFieldsError, match="UF_CRM_PROFIT"):
        await bitrix.ensure_uf_fields(bx)


async def test_ensure_uf_fields_missing_contact_field_raises(bx, respx_mock):
    """Поле контакта UF_CRM_TG_DRAFT_ID обязательно так же, как поля сделки."""
    respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.contact.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.deal.userfield.list")).mock(return_value=_deal_uf_list())
    respx_mock.post(method_url("crm.contact.userfield.list")).mock(return_value=_ok([]))

    with pytest.raises(bitrix.UFFieldsError, match="UF_CRM_TG_DRAFT_ID"):
        await bitrix.ensure_uf_fields(bx)


async def test_ensure_uf_fields_wrong_type_raises(bx, respx_mock):
    """Поле есть, но с чужим типом (TG_MSG_ID как double) — работать нельзя."""
    respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.contact.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.deal.userfield.list")).mock(
        return_value=_ok(
            [
                {"FIELD_NAME": "UF_CRM_TG_MSG_ID", "USER_TYPE_ID": "double"},
                {"FIELD_NAME": "UF_CRM_EXPENSE", "USER_TYPE_ID": "double"},
                {"FIELD_NAME": "UF_CRM_PROFIT", "USER_TYPE_ID": "double"},
                {"FIELD_NAME": "UF_CRM_SERVICE_CATEGORY", "USER_TYPE_ID": "string"},
            ]
        )
    )
    respx_mock.post(method_url("crm.contact.userfield.list")).mock(
        return_value=_contact_uf_list()
    )

    with pytest.raises(bitrix.UFFieldsError, match="UF_CRM_TG_MSG_ID"):
        await bitrix.ensure_uf_fields(bx)


async def test_ensure_uf_fields_accepts_unprefixed_names(bx, respx_mock):
    """Имя без префикса UF_CRM_ в ответе списка тоже принимается."""
    respx_mock.post(method_url("crm.deal.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.contact.userfield.add")).mock(
        return_value=_error("ERROR_CORE", "уже существует")
    )
    respx_mock.post(method_url("crm.deal.userfield.list")).mock(
        return_value=_ok(
            [
                {"FIELD_NAME": "TG_MSG_ID", "USER_TYPE_ID": "string"},
                {"FIELD_NAME": "EXPENSE", "USER_TYPE_ID": "double"},
                {"FIELD_NAME": "PROFIT", "USER_TYPE_ID": "double"},
                {"FIELD_NAME": "SERVICE_CATEGORY", "USER_TYPE_ID": "string"},
            ]
        )
    )
    respx_mock.post(method_url("crm.contact.userfield.list")).mock(
        return_value=_ok(
            [
                {"FIELD_NAME": "TG_DRAFT_ID", "USER_TYPE_ID": "string"},
                {"FIELD_NAME": "ORG", "USER_TYPE_ID": "string"},
            ]
        )
    )

    await bitrix.ensure_uf_fields(bx)  # ошибок нет


async def test_find_deal_by_key(bx, respx_mock):
    """Поиск сделки по идемпотентному ключу: найдена / не найдена."""
    listing = respx_mock.post(method_url("crm.deal.list")).mock(
        side_effect=[_ok([{"ID": "154"}]), _ok([])]
    )

    assert await bitrix.find_deal_by_key(bx, "msg:1:100") == 154
    assert await bitrix.find_deal_by_key(bx, "msg:1:101") is None
    assert request_json(listing)["filter"] == {bitrix.UF_TG_MSG_ID: "msg:1:101"}


# ---------------------------------------------------------------------------
# Поиск и списки: /find, /last
# ---------------------------------------------------------------------------


async def test_get_deal_found_unwraps_batch_label(bx, respx_mock):
    """Успешный crm.deal.get: служебная обёртка батча снимается."""
    deal = {"ID": "154", "TITLE": "сантехника: замена крана", "STAGE_ID": "NEW"}
    respx_mock.post(method_url("crm.deal.get")).mock(return_value=_ok(deal))

    assert await bitrix.get_deal(bx, 154) == deal


@pytest.mark.parametrize("payload", [None, False, {}, {"TITLE": "без ID"}])
async def test_get_deal_rejects_malformed_success_result(bx, respx_mock, payload):
    """Повреждённый HTTP 200 не выдаётся за отсутствие сделки."""
    respx_mock.post(method_url("crm.deal.get")).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse):
        await bitrix.get_deal(bx, 154)


@pytest.mark.parametrize("payload", [None, {"garbage": []}, [{}], [{"ID": False}]])
async def test_limited_list_read_requires_rows_with_ids(bx, respx_mock, payload):
    """Ограниченный raw-поиск также закрыт при неверной method-схеме."""
    respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok(payload))

    with pytest.raises(bitrix.MalformedBitrixResponse):
        await bitrix._list_page(bx, "crm.deal.list", {"select": ["ID"]})


async def test_get_deal_not_found_is_none(bx, respx_mock):
    respx_mock.post(method_url("crm.deal.get")).mock(
        return_value=_error("ERROR_NOT_FOUND", "Not found")
    )

    assert await bitrix.get_deal(bx, 999) is None


async def test_get_deal_crm_error_propagates(bx, respx_mock):
    """403/сеть — не «не найдена»: ошибка пробрасывается, а не глотается."""
    respx_mock.post(method_url("crm.deal.get")).mock(
        return_value=_error("ACCESS_DENIED", "Access denied")
    )

    with pytest.raises(ErrorInServerResponseException, match="ACCESS_DENIED"):
        await bitrix.get_deal(bx, 154)


async def test_get_deal_method_not_found_is_error_not_none(bx, respx_mock):
    """ERROR_METHOD_NOT_FOUND — сбой интеграции, а не «сделка не найдена».

    Раньше маркер "not found" ловил и «Method not found!»: get_deal отвечал
    None, и /find врал «ничего не нашёл» вместо честного «поиск не работает».
    """
    respx_mock.post(method_url("crm.deal.get")).mock(
        return_value=_error("ERROR_METHOD_NOT_FOUND", "Method not found!")
    )

    with pytest.raises(ErrorInServerResponseException, match="METHOD_NOT_FOUND"):
        await bitrix.get_deal(bx, 154)


async def test_get_deal_http_404_text_is_not_entity_absence():
    """Внешний HTTP 404 «Not Found» не означает отсутствие сделки."""
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    headers = CIMultiDictProxy(CIMultiDict())
    request_info = RequestInfo(URL("https://bad.example/"), "POST", headers, URL("https://bad.example/"))
    error = ClientResponseError(request_info, (), status=404, message="Not Found")
    assert bitrix._is_deal_not_found(error) is False
    assert bitrix.is_server_refusal(error) is True


async def test_search_deals_by_phone_all_contacts_at_operator(bx, respx_mock):
    """Сделки собираются по ВСЕМ контактам-дублям через оператор @CONTACT_ID."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [15, 18]})
    )
    listing = respx_mock.post(method_url("crm.deal.list")).mock(
        return_value=_ok([{"ID": "154"}, {"ID": "155"}])
    )

    found = await bitrix.search_deals_by_phone(bx, PHONE)

    assert [row["ID"] for row in found.deals] == ["155", "154"]  # новые сверху
    assert found.truncated is False
    body = request_json(listing)
    assert body["filter"] == {"@CONTACT_ID": [15, 18]}
    # чтение ограничено: первая страница самых новых, а не полный get_all
    assert body["order"] == {"ID": "DESC"} and body["start"] == 0


async def test_search_deals_by_phone_no_contacts(bx, respx_mock):
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    listing = respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok([]))

    found = await bitrix.search_deals_by_phone(bx, PHONE)
    assert found.deals == [] and found.truncated is False
    assert not listing.called


async def test_search_deals_by_phone_reports_unread_next_page(bx, respx_mock):
    """51-я сделка не скрывается за якобы полным телефонным результатом."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(
        return_value=_ok({"CONTACT": [15]})
    )
    rows = [{"ID": str(i)} for i in range(100, 49, -1)]
    respx_mock.post(method_url("crm.deal.list")).mock(
        return_value=_page(rows[:50], total=51, next_start=50)
    )

    found = await bitrix.search_deals_by_phone(bx, PHONE)

    assert len(found.deals) == 50
    assert found.truncated is True


async def test_search_deals_by_text_merges_and_sorts(bx, respx_mock):
    """Текстовый поиск: %TITLE/%COMMENTS + контакты по имени через @CONTACT_ID."""
    deal_listing = respx_mock.post(method_url("crm.deal.list")).mock(
        side_effect=[
            _ok([{"ID": "154"}]),  # %TITLE
            _ok([{"ID": "154"}, {"ID": "157"}]),  # %COMMENTS (154 — дубль)
            _ok([{"ID": "155"}]),  # @CONTACT_ID
        ]
    )
    contact_listing = respx_mock.post(method_url("crm.contact.list")).mock(
        side_effect=[_ok([{"ID": "15"}]), _ok([]), _ok([])]  # %NAME, %LAST_NAME, UF_ORG
    )

    found = await bitrix.search_deals_by_text(bx, "ромашка")

    # без дублей, новые сверху; выборка полная — truncated не выставлен
    assert [row["ID"] for row in found.deals] == ["157", "155", "154"]
    assert found.truncated is False
    deal_filters = [call_json(call)["filter"] for call in deal_listing.calls]
    assert deal_filters == [
        {"%TITLE": "ромашка"},
        {"%COMMENTS": "ромашка"},
        {"@CONTACT_ID": [15]},
    ]
    # каждый запрос — ограниченная первая страница самых новых
    first_body = call_json(deal_listing.calls[0])
    assert first_body["order"] == {"ID": "DESC"} and first_body["start"] == 0
    contact_filters = [call_json(call)["filter"] for call in contact_listing.calls]
    # контакты ищутся по имени, фамилии и организации (точный фильтр по UF:
    # штатного поля организации у контакта нет, LIKE по UF ненадёжен)
    assert contact_filters == [
        {"%NAME": "ромашка"},
        {"%LAST_NAME": "ромашка"},
        {bitrix.UF_ORG: "ромашка"},
    ]


def _page(rows: list, total: int, next_start: int | None = None) -> httpx.Response:
    """Страница ответа сервера с total/next — как шлёт реальный портал."""
    payload: dict = {"result": rows, "total": total}
    if next_start is not None:
        payload["next"] = next_start
    return httpx.Response(200, json=payload)


async def test_search_deals_by_text_bounded_requests_no_false_negative(bx, respx_mock):
    """Широкий запрос: чтение ограничено страницами, старый контакт не теряется.

    Сценарий: 51+ однофамильцев, искомая сделка у самого
    старого контакта. Контакты дочитываются до MAX_CONTACT_PAGES страниц
    (старые попадают в выборку), сделки берутся почанково; число REST-вызовов
    ограничено, а недочитанный хвост честно помечается truncated.
    """
    # 120 контактов «Иванов»: две страницы по 50, третья не читается
    ids_desc = [str(i) for i in range(120, 0, -1)]
    contact_listing = respx_mock.post(method_url("crm.contact.list")).mock(
        side_effect=[
            _page([{"ID": i} for i in ids_desc[:50]], total=120, next_start=50),
            _page([{"ID": i} for i in ids_desc[50:100]], total=120, next_start=100),
            _page([], total=0),  # %LAST_NAME
            _page([], total=0),  # UF_CRM_ORG
        ]
    )
    deal_listing = respx_mock.post(method_url("crm.deal.list")).mock(
        side_effect=[
            _page([], total=0),  # %TITLE
            _page([], total=0),  # %COMMENTS
            _page([], total=0),  # чанк 1: контакты 120..71
            # чанк 2: сделка старого контакта id=21 нашлась
            _page([{"ID": "154", "CONTACT_ID": "21"}], total=1),
        ]
    )

    found = await bitrix.search_deals_by_text(bx, "иванов")

    assert [row["ID"] for row in found.deals] == ["154"]
    assert found.truncated is True  # контакты 20..1 не дочитаны — выборка неполная
    assert contact_listing.call_count == 4  # 2 страницы %NAME + %LAST_NAME + UF_ORG
    assert deal_listing.call_count == 4  # %TITLE + %COMMENTS + 2 чанка контактов
    # вторая страница контактов запрошена по start из ответа сервера
    assert call_json(contact_listing.calls[1])["start"] == 50
    chunk_filters = [call_json(call)["filter"] for call in deal_listing.calls[2:]]
    assert chunk_filters[0] == {"@CONTACT_ID": [int(i) for i in ids_desc[:50]]}
    assert chunk_filters[1] == {"@CONTACT_ID": [int(i) for i in ids_desc[50:100]]}


async def test_recent_deals_single_page_request(bx, respx_mock):
    """/last: одна страница самых новых (order DESC), а не выгрузка всех сделок.

    Раньше get_all тянул ВСЕ страницы сделок ради топ-10. Теперь ровно один
    REST-вызов: первая страница DESC заведомо покрывает limit, хвост total
    не дочитывается. Сортировка всё равно наводится на клиенте.
    """
    rows = [{"ID": str(100 + i)} for i in range(12)]
    listing = respx_mock.post(method_url("crm.deal.list")).mock(
        return_value=_page(list(reversed(rows)), total=999, next_start=50)
    )

    top = await bitrix.recent_deals(bx, limit=10)

    assert [row["ID"] for row in top] == [str(i) for i in range(111, 101, -1)]
    assert listing.call_count == 1  # хвост из сотен сделок не выгружается
    body = request_json(listing)
    assert body["select"] == bitrix.DEAL_SUMMARY_SELECT
    assert "filter" not in body
    assert body["order"] == {"ID": "DESC"} and body["start"] == 0


async def test_stage_names_cover_all_pipelines(bx, respx_mock):
    """Стадии берутся для всех воронок (DEAL_STAGE и DEAL_STAGE_<n>)."""
    respx_mock.post(method_url("crm.status.list")).mock(
        return_value=_ok(
            [
                {"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "NEW", "NAME": "Новая заявка"},
                {"ENTITY_ID": "DEAL_STAGE_5", "STATUS_ID": "C5:NEW", "NAME": "Новая (доп.)"},
                {"ENTITY_ID": "SOURCE", "STATUS_ID": "CALL", "NAME": "Звонок"},
            ]
        )
    )

    names = await bitrix.stage_names(bx)

    assert names == {"NEW": "Новая заявка", "C5:NEW": "Новая (доп.)"}


async def test_stage_names_reads_only_first_page(bx, respx_mock):
    """Справочник стадий не запускает неограниченный get_all."""
    listing = respx_mock.post(method_url("crm.status.list")).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "NEW", "NAME": "Новая"}
                ],
                "next": 50,
                "total": 500,
            },
        )
    )

    assert await bitrix.stage_names(bx) == {"NEW": "Новая"}
    assert listing.call_count == 1


async def test_raw_error_envelope_is_not_empty_search_result(bx, respx_mock):
    respx_mock.post(method_url("crm.deal.list")).mock(
        return_value=_error("ACCESS_DENIED", "Access denied")
    )

    with pytest.raises(ErrorInServerResponseException, match="ACCESS_DENIED"):
        await bitrix.recent_deals(bx)


async def test_normalize_then_full_flow(bx, respx_mock):
    """Сквозной сценарий: сырой номер -> контакт не найден -> контакт + сделка."""
    respx_mock.post(method_url("crm.duplicate.findbycomm")).mock(return_value=_ok([]))
    respx_mock.post(method_url("crm.contact.list")).mock(return_value=_ok([]))
    respx_mock.post(method_url("crm.contact.add")).mock(return_value=_ok(15))
    respx_mock.post(method_url("crm.deal.list")).mock(return_value=_ok([]))
    add = respx_mock.post(method_url("crm.deal.add")).mock(return_value=_ok(154))

    phone = bitrix.normalize_phone("8 (914) 123-45-67")
    assert phone == PHONE

    contact_id = await bitrix.create_or_update_contact(bx, name="Иван", phone=phone)
    deal_id = await bitrix.create_deal_idempotent(
        bx,
        contact_id=contact_id,
        fields={"TITLE": "сантехника: замена крана"},
        tg_msg_key="msg:5:777",
    )

    assert (contact_id, deal_id) == (15, 154)
    fields = request_json(add)["fields"]
    assert fields["CONTACT_ID"] == 15
    assert fields[bitrix.UF_TG_MSG_ID] == "msg:5:777"
