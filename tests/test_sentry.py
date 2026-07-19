import pytest

from app import sentry_setup
from app.sentry_setup import PHONE_MASK, PII_MASK, _scrub_string, scrub_pii


def test_scrub_pii_removes_phone_from_exception_message() -> None:
    """Телефон в exception.message заменяется на маску."""
    event = {"exception": {"values": [{"value": "Не удалось +79991234567 обработать"}]}}
    result = scrub_pii(event, None)
    assert result is not None
    value = result["exception"]["values"][0]["value"]
    assert "+79991234567" not in value
    assert PHONE_MASK in value


def test_scrub_pii_masks_phone_key_in_extra() -> None:
    """Значение ключа phone в extra заменяется на PII_MASK."""
    event = {"extra": {"phone": "+79991234567"}}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["extra"]["phone"] == PII_MASK


def test_scrub_pii_masks_client_name_and_address_and_org() -> None:
    """Ключи client_name, address, org в extra маскируются."""
    event = {"extra": {"client_name": "Иван", "address": "Ленина 5", "org": "ООО Ромашка"}}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["extra"]["client_name"] == PII_MASK
    assert result["extra"]["address"] == PII_MASK
    assert result["extra"]["org"] == PII_MASK


def test_scrub_pii_handles_nested_breadcrumbs() -> None:
    """Вложенные breadcrumbs: message и data.phone маскируются."""
    event = {
        "breadcrumbs": {
            "values": [
                {"message": "звонок +79991234567", "data": {"phone": "+79991234567"}}
            ]
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    crumb = result["breadcrumbs"]["values"][0]
    assert "+79991234567" not in crumb["message"]
    assert PHONE_MASK in crumb["message"]
    assert crumb["data"]["phone"] == PII_MASK


def test_scrub_pii_masks_multiple_phone_formats() -> None:
    """Разные форматы телефонов заменяются на PHONE_MASK."""
    event = {"extra": {"msg": "+79991234567 и 89991234567"}}
    result = scrub_pii(event, None)
    assert result is not None
    msg = result["extra"]["msg"]
    assert "+79991234567" not in msg
    assert "89991234567" not in msg
    assert msg.count(PHONE_MASK) == 2


def test_scrub_pii_handles_contexts() -> None:
    """Телефон в contexts.custom.note маскируется."""
    event = {"contexts": {"custom": {"note": "клиент +79991234567"}}}
    result = scrub_pii(event, None)
    assert result is not None
    note = result["contexts"]["custom"]["note"]
    assert "+79991234567" not in note
    assert PHONE_MASK in note


def test_scrub_pii_handles_logentry() -> None:
    """Телефон в logentry.message и params маскируется."""
    event = {"logentry": {"message": "Заявка от +79991234567", "params": ["+79991234567"]}}
    result = scrub_pii(event, None)
    assert result is not None
    logentry = result["logentry"]
    assert "+79991234567" not in logentry["message"]
    assert PHONE_MASK in logentry["message"]
    assert "+79991234567" not in logentry["params"][0]
    assert PHONE_MASK in logentry["params"][0]


def test_init_sentry_returns_false_when_dsn_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустой или whitespace DSN не вызывает sentry_sdk.init и возвращает False."""
    calls: list[dict] = []

    def fake_init(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(sentry_setup.sentry_sdk, "init", fake_init)

    assert sentry_setup.init_sentry("") is False
    assert sentry_setup.init_sentry("   ") is False
    assert len(calls) == 0


def test_init_sentry_returns_true_and_configures_when_dsn_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Валидный DSN вызывает sentry_sdk.init с правильными параметрами и возвращает True."""
    calls: list[dict] = []

    def fake_init(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(sentry_setup.sentry_sdk, "init", fake_init)

    dsn = "https://xxx@sentry.io/1"
    assert sentry_setup.init_sentry(dsn) is True

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["dsn"] == dsn
    assert kwargs["send_default_pii"] is False
    assert kwargs["include_local_variables"] is False
    assert kwargs["before_send"] is scrub_pii
    assert kwargs["traces_sample_rate"] == 0.1


def test_scrub_pii_masks_phone_in_string_keys() -> None:
    """Телефон в ключе dict маскируется."""
    event = {"extra": {"+79991234567": "status"}}
    result = scrub_pii(event, None)
    assert result is not None
    keys = list(result["extra"].keys())
    assert "+79991234567" not in keys
    assert any(PHONE_MASK in k for k in keys)


def test_scrub_pii_masks_phone_in_nested_string_keys() -> None:
    """Телефон во вложенном ключе contexts маскируется."""
    event = {"contexts": {"custom": {"+79991234567": "note"}}}
    result = scrub_pii(event, None)
    assert result is not None
    nested_keys = list(result["contexts"]["custom"].keys())
    assert "+79991234567" not in nested_keys
    assert any(PHONE_MASK in k for k in nested_keys)


def test_init_sentry_returns_false_on_bad_dsn() -> None:
    """init_sentry возвращает False при некорректном DSN без исключения."""
    assert sentry_setup.init_sentry("PENDING") is False
    assert sentry_setup.init_sentry("not-a-url") is False


def test_init_sentry_configures_before_send_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_sentry передаёт scrub_pii в before_send_transaction."""
    captured: dict = {}

    def fake_init(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(sentry_setup.sentry_sdk, "init", fake_init)
    assert sentry_setup.init_sentry("https://xxx@sentry.io/1") is True
    assert captured.get("before_send_transaction") is scrub_pii


def test_scrub_two_bare_11digit_numbers_joined_by_space() -> None:
    """Два 11-значных номера через пробел маскируются, цифры не утекают."""
    result = _scrub_string("79991234567 78881234567")
    assert PHONE_MASK in result
    assert "79991234567" not in result
    assert "78881234567" not in result
    assert sum(c.isdigit() for c in result) == 0


def test_scrub_formatted_numbers_without_plus() -> None:
    """Отформатированные номера без плюса маскируются, коды не видны."""
    result = _scrub_string("7 (999) 123-45-67 7 (888) 123-45-67")
    assert PHONE_MASK in result
    assert "999" not in result
    assert "888" not in result


def test_scrub_numbers_joined_by_dash() -> None:
    """Номера, соединенные дефисом, маскируются, цифры не утекают."""
    result = _scrub_string("79991234567-78881234567")
    assert PHONE_MASK in result
    assert "1234567" not in result
    assert sum(c.isdigit() for c in result) == 0


def test_scrub_continuous_digit_run_longer_than_15_masked_whole() -> None:
    """Непрерывная цепочка цифр длиннее 15 маскируется целиком."""
    result1 = _scrub_string("7999123456778881234567")
    assert result1 == PHONE_MASK
    assert not any(ch.isdigit() for ch in result1)

    result2 = _scrub_string("1234567890123456")
    assert result2 == PHONE_MASK


def test_scrub_10_digit_number_without_plus() -> None:
    """10-значный номер без плюса маскируется."""
    result = _scrub_string("9991234567")
    assert result == PHONE_MASK


def test_scrub_phone_with_commas() -> None:
    """Номер с запятыми маскируется."""
    result = _scrub_string("+7,999,123,45,67")
    assert PHONE_MASK in result
    assert "999" not in result
    assert "1234567" not in result


def test_scrub_phone_with_brackets() -> None:
    """Номер с квадратными скобками маскируется."""
    result = _scrub_string("+7 [999] 123-45-67")
    assert PHONE_MASK in result
    assert "999" not in result


def test_scrub_phone_with_zero_width_spaces() -> None:
    """Номер с zero-width space разделителями маскируется."""
    zws = "​"
    phone = f"+7{zws}999{zws}123{zws}45{zws}67"
    result = _scrub_string(phone)
    assert PHONE_MASK in result
    assert "999" not in result
    assert "1234567" not in result


def test_scrub_iso_timestamp_not_masked() -> None:
    """ISO timestamp не должен маскироваться как телефон."""
    ts = "2026-07-13T00:34:17.031104Z"
    result = _scrub_string(ts)
    assert PHONE_MASK not in result
    assert result == ts


def test_scrub_pii_preserves_sentry_timestamp() -> None:
    """Sentry event.timestamp сохраняется как валидный ISO datetime."""
    event = {"timestamp": "2026-07-13T00:34:17.031104Z", "message": "test"}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["timestamp"] == "2026-07-13T00:34:17.031104Z"


def test_scrub_phone_with_escaped_brackets() -> None:
    """Номер с backslash-escaped брекетами маскируется."""
    result = _scrub_string(r"+7\[999\] 123-45-67")
    assert PHONE_MASK in result
    assert "999" not in result


def test_scrub_phone_with_soft_hyphen() -> None:
    """Номер через soft hyphen U+00AD маскируется."""
    sh = "­"
    phone = f"+7{sh}999{sh}123{sh}45{sh}67"
    result = _scrub_string(phone)
    assert PHONE_MASK in result
    assert "999" not in result


def test_scrub_phone_with_invisible_operators() -> None:
    """Номер через U+2061..U+2064 маскируется."""
    for cp in (0x2061, 0x2062, 0x2063, 0x2064):
        sep = chr(cp)
        phone = f"+7{sep}999{sep}123{sep}45{sep}67"
        result = _scrub_string(phone)
        assert PHONE_MASK in result, f"leak at U+{cp:04X}: {result!r}"
        assert "999" not in result, f"leak at U+{cp:04X}: {result!r}"


def test_scrub_russian_pii_keys() -> None:
    """Русские PII-ключи (телефон/фио/адрес) маскируются."""
    event = {
        "extra": {
            "телефон": "+79991234567",
            "фио": "Иван Петров",
            "адрес": "Ленина 5",
            "организация": "ООО Ромашка",
            "клиент": "Мария",
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    for k in ("телефон", "фио", "адрес", "организация", "клиент"):
        assert result["extra"][k] == PII_MASK, f"{k}: {result['extra'][k]!r}"


def test_scrub_russian_phone_in_message() -> None:
    """Телефон в русском тексте маскируется."""
    event = {
        "exception": {
            "values": [
                {"value": "Клиент Иван написал с номера +7 (999) 123-45-67 в 15:00"}
            ]
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    value = result["exception"]["values"][0]["value"]
    assert "999" not in value
    assert "1234567" not in value
    assert PHONE_MASK in value


def test_scrub_pii_preserves_system_hex_ids() -> None:
    """event_id/trace_id/span_id не должны скрабиться как телефон."""
    event = {
        "event_id": "1234567890abcdef1234567890abcdef",
        "contexts": {
            "trace": {
                "trace_id": "1234567890abcdef1234567890abcdef",
                "span_id": "1234567890abcdef",
                "parent_span_id": "1234567890abcdef",
            }
        },
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["event_id"] == "1234567890abcdef1234567890abcdef"
    trace = result["contexts"]["trace"]
    assert trace["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert trace["span_id"] == "1234567890abcdef"
    assert trace["parent_span_id"] == "1234567890abcdef"


def test_scrub_pii_preserves_release_git_sha() -> None:
    """release с 40-hex SHA (10+ подряд digits) не должен становиться [PHONE]."""
    sha = "32da6e6847626271e05a5e3bc1f5b247061cac06"
    event = {"release": sha, "environment": "production"}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["release"] == sha
    assert result["environment"] == "production"


def test_scrub_pii_scrubs_phone_in_nested_key_named_event_id() -> None:
    """Bypass через маскировочный key: extra.nested.event_id — должен скрабиться."""
    event = {
        "extra": {
            "nested": {
                "event_id": "+79991234567",
                "trace_id": "+79991234567",
            }
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    nested = result["extra"]["nested"]
    assert nested["event_id"] == PHONE_MASK
    assert nested["trace_id"] == PHONE_MASK


def test_scrub_pii_scrubs_phone_in_breadcrumbs_data() -> None:
    """PII bypass через breadcrumbs.values[].data.trace_id — должен скрабиться."""
    event = {
        "breadcrumbs": {
            "values": [
                {"data": {"trace_id": "+79991234567", "event_id": "+79991234567"}}
            ]
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    data = result["breadcrumbs"]["values"][0]["data"]
    assert data["trace_id"] == PHONE_MASK
    assert data["event_id"] == PHONE_MASK


def test_scrub_string_preserves_multipart_version() -> None:
    """Версии браузера/ОС сохраняются по ПУТИ (contexts.browser/os.version).
    Защита по форме строки убрана: телефон 7.999.123.45.67 по форме неотличим
    от версии и раньше утекал (потенциальная утечка). Сохранность теперь только
    через whitelist путей."""
    event = {
        "contexts": {
            "browser": {"version": "126.0.6478.127"},
            "os": {"version": "10.0.26100.4652"},
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["contexts"]["browser"]["version"] == "126.0.6478.127"
    assert result["contexts"]["os"]["version"] == "10.0.26100.4652"


def test_scrub_string_preserves_hex_address() -> None:
    """Hex-адреса сохраняются по ПУТИ (debug_meta.images.image_vmaddr/image_addr).
    Голый 0x1234567890 из одних цифр по форме неотличим от телефона, защищается
    только путём."""
    event = {
        "debug_meta": {
            "images": [{"image_vmaddr": "0x1234567890", "image_addr": "0xABCDEF01"}]
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    img = result["debug_meta"]["images"][0]
    assert img["image_vmaddr"] == "0x1234567890"
    assert img["image_addr"] == "0xABCDEF01"


def test_scrub_string_preserves_hex_id_with_letters() -> None:
    """SHA/32-hex/16-hex сохраняются по ПУТИ (release, trace_id, span_id).
    Строки с 10+ ведущими цифрами (1234567890abcdef) по форме неотличимы от
    телефона, поэтому защищаются путём, а не формой."""
    sha = "32da6e6847626271e05a5e3bc1f5b247061cac06"
    event = {
        "release": sha,
        "contexts": {
            "trace": {
                "trace_id": "1234567890abcdef1234567890abcdef",
                "span_id": "1234567890abcdef",
            }
        },
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["release"] == sha
    trace = result["contexts"]["trace"]
    assert trace["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert trace["span_id"] == "1234567890abcdef"


def test_scrub_string_preserves_uuid() -> None:
    """UUID сохраняется по ПУТИ (event_id). UUID из одних цифр по форме неотличим
    от склейки телефонов, поэтому защищается только путём."""
    uuid = "12345678-1234-1234-1234-123456789012"
    event = {"event_id": uuid}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["event_id"] == uuid


def test_scrub_string_scrubs_ambiguous_digit_shapes_without_safe_path() -> None:
    """Обратная сторона той же проблемы: строки, по форме неотличимые от телефона
    (версия/0x/uuid из одних цифр), БЕЗ безопасного пути обязаны скрабиться.
    Если кто-то вернёт защиту по форме, эти проверки упадут."""
    assert _scrub_string("126.0.6478.127") != "126.0.6478.127"
    assert _scrub_string("0x1234567890") != "0x1234567890"
    assert (
        _scrub_string("12345678-1234-1234-1234-123456789012")
        != "12345678-1234-1234-1234-123456789012"
    )


def test_scrub_pii_scrubs_phone_in_transaction() -> None:
    """Freeform transaction с телефоном должен скрабиться."""
    event = {"transaction": "/client/+7 (999) 123-45-67"}
    result = scrub_pii(event, None)
    assert result is not None
    tx = result["transaction"]
    assert "999" not in tx
    assert PHONE_MASK in tx


def test_scrub_pii_scrubs_phone_in_code_file() -> None:
    """Freeform code_file с телефоном должен скрабиться."""
    event = {"debug_meta": {"images": [{"code_file": "/srv/+79991234567/lib"}]}}
    result = scrub_pii(event, None)
    assert result is not None
    cf = result["debug_meta"]["images"][0]["code_file"]
    assert "999" not in cf
    assert PHONE_MASK in cf


def test_scrub_pii_preserves_image_vmaddr() -> None:
    """image_vmaddr 0x... с 10+ digits не должен становиться 0x[PHONE]."""
    event = {"debug_meta": {"images": [{"image_vmaddr": "0x1234567890"}]}}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["debug_meta"]["images"][0]["image_vmaddr"] == "0x1234567890"


def test_scrub_pii_preserves_browser_and_os_version() -> None:
    """Многочастные версии в contexts.browser/os не должны становиться [PHONE]."""
    event = {
        "contexts": {
            "browser": {"name": "Chrome", "version": "126.0.6478.127"},
            "os": {"name": "Windows", "version": "10.0.26100.4652"},
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["contexts"]["browser"]["version"] == "126.0.6478.127"
    assert result["contexts"]["os"]["version"] == "10.0.26100.4652"


def test_scrub_pii_preserves_sdk_packages_version_sha() -> None:
    """SHA в sdk.packages[].version не должен становиться [PHONE]."""
    sha = "32da6e6847626271e05a5e3bc1f5b247061cac06"
    event = {"sdk": {"name": "sentry.python", "packages": [{"name": "app", "version": sha}]}}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["sdk"]["packages"][0]["version"] == sha


def test_scrub_pii_scrubs_phone_list_on_whitelist_path() -> None:
    """Список на whitelist-пути НЕ наследует исключение: телефон маскируется.

    Исключение по RAW_PATHS положено только скалярной строке прямо под ключом.
    Элемент списка скрабится всегда, иначе trace_id=["+7..."] уходил бы целиком."""
    event = {
        "contexts": {"trace": {"trace_id": ["+79991234567"]}},
        "release": ["+79991234567"],
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["contexts"]["trace"]["trace_id"] == [PHONE_MASK]
    assert result["release"] == [PHONE_MASK]
    assert not any(c.isdigit() for c in repr(result))


def test_scrub_pii_scrubs_phone_dict_on_whitelist_path() -> None:
    """Dict на whitelist-пути: вложенный путь уже не whitelist, телефон маскируется."""
    event = {"contexts": {"trace": {"trace_id": {"nested": "+79991234567"}}}}
    result = scrub_pii(event, None)
    assert result is not None
    assert result["contexts"]["trace"]["trace_id"]["nested"] == PHONE_MASK


def test_scrub_pii_scrubs_phone_on_paths_resembling_whitelist() -> None:
    """Пути, похожие на whitelist, но не совпадающие точно, скрабятся."""
    event = {
        "extra": {"nested": {"event_id": "+79991234567"}},
        "breadcrumbs": {"values": [{"data": {"trace_id": "+79991234567"}}]},
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["extra"]["nested"]["event_id"] == PHONE_MASK
    assert result["breadcrumbs"]["values"][0]["data"]["trace_id"] == PHONE_MASK


def test_scrub_pii_preserves_legit_telemetry_paths() -> None:
    """Легитимная телеметрия на своих путях проходит без изменений."""
    event = {
        "release": "1a2b3c4d5e6f7a8b",
        "contexts": {
            "trace": {"trace_id": "0af7651916cd43dd8448eb211c80319c"},
            "os": {"version": "10.0.19045"},
            "runtime": {"version": "3.12.4"},
        },
        "sdk": {
            "version": "2.64.0",
            "packages": [{"name": "sentry.python", "version": "2.64.0"}],
        },
    }
    result = scrub_pii(event, None)
    assert result is not None
    assert result["release"] == "1a2b3c4d5e6f7a8b"
    assert result["contexts"]["trace"]["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert result["contexts"]["os"]["version"] == "10.0.19045"
    assert result["contexts"]["runtime"]["version"] == "3.12.4"
    assert result["sdk"]["version"] == "2.64.0"
    assert result["sdk"]["packages"] == [{"name": "sentry.python", "version": "2.64.0"}]


def test_scrub_pii_masks_disguised_phone_forms() -> None:
    """Формы-обходы (точки, hex-суффикс, 0x, uuid-подобные) на обычном пути скрабятся."""
    forms = [
        "7.999.123.45.67",
        "79991234567abcdef",
        "0x79991234567",
        "79991234-567a-1234-1234-123456789012",
    ]
    for form in forms:
        result = scrub_pii({"extra": {"msg": form}}, None)
        assert result is not None
        msg = result["extra"]["msg"]
        assert PHONE_MASK in msg, f"{form!r} -> {msg!r}"
        assert "999" not in msg, f"{form!r} -> {msg!r}"
        assert "1234567" not in msg, f"{form!r} -> {msg!r}"


def test_scrub_phone_with_directional_isolates() -> None:
    """Номер, разбитый U+2066/U+2069 и zero-width space, маскируется."""
    phone = "+7⁦999⁩123​4567"
    assert _scrub_string(phone) == PHONE_MASK


def test_scrub_pii_key_with_zero_width_chars() -> None:
    """PII-ключ с невидимыми символами распознаётся, значение маскируется."""
    event = {
        "extra": {
            "phone​": "+79991234567",
            "фио⁡": "Иван Петров",
            "адрес­": "Ленина 5",
        }
    }
    result = scrub_pii(event, None)
    assert result is not None
    extra = result["extra"]
    assert set(extra.keys()) == {"phone", "фио", "адрес"}
    assert all(v == PII_MASK for v in extra.values())


def test_scrub_joined_numbers_no_digit_survives() -> None:
    """Склеенные номера: ни одна цифра не выживает.

    Число масок не проверяем: схлопывание склейки в один [PHONE] — принятое
    решение по проекту, инвариант — отсутствие выживших цифр."""
    for joined in ("79991234567 78881234567", "+79991234567+78881234567"):
        result = _scrub_string(joined)
        assert PHONE_MASK in result
        assert not any(c.isdigit() for c in result), f"{joined!r} -> {result!r}"


def test_scrub_very_long_digit_chain_no_digit_survives() -> None:
    """Цепочка из 40 цифр маскируется целиком, без выживших хвостов."""
    chain = "1234567890" * 4
    assert _scrub_string(chain) == PHONE_MASK
    spaced = "12345 67890 12345 67890"
    result = _scrub_string(spaced)
    assert not any(c.isdigit() for c in result), f"{spaced!r} -> {result!r}"
