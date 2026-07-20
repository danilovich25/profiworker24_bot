"""Поиск по заявкам (/find) и последние заявки (/last).

Апдейты идут через настоящий диспетчер (create_dispatcher). Сеть не нужна:
Telegram подменён RecordingSession, Bitrix24 — фейком в памяти с семантикой
настоящего клиента fast-bitrix24 (SemanticBitrixFake из conftest) и с
квирками реального портала: %PHONE и список в точном фильтре игнорируются,
для «ID из массива» обязателен оператор @.
"""

import re
from types import SimpleNamespace

import pytest
from aiogram.methods import SendMessage
from aiogram.types import ForceReply

import app.handlers.search as search_handlers
from app.db import Database
from app.handlers import routers
from app.handlers.messages import OrderFlow
from app.handlers.search import (
    ACTIVE_ORDER_WARNING,
    ASK_QUERY,
    LAST_EMPTY,
    NO_CRM,
    NOTHING_FOUND,
    QUERY_TOO_SHORT,
    SEARCH_AGAIN_HINT,
    SEARCH_FAILED,
    SEARCH_TOO_BROAD,
    SearchFlow,
    clean_search_query,
)
from app.handlers.start import BTN_NEW
from app.main import create_dispatcher
from app.schemas import Category, ParsedOrder
from app.services import llm
from tests.conftest import SemanticBitrixFake, make_message_update


@pytest.fixture(autouse=True)
def _detach_routers():
    """Роутеры - модульные синглтоны; после теста отвязываем их от диспетчера."""
    yield
    for r in routers:
        r._parent_router = None


class FakeSearchBitrix(SemanticBitrixFake):
    """Bitrix24 в памяти: контакты, сделки и стадии для поисковых запросов.

    Семантика клиента (call разворачивает списки, get_all запрещает order)
    наследуется от SemanticBitrixFake; здесь — «серверная» часть портала
    с его настоящими квирками фильтров (_match).
    """

    def __init__(self) -> None:
        self.contacts: list[dict] = [
            {
                "ID": "15",
                "NAME": "Иван",
                "LAST_NAME": "Петров",
                "UF_CRM_ORG": "Ромашка",
                "PHONE": "+79141234567",
            }
        ]
        self.deals: list[dict] = [
            {
                "ID": "154",
                "TITLE": "сантехника: замена крана",
                "STAGE_ID": "NEW",
                "DATE_CREATE": "2026-07-18T10:00:00+10:00",
                "CONTACT_ID": "15",
                "COMMENTS": "Организация: Ромашка\nАдрес: Владивосток",
            },
            {
                "ID": "155",
                "TITLE": "электрика: замена розетки",
                "STAGE_ID": "WON",
                "DATE_CREATE": "2026-07-17T12:00:00+10:00",
                "CONTACT_ID": "15",
                "COMMENTS": "",
            },
        ]
        self.stages: list[dict] = [
            {"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "NEW", "NAME": "Новая заявка"},
            {"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "WON", "NAME": "Выполнена"},
            {"ENTITY_ID": "DEAL_STAGE_5", "STATUS_ID": "C5:NEW", "NAME": "Новая (доп.)"},
        ]
        self.fail_all = False
        # Точечные сбои: методы из этого набора падают, остальные работают.
        self.fail_methods: set[str] = set()

    async def _dispatch(self, method: str, params: dict):
        if self.fail_all or method in self.fail_methods:
            raise RuntimeError("Bitrix24 недоступен")
        flt = params.get("filter") or {}
        if method == "crm.duplicate.findbycomm":
            wanted = {
                re.sub(r"\D", "", str(v))[-10:] for v in params.get("values") or [] if v
            }
            hits = [
                int(c["ID"])
                for c in self.contacts
                if re.sub(r"\D", "", str(c.get("PHONE") or ""))[-10:] in wanted
            ]
            return {"CONTACT": hits} if hits else []
        if method == "crm.deal.get":
            wanted_id = int(params.get("id", 0))
            for deal in self.deals:
                if int(deal["ID"]) == wanted_id:
                    return deal
            raise RuntimeError("ERROR_NOT_FOUND: Не найдено")
        if method == "crm.status.list":
            return list(self.stages)
        if method == "crm.contact.list":
            return [c for c in self.contacts if self._match(c, flt)]
        if method == "crm.deal.list":
            return [d for d in self.deals if self._match(d, flt)]
        raise AssertionError(f"неожиданный метод: {method}")

    @staticmethod
    def _match(row: dict, flt: dict) -> bool:
        """Отбор строки по фильтру С КВИРКАМИ реального портала.

        Живыми запросами подтверждено: LIKE по множественному PHONE портал
        игнорирует (возвращая посторонние контакты), список значений в
        ТОЧНОМ фильтре тоже игнорирует (нужен оператор @). Фейк повторяет
        это поведение, чтобы код, полагающийся на такие фильтры, падал в
        тестах так же, как в проде.
        """
        for key, value in flt.items():
            if key.startswith("%"):
                if key[1:] == "PHONE":
                    continue  # квирк: LIKE по множественному PHONE игнорируется
                if str(value).lower() not in str(row.get(key[1:]) or "").lower():
                    return False
            elif key.startswith("@"):  # вхождение в список значений
                wanted = [str(v) for v in value]
                if str(row.get(key[1:]) or "") not in wanted:
                    return False
            elif isinstance(value, list):
                continue  # квирк: список в точном фильтре игнорируется
            elif str(row.get(key) or "") != str(value):
                return False
        return True


@pytest.fixture
async def flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "search.db"))
    await db.init()
    bx = FakeSearchBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


async def send(flow, text: str, user_id: int = 1, **extra) -> None:
    await flow.dp.feed_update(
        flow.bot, make_message_update(flow.bot, text, user_id=user_id, **extra)
    )


# ---------------------------------------------------------------------------
# /find: телефон, номер заявки, текст
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Андрея можешь найти", "Андрея"),
        ("Пожалуйста, покажи мне клиента Петрова!", "Петрова"),
        (
            "поиск: ООО «Рога & Копыта», пожалуйста",
            "ООО «Рога & Копыта»",
        ),
        ("Нет, найди Андрея", "Андрея"),
        (
            "найди найти поиск искать ищу можешь пожалуйста покажи дай мне "
            "клиент клиента заявку заявка заказ по номер телефон Андрей",
            "Андрей",
        ),
        ("найди, пожалуйста!!!", ""),
    ],
)
def test_clean_search_query_removes_only_edge_service_words(raw, expected):
    assert clean_search_query(raw) == expected


async def test_find_asks_query_then_searches_by_phone(flow):
    await send(flow, "/find")
    assert flow.session.sent_texts[-1] == ASK_QUERY

    await send(flow, "8 (914) 123-45-67")  # телефон в свободном формате
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply  # обе сделки контакта
    assert "Иван Петров" in reply
    assert "сантехника: замена крана" in reply
    assert "Новая заявка" in reply and "Выполнена" in reply  # стадии по именам
    assert "18.07.2026" in reply
    assert reply.index("№155") < reply.index("№154")  # новые сверху
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


async def test_find_prompt_opens_input_field(flow):
    """«Найти» сразу открывает поле ввода (ForceReply), без лишнего нажатия."""
    await send(flow, "Найти")

    prompt = flow.session.sent_messages[-1]
    assert prompt.text == ASK_QUERY
    markup = prompt.reply_markup
    assert isinstance(markup, ForceReply)
    assert markup.force_reply is True
    assert markup.input_field_placeholder == search_handlers.SEARCH_INPUT_PLACEHOLDER

    # /find без аргументов ведёт себя так же
    await send(flow, "/find")
    assert isinstance(flow.session.sent_messages[-1].reply_markup, ForceReply)


async def test_find_with_inline_deal_number(flow):
    await send(flow, "/find 154")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" not in reply
    assert "Иван Петров" in reply
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


async def test_find_unknown_deal_number(flow):
    await send(flow, "/find 999")
    assert flow.session.sent_texts[-1].startswith(NOTHING_FOUND)
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_find_natural_phrase_with_deal_number(flow):
    await send(flow, "/find")
    await send(flow, "Найди, пожалуйста, по номеру 154")

    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" not in reply


@pytest.mark.parametrize(
    "phrase",
    ["статус заявки 42", "что с заявкой 42", "покажи заявку 42"],
)
async def test_plain_text_order_number_uses_exact_search(
    flow, monkeypatch, phrase
):
    deal = dict(flow.bx.deals[0], ID="42", TITLE="электрика: диагностика")
    flow.bx.deals.append(deal)

    async def must_not_parse_order(text: str):
        raise AssertionError(f"текст статуса ушёл в LLM: {text}")

    monkeypatch.setattr(llm, "parse_order", must_not_parse_order)
    before = list(flow.bx.deals)

    await send(flow, phrase)

    reply = flow.session.sent_texts[-1]
    assert "№42" in reply and "№154" not in reply and "№155" not in reply
    assert "электрика: диагностика" in reply
    assert flow.bx.deals == before
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() is None


async def test_plain_text_order_number_is_one_shot_before_new_order(flow, monkeypatch):
    deal = dict(flow.bx.deals[0], ID="40", TITLE="электрика: диагностика")
    flow.bx.deals.append(deal)
    parsed = ParsedOrder(
        client_name="Иван",
        phone="+79141234567",
        category=Category.plumbing,
        problem="замена крана",
        income_rub=5000,
    )
    parsed_texts = []

    async def parse_order(text: str):
        parsed_texts.append(text)
        return parsed

    monkeypatch.setattr(llm, "parse_order", parse_order)

    await send(flow, "статус заявки 40")

    assert "№40" in flow.session.sent_texts[-1]
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() is None

    await send(flow, "Иван, сантехника, замена крана")

    assert parsed_texts == ["Иван, сантехника, замена крана"]
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text


async def test_plain_text_unknown_order_number_returns_nothing_found(flow):
    await send(flow, "статус заявки 42")

    assert flow.session.sent_texts[-1].startswith(NOTHING_FOUND)


async def test_find_by_org_name(flow):
    """Организация ищется и по UF-полю контакта, и по комментарию сделки.

    Сделка №154 несёт «Организация: Ромашка» в комментарии, №155 — нет,
    но принадлежит контакту с UF_CRM_ORG «Ромашка»: находиться должны обе
    (раньше поиск шёл только по %COMMENTS и №155 терялась).
    """
    await send(flow, "/find Ромашка")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply


async def test_find_by_org_candidates_preserve_name(flow):
    """Естественная фраза очищается, а значимая пунктуация не теряется."""
    for deal in flow.bx.deals:
        deal["COMMENTS"] = ""  # в комментариях сделок организации больше нет

    await send(flow, "/find найди организацию Ромашка")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply

    flow.bx.contacts[0]["UF_CRM_ORG"] = "ООО Рога & Копыта"
    await send(flow, "/find ООО Рога & Копыта")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply


async def test_find_by_contact_last_name(flow):
    """Поиск по фамилии контакта находит все его сделки через @CONTACT_ID."""
    await send(flow, "/find Петров")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply


async def test_find_cleans_natural_phrase(flow):
    await send(flow, "/find")
    await send(flow, "Пожалуйста, можешь найти клиента Петров")

    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply
    assert SEARCH_AGAIN_HINT in reply


async def test_find_uses_one_stem_fallback_for_inflected_name(flow, monkeypatch):
    flow.bx.contacts.append(
        {"ID": "18", "NAME": "Андрей", "LAST_NAME": "", "PHONE": ""}
    )
    flow.bx.deals.append(
        {
            "ID": "156",
            "TITLE": "прочее: консультация",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T11:00:00+10:00",
            "CONTACT_ID": "18",
            "COMMENTS": "",
        }
    )

    calls: list[str] = []
    original_search = search_handlers.search_deals_by_text

    async def counting_search(bitrix, query):
        calls.append(query)
        return await original_search(bitrix, query)

    monkeypatch.setattr(search_handlers, "search_deals_by_text", counting_search)

    await send(flow, "/find")
    await send(flow, "Андрея можешь найти")

    reply = flow.session.sent_texts[-1]
    assert "№156" in reply and "Андрей" in reply
    assert SEARCH_AGAIN_HINT in reply
    assert calls == ["Андрея можешь найти", "Андрея", "Андре"]


async def test_empty_cleaned_query_asks_again_and_keeps_search(flow):
    await send(flow, "/find")
    await send(flow, "Найди, пожалуйста!")

    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert flow.session.sent_texts[-1] == ASK_QUERY
    assert await context.get_state() == SearchFlow.query.state


async def test_find_by_deal_title(flow):
    await send(flow, "/find розетк")
    reply = flow.session.sent_texts[-1]
    assert "№155" in reply and "№154" not in reply


async def test_find_nothing(flow):
    await send(flow, "/find Пётр")
    assert flow.session.sent_texts[-1].startswith(NOTHING_FOUND)
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_find_phone_without_contact(flow):
    """Несуществующий номер: контакт не найден, ЧУЖИЕ сделки не показываются.

    Раньше запасной %PHONE-фильтр «находил» произвольный контакт (портал
    игнорирует LIKE по множественному PHONE) и /find выдавал ложное
    совпадение — фейк теперь повторяет квирк, а код не использует %PHONE.
    """
    await send(flow, "/find 89147654321")
    assert flow.session.sent_texts[-1].startswith(NOTHING_FOUND)
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_find_phone_collects_all_duplicate_contacts(flow):
    """Телефон записан у двух контактов-дублей: показываются сделки обоих."""
    flow.bx.contacts.append(
        {"ID": "18", "NAME": "Валерия", "LAST_NAME": "", "PHONE": "89141234567"}
    )
    flow.bx.deals.append(
        {
            "ID": "156",
            "TITLE": "прочее: консультация",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T11:00:00+10:00",
            "CONTACT_ID": "18",
            "COMMENTS": "",
        }
    )
    await send(flow, "/find 89141234567")
    reply = flow.session.sent_texts[-1]
    assert "№154" in reply and "№155" in reply and "№156" in reply


async def test_find_phone_with_more_than_one_page_is_too_broad(flow):
    """Телефонный поиск не выдаёт первые 50 сделок за полный результат."""
    flow.bx.deals = [
        {
            "ID": str(index),
            "TITLE": "сантехника: заявка",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T10:00:00+10:00",
            "CONTACT_ID": "15",
            "COMMENTS": "",
        }
        for index in range(1, 52)
    ]

    await send(flow, "/find 89141234567")

    assert flow.session.sent_texts[-1] == SEARCH_TOO_BROAD


async def test_find_last_name_match_beyond_first_page(flow):
    """51 однофамилец: сделка самого старого контакта находится.

    Сценарий: срез «50 новых контактов» отбрасывал старые ID, и
    /find по фамилии врал «ничего не нашёл», хотя сделка есть. Теперь
    контакты дочитываются страницами (в пределах лимита), и сделка контакта
    со второй страницы находится.
    """
    flow.bx.contacts = [
        {"ID": str(i), "NAME": "Пётр", "LAST_NAME": "Иванов", "PHONE": ""}
        for i in range(1, 52)
    ]
    flow.bx.deals = [
        {
            "ID": "154",
            "TITLE": "сантехника: замена крана",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T10:00:00+10:00",
            "CONTACT_ID": "1",  # самый старый контакт — вторая страница выборки
            "COMMENTS": "",
        }
    ]

    await send(flow, "/find Иванов")

    reply = flow.session.sent_texts[-1]
    assert "№154" in reply
    assert "Пётр Иванов" in reply


async def test_find_too_broad_query_is_not_nothing_found(flow):
    """Оборванная лимитом выборка без сделок — «уточните», а не «не нашёл».

    Контактов-совпадений больше, чем читается страниц: молчаливое «ничего
    не нашёл» было бы враньём — сделка может быть у недочитанного контакта.
    """
    flow.bx.contacts = [
        {"ID": str(i), "NAME": "Пётр", "LAST_NAME": "Иванов", "PHONE": ""}
        for i in range(1, 121)  # 120 однофамильцев: больше лимита страниц
    ]
    flow.bx.deals = []

    await send(flow, "/find Иванов")

    assert flow.session.sent_texts[-1] == SEARCH_TOO_BROAD


async def test_find_truncated_nonempty_result_is_still_too_broad(flow):
    """Непустой, но усечённый список не выдаётся за полный результат."""
    flow.bx.contacts = [
        {"ID": str(i), "NAME": "Пётр", "LAST_NAME": "Иванов", "PHONE": ""}
        for i in range(1, 121)
    ]
    flow.bx.deals = [
        {
            "ID": "154",
            "TITLE": "старая найденная заявка",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T10:00:00+10:00",
            "CONTACT_ID": "120",
            "COMMENTS": "",
        }
    ]

    await send(flow, "/find Иванов")

    assert flow.session.sent_texts[-1] == SEARCH_TOO_BROAD
    assert "№154" not in flow.session.sent_texts[-1]


@pytest.mark.parametrize(
    "active_state",
    [
        OrderFlow.ask_phone,
        OrderFlow.ask_category,
        OrderFlow.form_name,
        OrderFlow.form_phone,
        OrderFlow.form_category,
        OrderFlow.form_problem,
        OrderFlow.form_deadline,
    ],
)
@pytest.mark.parametrize("text", ["/find", "Найти", "Последние"])
async def test_search_controls_preserve_active_order(flow, active_state, text):
    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    sentinel = {"order": {"client_name": "Иван"}, "dedup_key": "msg:1:1"}
    await context.set_state(active_state)
    await context.set_data(sentinel)

    await send(flow, text)

    assert flow.session.sent_texts[-1] == ACTIVE_ORDER_WARNING
    assert await context.get_state() == active_state.state
    assert await context.get_data() == sentinel


async def test_find_question_failure_does_not_change_state(flow, monkeypatch):
    orig_request = flow.session.make_request

    async def fail_question(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.text == ASK_QUERY:
            raise RuntimeError("Telegram недоступен")
        return await orig_request(bot, method, timeout)

    monkeypatch.setattr(flow.session, "make_request", fail_question)
    with pytest.raises(RuntimeError):
        await send(flow, "/find")

    context = flow.dp.fsm.get_context(bot=flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() is None


async def test_find_deal_number_method_not_found_is_failure(flow):
    """«Method not found» на crm.deal.get — сбой поиска, а не «сделки нет»."""
    orig_dispatch = flow.bx._dispatch

    async def method_not_found(method: str, params: dict):
        if method == "crm.deal.get":
            raise RuntimeError("ERROR_METHOD_NOT_FOUND: Method not found!")
        return await orig_dispatch(method, params)

    flow.bx._dispatch = method_not_found

    await send(flow, "/find 154")
    assert flow.session.sent_texts[-1] == SEARCH_FAILED


async def test_find_error_answers_softly(flow):
    flow.bx.fail_all = True
    await send(flow, "/find Ромашка")
    assert flow.session.sent_texts[-1] == SEARCH_FAILED


async def test_find_deal_number_crm_error_is_not_nothing_found(flow):
    """Сбой CRM на /find 154 — «поиск не работает», а не «ничего не нашёл»."""
    flow.bx.fail_all = True
    await send(flow, "/find 154")
    assert flow.session.sent_texts[-1] == SEARCH_FAILED


async def test_enrichment_failure_answers_softly(flow):
    """Сделки найдены, но упало обогащение именами контактов: мягкий ответ.

    Раньше contact_names/стадии выполнялись вне try поиска: ошибка уходила
    в глобальный error-хендлер, и пользователь не получал ничего.
    """
    flow.bx.fail_methods.add("crm.contact.list")
    await send(flow, "/find 154")
    assert flow.session.sent_texts[-1] == SEARCH_FAILED


async def test_find_single_char_query_asks_to_refine(flow):
    """Однобуквенный запрос не выгружает весь портал, а просит уточнить."""
    await send(flow, "/find а")
    assert flow.session.sent_texts[-1] == QUERY_TOO_SHORT


async def test_find_with_args_keeps_search_state(flow, monkeypatch):
    """После результата следующий текст остаётся поисковым запросом."""

    async def not_an_order(text: str):
        return None

    monkeypatch.setattr(llm, "parse_order", not_an_order)

    await send(flow, "/find")
    assert flow.session.sent_texts[-1] == ASK_QUERY
    await send(flow, "/find 154")  # запрос пришёл аргументом, а не сообщением
    assert "№154" in flow.session.sent_texts[-1]

    await send(flow, "Петров")
    assert "№154" in flow.session.sent_texts[-1]
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_last_keeps_search_state(flow, monkeypatch):
    async def not_an_order(text: str):
        return None

    monkeypatch.setattr(llm, "parse_order", not_an_order)

    await send(flow, "/find")
    await send(flow, "/last")
    assert "№155" in flow.session.sent_texts[-1]

    await send(flow, "Петров")
    assert "№154" in flow.session.sent_texts[-1]
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_new_order_button_exits_sticky_search(flow, monkeypatch):
    calls = {"count": 0}

    async def not_an_order(text: str):
        calls["count"] += 1
        return None

    monkeypatch.setattr(llm, "parse_order", not_an_order)

    await send(flow, "/find")
    await send(flow, "Пётр")
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]

    await send(flow, BTN_NEW)
    await send(flow, "Иван, сантехника, замена крана")

    assert calls["count"] == 1
    assert "Пришлите текст заявки" in flow.session.sent_texts[-1]


async def test_find_without_crm(tmp_path, bot, session):
    db = Database(str(tmp_path / "nocrm-search.db"))
    await db.init()
    dp = create_dispatcher(db, bitrix=None, allowed_ids=set(), allow_all=True)
    flow = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=None)

    await send(flow, "/find")
    assert flow.session.sent_texts[-1] == NO_CRM
    await dp.storage.close()


# ---------------------------------------------------------------------------
# /last: последние заявки
# ---------------------------------------------------------------------------


async def test_last_shows_top_10_desc(flow):
    flow.bx.deals = [
        {
            "ID": str(100 + i),
            "TITLE": f"прочее: заявка {i}",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-10T09:00:00+10:00",
            "CONTACT_ID": "15",
            "COMMENTS": "",
        }
        for i in range(12)
    ]
    await send(flow, "/last")
    reply = flow.session.sent_texts[-1]
    lines = [line for line in reply.splitlines() if line.startswith("№")]
    assert len(lines) == 10  # топ-10, остальное отрезано
    assert lines[0].startswith("№111")  # самая свежая сверху
    assert lines[-1].startswith("№102")
    assert "№100" not in reply and "№101" not in reply


async def test_last_empty(flow):
    flow.bx.deals = []
    await send(flow, "/last")
    assert flow.session.sent_texts[-1].startswith(LAST_EMPTY)
    assert SEARCH_AGAIN_HINT in flow.session.sent_texts[-1]


async def test_last_reads_single_page_on_big_portal(flow):
    """/last на портале в 75 сделок: один запрос списка, топ-10 самых новых."""
    flow.bx.deals = [
        {
            "ID": str(100 + i),
            "TITLE": f"прочее: заявка {i}",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-10T09:00:00+10:00",
            "CONTACT_ID": "15",
            "COMMENTS": "",
        }
        for i in range(75)
    ]
    deal_list_calls = {"count": 0}
    orig_dispatch = flow.bx._dispatch

    async def counting(method: str, params: dict):
        if method == "crm.deal.list":
            deal_list_calls["count"] += 1
        return await orig_dispatch(method, params)

    flow.bx._dispatch = counting

    await send(flow, "/last")

    reply = flow.session.sent_texts[-1]
    lines = [line for line in reply.splitlines() if line.startswith("№")]
    assert len(lines) == 10
    assert lines[0].startswith("№174") and lines[-1].startswith("№165")
    assert deal_list_calls["count"] == 1  # одна страница, без выгрузки хвоста


async def test_last_names_stages_of_extra_pipelines(flow):
    """Сделка из дополнительной воронки показывает имя стадии, а не код C5:NEW."""
    flow.bx.deals.append(
        {
            "ID": "156",
            "TITLE": "прочее: заявка из доп. воронки",
            "STAGE_ID": "C5:NEW",
            "DATE_CREATE": "2026-07-18T12:00:00+10:00",
            "CONTACT_ID": "15",
            "COMMENTS": "",
        }
    )
    await send(flow, "/last")
    reply = flow.session.sent_texts[-1]
    assert "Новая (доп.)" in reply
    assert "C5:NEW" not in reply


async def test_last_without_crm(tmp_path, bot, session):
    db = Database(str(tmp_path / "nocrm-last.db"))
    await db.init()
    dp = create_dispatcher(db, bitrix=None, allowed_ids=set(), allow_all=True)
    flow = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=None)

    await send(flow, "/last")
    assert flow.session.sent_texts[-1] == NO_CRM
    await dp.storage.close()


# ---------------------------------------------------------------------------
# Длина ответа и сбой отправки
# ---------------------------------------------------------------------------


async def test_long_reply_clipped_to_telegram_limit(flow):
    """Ответ с длинными названиями укладывается в лимит sendMessage (4096).

    Раньше длинный список уходил как есть: Telegram отвечал Bad Request, а
    пользователь не получал ничего. Теперь ответ режется построчно с честным
    «…и ещё N».
    """
    flow.bx.deals = [
        {
            "ID": str(200 + i),
            "TITLE": "очень длинное описание работ " * 20,
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T10:00:00+10:00",
            "CONTACT_ID": "15",
            "COMMENTS": "",
        }
        for i in range(10)
    ]

    await send(flow, "/last")

    reply = flow.session.sent_texts[-1]
    assert len(reply) <= 4096
    assert reply.startswith("Последние заявки:")
    assert "…и ещё" in reply  # скрытые строки честно подсчитаны


async def test_reply_send_failure_answers_softly(flow, monkeypatch):
    """Сбой отправки результата не роняет апдейт: пользователь получает ответ.

    Раньше message.answer(reply) стоял вне try поиска: ошибка Telegram уходила
    в глобальный error-хендлер, пользователь не видел даже SEARCH_FAILED.
    """
    orig_request = flow.session.make_request

    async def flaky_request(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.text.startswith("Нашёл заявок"):
            raise RuntimeError("Bad Request: message is too long")
        return await orig_request(bot, method, timeout)

    monkeypatch.setattr(flow.session, "make_request", flaky_request)

    await send(flow, "/find 154")
    assert flow.session.sent_texts[-1] == SEARCH_FAILED
