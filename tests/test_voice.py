"""Голосовые сообщения: лимиты, распознавание, тот же поток заявки.

Хендлер тестируется через диспетчер с замоканным STT (recognize_ogg);
сам recognize_ogg — отдельно, с перехватом HTTP через respx.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.db import Database
from app.handlers import routers
from app.handlers.search import SEARCH_AGAIN_HINT, SearchFlow
from app.handlers.voice import VOICE_NOT_RECOGNIZED, VOICE_STATE_CHANGED, VOICE_TOO_LONG
from app.main import create_dispatcher
from app.services import llm, speech
from tests.conftest import make_message_update, make_voice_update
from tests.test_handlers_messages import FULL_ORDER, FakeBitrix, press_card
from tests.test_search import FakeSearchBitrix

RECOGNIZED = "Иван, 89141234567, сантехника, замена крана"


@pytest.fixture(autouse=True)
def _detach_routers():
    """Роутеры - модульные синглтоны; после теста отвязываем их от диспетчера."""
    yield
    for r in routers:
        r._parent_router = None


@pytest.fixture
async def flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "voice.db"))
    await db.init()
    bx = FakeBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


@pytest.fixture
async def search_flow(tmp_path, bot, session):
    db = Database(str(tmp_path / "voice-search.db"))
    await db.init()
    bx = FakeSearchBitrix()
    dp = create_dispatcher(db, bitrix=bx, allowed_ids=set(), allow_all=True)
    harness = SimpleNamespace(dp=dp, bot=bot, session=session, db=db, bx=bx)
    yield harness
    await dp.storage.close()


def recognize_mock(monkeypatch, text: str = RECOGNIZED) -> dict:
    calls = {"count": 0}

    async def fake(data: bytes) -> str:
        calls["count"] += 1
        return text

    monkeypatch.setattr(speech, "recognize_ogg", fake)
    return calls


def parse_order_mock(monkeypatch, order=FULL_ORDER):
    async def fake(text: str):
        return order

    monkeypatch.setattr(llm, "parse_order", fake)


async def send_voice(flow, duration: int = 5, file_size: int = 1024, user_id: int = 1):
    await flow.dp.feed_update(
        flow.bot,
        make_voice_update(flow.bot, duration=duration, file_size=file_size, user_id=user_id),
    )


# ---------------------------------------------------------------------------
# Хендлер голосовых
# ---------------------------------------------------------------------------


async def test_voice_to_order_flow(flow, monkeypatch):
    """Голос -> текст -> тот же поток заявки: карточка и сделка."""
    stt = recognize_mock(monkeypatch)
    parse_order_mock(monkeypatch)

    await send_voice(flow)

    texts = flow.session.sent_texts
    assert any("Распознал" in t and RECOGNIZED in t for t in texts)
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text
    assert stt["count"] == 1

    await press_card(flow, "create", card)
    assert len(flow.bx.deals) == 1
    assert "Заявка №154 создана" in flow.session.sent_texts[-1]


async def test_voice_stt_error_soft_answer(flow, monkeypatch):
    """Сбой распознавания: мягкая подсказка, апдейт не уронен."""

    async def failing(data: bytes) -> str:
        raise speech.SpeechUnavailable("сеть")

    monkeypatch.setattr(speech, "recognize_ogg", failing)
    parse_order_mock(monkeypatch)

    await send_voice(flow)
    assert flow.session.sent_texts[-1] == VOICE_NOT_RECOGNIZED

    # бот жив, обычный текст проходит как всегда
    await flow.dp.feed_update(flow.bot, make_message_update(flow.bot, "заявка"))
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text


async def test_voice_too_long_rejected_without_stt(flow, monkeypatch):
    stt = recognize_mock(monkeypatch)

    await send_voice(flow, duration=31)
    assert flow.session.sent_texts[-1] == VOICE_TOO_LONG
    assert stt["count"] == 0  # к распознаванию не ходили


async def test_voice_too_big_rejected_without_stt(flow, monkeypatch):
    stt = recognize_mock(monkeypatch)

    await send_voice(flow, file_size=2 * 1024 * 1024)
    assert flow.session.sent_texts[-1] == VOICE_TOO_LONG
    assert stt["count"] == 0


async def test_voice_unknown_size_checked_via_get_file(flow, monkeypatch):
    """Размер отсутствует в апдейте: до скачивания его сообщает getFile.

    Раньше отсутствующий Voice.file_size считался нулём, и двухмегабайтный
    файл целиком скачивался в память, чтобы только потом быть отклонённым.
    """
    stt = recognize_mock(monkeypatch)
    flow.session.get_file_size = 2 * 1024 * 1024  # настоящий размер из getFile

    await send_voice(flow, file_size=None)

    assert flow.session.sent_texts[-1] == VOICE_TOO_LONG
    assert stt["count"] == 0
    assert flow.session.downloads == 0  # файл не скачивался


async def test_voice_unknown_everywhere_capped_during_download(flow, monkeypatch):
    """Размер не заявлен нигде — скачивание жёстко обрезается на лимите.

    Voice.file_size=None и getFile.file_size=None: раньше поток качался в
    память целиком, и только STT отклонял его после загрузки. Теперь читается
    не больше лимита с хвостиком: превышение прерывает скачивание на месте.
    """
    stt = recognize_mock(monkeypatch)
    flow.session.get_file_size = None  # и getFile не знает размера
    chunk = b"x" * 65536
    flow.session.stream_chunks = [chunk] * 100  # ~6.5 МБ потока

    await send_voice(flow, file_size=None)

    assert flow.session.sent_texts[-1] == VOICE_TOO_LONG
    assert stt["count"] == 0  # к распознаванию не ходили
    # прочитано ровно до превышения лимита (1 МБ / 64 КБ = 16 кусков + 1),
    # остальные ~83 куска не скачивались
    assert flow.session.streamed_chunks <= 17


async def test_voice_small_stream_without_sizes_recognized(flow, monkeypatch):
    """Маленькое голосовое без заявленных размеров штатно распознаётся."""
    stt = recognize_mock(monkeypatch)
    parse_order_mock(monkeypatch)
    flow.session.get_file_size = None
    flow.session.stream_chunks = [b"OggS", b"data"]

    await send_voice(flow, file_size=None)

    assert stt["count"] == 1
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text


async def test_voice_empty_recognition_soft_answer(flow, monkeypatch):
    """Тишина в голосовом (пустой результат STT) — та же мягкая подсказка."""
    recognize_mock(monkeypatch, text="")

    await send_voice(flow)
    assert flow.session.sent_texts[-1] == VOICE_NOT_RECOGNIZED


async def test_voice_same_text_twice_soft_dedup(flow, monkeypatch):
    """Два голосовых с одинаковым распознанным текстом ловит контент-дедуп."""
    recognize_mock(monkeypatch)
    parse_order_mock(monkeypatch)

    await send_voice(flow)
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text

    await send_voice(flow)  # другой файл, но распознан тот же текст
    assert "Создать всё равно?" in flow.session.sent_texts[-1]


async def test_voice_in_search_is_cleaned_and_keeps_search(search_flow, monkeypatch):
    search_flow.bx.contacts.append(
        {"ID": "18", "NAME": "Андрей", "LAST_NAME": "", "PHONE": ""}
    )
    search_flow.bx.deals.append(
        {
            "ID": "156",
            "TITLE": "прочее: консультация",
            "STAGE_ID": "NEW",
            "DATE_CREATE": "2026-07-18T11:00:00+10:00",
            "CONTACT_ID": "18",
            "COMMENTS": "",
        }
    )
    recognize_mock(monkeypatch, text="Андрея можешь найти")

    await send_text(search_flow, "/find")
    await send_voice(search_flow)

    assert "Распознал: «Андрея можешь найти»" in search_flow.session.sent_texts
    reply = search_flow.session.sent_texts[-1]
    assert "№156" in reply and SEARCH_AGAIN_HINT in reply
    context = search_flow.dp.fsm.get_context(bot=search_flow.bot, chat_id=1, user_id=1)
    assert await context.get_state() == SearchFlow.query.state


# ---------------------------------------------------------------------------
# Голос учитывает текущее состояние FSM (опросник, уточнения, поиск)
# ---------------------------------------------------------------------------


def parse_order_unavailable(monkeypatch):
    async def fake(text: str):
        raise llm.LLMUnavailable("недоступна")

    monkeypatch.setattr(llm, "parse_order", fake)


async def send_text(flow, text: str):
    await flow.dp.feed_update(flow.bot, make_message_update(flow.bot, text))


async def test_voice_in_form_answers_current_question(flow, monkeypatch):
    """Голос в опроснике отвечает на вопрос, а не стирает собранные поля.

    Раньше глобальный F.voice всегда запускал handle_order_text: голосовой
    ответ на «Как зовут клиента?» при недоступной модели сбрасывал опросник
    к вопросу 1, теряя всё введённое.
    """
    parse_order_unavailable(monkeypatch)
    recognize_mock(monkeypatch, text="Иван")

    await send_text(flow, "Иван, замена крана")
    assert "Вопрос 1 из 5" in flow.session.sent_texts[-1]

    await send_voice(flow)  # голосом: «Иван»
    assert "Вопрос 2 из 5" in flow.session.sent_texts[-1]  # опрос идёт дальше
    assert sum("Вопрос 1 из 5" in t for t in flow.session.sent_texts) == 1

    await send_text(flow, "89141234567")
    assert "Вопрос 3 из 5" in flow.session.sent_texts[-1]


async def test_voice_in_form_problem_fills_description(flow, monkeypatch):
    parse_order_unavailable(monkeypatch)
    recognize_mock(monkeypatch, text="заменить кран на кухне")

    await send_text(flow, "заявка")
    await send_text(flow, "Иван")
    await send_text(flow, "нет")
    await send_text(flow, "сантехника")  # категорию можно и напечатать
    assert "Вопрос 4 из 5" in flow.session.sent_texts[-1]

    await send_voice(flow)  # голосом: описание
    assert "Вопрос 5 из 5" in flow.session.sent_texts[-1]

    await send_text(flow, "завтра")
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text
    assert "заменить кран на кухне" in card.text


async def test_voice_bare_number_answers_phone_question(flow, monkeypatch):
    """Голос с голым номером на вопросе о телефоне подставляет номер."""
    parse_order_mock(monkeypatch, FULL_ORDER.model_copy(update={"phone": None}))
    recognize_mock(monkeypatch, text="8 914 123 45 67")

    await send_text(flow, "Иван, сантехника, замена крана")
    assert "Не указан телефон клиента" in flow.session.sent_texts[-1]

    await send_voice(flow)
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text
    assert "+79141234567" in card.text
    assert sum("Не указан телефон" in t for t in flow.session.sent_texts) == 1


async def test_voice_phrase_on_phone_question_reparses_once(flow, monkeypatch):
    """Голосовая фраза вместо номера переразбирается, вопрос не повторяется."""
    orders = {
        "иван, сантехника, замена крана": FULL_ORDER.model_copy(update={"phone": None}),
        "мария, электрика, заменить розетку": FULL_ORDER.model_copy(
            update={"client_name": "Мария", "phone": None}
        ),
    }

    async def fake(text: str):
        return orders[text.lower()]

    monkeypatch.setattr(llm, "parse_order", fake)
    recognize_mock(monkeypatch, text="Мария, электрика, заменить розетку")

    await send_text(flow, "Иван, сантехника, замена крана")
    assert "Не указан телефон клиента" in flow.session.sent_texts[-1]

    await send_voice(flow)  # фраза, а не номер: переразбор без второго вопроса
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text and "Мария" in card.text
    assert sum("Не указан телефон" in t for t in flow.session.sent_texts) == 1


async def test_voice_and_following_text_are_serialized(flow, monkeypatch):
    """Текст после долгого STT ждёт голос и попадает уже в следующий шаг."""
    parse_order_unavailable(monkeypatch)

    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow_stt(data: bytes) -> str:
        entered.set()
        await gate.wait()
        return "заменить кран на кухне"

    monkeypatch.setattr(speech, "recognize_ogg", slow_stt)

    await send_text(flow, "заявка")
    await send_text(flow, "Иван")
    await send_text(flow, "нет")
    await send_text(flow, "сантехника")
    assert "Вопрос 4 из 5" in flow.session.sent_texts[-1]  # ждём описание работ

    voice = asyncio.create_task(
        flow.dp.feed_update(flow.bot, make_voice_update(flow.bot))
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    following_text = asyncio.create_task(
        flow.dp.feed_update(flow.bot, make_message_update(flow.bot, "завтра"))
    )
    await asyncio.sleep(0.05)
    assert not following_text.done()

    gate.set()
    await asyncio.gather(voice, following_text)
    card = flow.session.sent_messages[-1]
    assert "Проверьте заявку" in card.text
    assert "Описание: заменить кран на кухне" in card.text
    assert "Срок: завтра" in card.text
    assert VOICE_STATE_CHANGED not in flow.session.sent_texts


async def test_voice_category_answer_on_ask_category(flow, monkeypatch):
    """Голосовое название категории на уточняющем вопросе продолжает поток."""
    parse_order_mock(monkeypatch, FULL_ORDER.model_copy(update={"category": None}))
    recognize_mock(monkeypatch, text="Сантехника")

    await send_text(flow, "Иван, 89141234567, замена крана")
    assert "категорию" in flow.session.sent_texts[-1]

    await send_voice(flow)
    assert "Проверьте заявку" in flow.session.sent_messages[-1].text


# ---------------------------------------------------------------------------
# recognize_ogg: HTTP-обвязка SpeechKit v1
# ---------------------------------------------------------------------------


@pytest.fixture
def stt_settings(monkeypatch):
    monkeypatch.setattr(speech.settings, "yc_api_key", "test-key")
    monkeypatch.setattr(speech.settings, "yc_folder_id", "folder1")


async def test_recognize_ogg_success(respx_mock, stt_settings):
    route = respx_mock.post(speech.STT_URL).mock(
        return_value=httpx.Response(200, json={"result": "замена крана"})
    )

    assert await speech.recognize_ogg(b"OggS...") == "замена крана"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Api-Key test-key"
    assert "folderId=folder1" in str(request.url)
    assert "lang=ru-RU" in str(request.url)
    assert "format=oggopus" in str(request.url)
    assert request.content == b"OggS..."


async def test_recognize_ogg_http_error(respx_mock, stt_settings):
    respx_mock.post(speech.STT_URL).mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(speech.SpeechUnavailable):
        await speech.recognize_ogg(b"OggS...")


async def test_recognize_ogg_network_error(respx_mock, stt_settings):
    respx_mock.post(speech.STT_URL).mock(side_effect=httpx.ConnectError("нет сети"))

    with pytest.raises(speech.SpeechUnavailable):
        await speech.recognize_ogg(b"OggS...")


async def test_recognize_ogg_oversize_without_request(respx_mock, stt_settings):
    route = respx_mock.post(speech.STT_URL)

    with pytest.raises(speech.SpeechUnavailable):
        await speech.recognize_ogg(b"x" * (speech.MAX_SIZE_BYTES + 1))
    assert not route.called  # лимит 1 МБ отсекается до похода в сеть


async def test_recognize_ogg_without_keys(monkeypatch):
    monkeypatch.setattr(speech.settings, "yc_api_key", "")

    with pytest.raises(speech.SpeechUnavailable):
        await speech.recognize_ogg(b"OggS...")
