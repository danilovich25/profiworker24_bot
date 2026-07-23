"""Разбор привязки напоминания к заявке из текста сотрудника.

Заказчик называет заявку как удобно: «к последней заявке», «к заявке 154»,
«к заявке по телефону 8914…», названием или организацией, либо явно просит
обычное напоминание («без привязки»). Разбор детерминированный: и фраза
внутри текста напоминания (extract_inline_binding), и ответ на вопрос
«к какой заявке?» (parse_binding_answer) считаются кодом, не моделью.
"""

import re
from dataclasses import dataclass

from app.services.bitrix import (
    _EXTENSION_RE,
    extract_bare_phone,
    normalize_phone,
)

# Номер сделки Bitrix24; длиннее — уже похоже на телефон, а не на ID.
MAX_DEAL_ID_DIGITS = 9


@dataclass(frozen=True)
class BindingRef:
    """Ссылка на заявку: как сотрудник её назвал.

    kind: "none" (обычное, без привязки), "last" (последняя заявка),
    "deal_id" (номер), "phone" (телефон клиента), "text" (название,
    организация, имя — свободный поисковый запрос).
    """

    kind: str
    value: str | None = None


# «Обычное/простое напоминание», «без привязки» — привязка явно не нужна.
_NONE_RE = re.compile(
    r"\b(?:обычн\w*|прост\w*)\s+напоминани\w*|\bбез\s+привязк\w*", re.IGNORECASE
)

# «К последней заявке»: предлог обязателен — без него «последняя заявка»
# в тексте напоминания («проверить последнюю заявку») означала бы сам текст,
# а не привязку.
_LAST_RE = re.compile(r"\b(?:к|по|для)\s+последн\w*\s+заявк\w*", re.IGNORECASE)

# Общий префикс инлайн-ссылки: предлог + «заявка» в любом падеже + пунктуация,
# которую вставляет STT, включая типографские тире («к заявке — 154»).
_REF_PREFIX = r"\b(?:к|по|для)\s+заявк[а-яё]*[\s.,:;—–-]*"

# Слова, начинающие дату/срок сразу после числа: следующая цифровая группа
# принадлежит дате, а не номеру заявки или телефону («154 23 июля»).
# «Час/минута/год» — закрытыми словоформами с границей: «часовой пояс» и
# «годовой отчёт» — смысловые слова, а не срок (ревью ULTRA-4).
# Все корни — закрытыми словоформами с границей: «мартовских договоров» и
# «числовых полей» — смысловые слова, а не дата (ревью ULTRA-6).
# Семейства раздельно: у «года» и у «июля» разные ожидания о числе перед
# ними — год 4-значный, день/час 1-2-значные (ревью ULTRA-8).
_MONTHDAY_WORDS = (
    r"январ(?:ь|я|е|ю)\b|феврал(?:ь|я|е|ю)\b|март(?:а|е|у)?\b|"
    r"апрел(?:ь|я|е|ю)\b|ма(?:й|я|е|ю)\b|июн(?:ь|я|е|ю)\b|июл(?:ь|я|е|ю)\b|"
    r"август(?:а|е|у)?\b|сентябр(?:ь|я|е|ю)\b|октябр(?:ь|я|е|ю)\b|"
    r"ноябр(?:ь|я|е|ю)\b|декабр(?:ь|я|е|ю)\b|числ(?:а|е|у|о)?\b|"
    r"час(?:а|у|ов|ам|ах)?\b|минут(?:а|ы|у|ам|ах)?\b"
)
_YEAR_WORDS = r"год(?:а|у|ов|ам)?\b"
_DATE_WORDS = _MONTHDAY_WORDS + "|" + _YEAR_WORDS
_DATE_WORD_RE = re.compile(
    r"^[\s.,:;()—–-]*(?:%s)" % _DATE_WORDS, re.IGNORECASE
)
_MONTHDAY_WORD_RE = re.compile(
    r"^[\s.,:;()—–-]*(?:%s)" % _MONTHDAY_WORDS, re.IGNORECASE
)
_YEAR_WORD_RE = re.compile(
    r"^[\s.,:;()—–-]*(?:%s)" % _YEAR_WORDS, re.IGNORECASE
)

# Слова времени суток после числа — «к 15 вечера» это время, не заявка.
# Формы закрытые: «ночной выезд», «утренний» — смысловые слова (ULTRA-5).
_TIME_STOP_WORDS = (
    _DATE_WORDS
    + r"|утр[ао]?м?\b|вечер(?:а|у|ом)?\b|дн[яеё]м?\b|ноч(?:и|ью)?\b"
)

# Начало ЧИСЛОВОЙ даты или времени сразу после группы цифр: «23.07», «15:00»,
# «23/07», «23 – 07». Точка — только слитно: «. 23» после номера — это конец
# предложения, а не дата (ревью ULTRA-3/9/10).
_NUMERIC_DATE_RE = re.compile(r"^\s*[:/\-—–]\s*\d|^\.\d")

# Следующая цифровая группа после уже захваченного идентификатора. Если она
# не открывает дату/время — достоверно разобрать фразу нельзя, бот спрашивает
# кнопками вместо угадайки (политика ревью ULTRA-5).
_TRAILING_DIGITS_RE = re.compile(r"^\s+\(?(\d{1,4})")


def _tail_group_is_dateish(num: str, after: str) -> bool:
    """Хвостовая цифровая группа согласована со сроком после неё.

    «Год» требует 4-значное число, месяц/число/час — день ≤31; числовая
    дата («23.07», «–07–2026») согласована сама по себе. Несогласованное
    («2 года гарантии») датой НЕ считается (ревью ULTRA-8/9).
    """
    if _NUMERIC_DATE_RE.match(after):
        # Первая группа обязана быть днём (≤31) или годом (4 цифры):
        # «456-789» — не дата (ревью ULTRA-11).
        return (len(num) <= 2 and int(num) <= 31) or len(num) == 4
    if _YEAR_WORD_RE.match(after):
        return len(num) == 4
    if _MONTHDAY_WORD_RE.match(after):
        return len(num) <= 2 and int(num) <= 31
    return False


def _ambiguous_digit_tail(tail: str) -> bool:
    """Цифры следом за идентификатором, которые не похожи на дату/время."""
    match = _TRAILING_DIGITS_RE.match(tail)
    if match is None:
        return False
    return not _tail_group_is_dateish(match.group(1), tail[match.end() :])

# «К заявке по телефону 8914…» — телефон назван явно; в записи допустимы
# и типографские тире (ревью ULTRA-8).
_PHONE_KEYWORD_RE = re.compile(
    _REF_PREFIX + r"(?:по\s+)?(?:телефону?|номеру\s+телефона)[\s.,:;—–-]*"
    r"(\(?\+?[\d(][\d\s()./—–\-]{4,}\d)",
    re.IGNORECASE,
)

# «К заявке 89141234567» — длинная цифровая строка после «к заявке» это
# телефон, а не номер сделки (тот короче MAX_DEAL_ID_DIGITS). Десять цифр
# (без восьмёрки) — тоже штатная российская форма (normalize_phone),
# поэтому минимум — 10 знаков (ревью R5).
# Голая форма начинается только с телефонного старта (7/8/9 или «+»):
# «к заявке 154 23-07-2026» — это номер заявки с датой, не телефон
# (ревью ULTRA-10).
_PHONE_BARE_RE = re.compile(
    _REF_PREFIX + r"((?:\+|\(?[789])[\d\s()./—–\-]{8,}\d)", re.IGNORECASE
)

# Инлайн-добавочный сразу после номера: вырезается из текста напоминания
# вместе с номером («…-67 доб. 12 завтра» → «завтра»; ревью ULTRA-8).
# Маркеру нужна граница слова и ХОТЯ БЫ одна цифра добавочного: «внести»
# и «добавить» — обычные слова, не маркеры (ревью ULTRA-9).
_INLINE_EXTENSION_RE = re.compile(
    r"^[\s.,]*(?:(?:доб|вн|extension|ext)(?![a-zа-яё])|[x#])"
    r"[\s.,:]*\d{1,7}\b",
    re.IGNORECASE,
)

# «К заявке 154», «к заявке №154», «к заявке номер: 154». Захватывается
# ПЕРВАЯ цифровая группа; STT-хвосты («123 456 789») подклеивает код,
# останавливаясь перед днём даты («154 23 июля» — это ID и дата).
_DEAL_NUM_RE = re.compile(
    _REF_PREFIX + r"(?:№\s*|номер[ау]?[\s.,:;—–-]*)?(\d{1,%d})(?!\d)"
    % MAX_DEAL_ID_DIGITS,
    re.IGNORECASE,
)

# Самоисправление голосом без слова «заявка»: «…, нет, к 155», «нет — к
# 155», «нет. к 155». Числа, похожие на время («к 15:00», «к 15 вечера»)
# или дату («к 23 июля»), исправлением ПРИВЯЗКИ не считаются.
# Слово времени глушит исправление только для чисел, похожих на время
# (1-2 цифры): «к 155 вечером» — номер заявки, не 155 часов (ревью ULTRA-7).
# Пауза-тире после предлога («нет, к — 155») тоже пунктуация. Захватывается
# ЧИСЛО кандидата-исправления; согласование со временем/датой решает код
# (_correction_conflict) — регэксп-lookahead на это уже дважды пробивался
# бэктрекингом (ревью ULTRA-8/9/10).
_CORRECTION_RE = re.compile(
    r"\bнет\b[\s,.:;!?—–-]*(?:лучше\s+)?(?:к|по|на)[\s,—–-]+"
    r"(?:заявк[а-яё]*[\s.,:;—–-]*)?"
    r"(?:№\s*)?(\d{1,9})(?![.,:/—–-]?\d)",
    re.IGNORECASE,
)

# Слово времени/суток (с широкой пунктуацией перед ним) после числа.
# БЕЗ «года»: 1-2-значное число годом быть не может (ревью ULTRA-11).
_SHORT_TIME_WORDS = (
    _MONTHDAY_WORDS
    + r"|утр[ао]?м?\b|вечер(?:а|у|ом)?\b|дн[яеё]м?\b|ноч(?:и|ью)?\b"
)
_TIME_SUPPRESS_RE = re.compile(
    r"^[\s,.;:!?()—–-]*(?:%s)" % _SHORT_TIME_WORDS, re.IGNORECASE
)
_YEAR_SUPPRESS_RE = re.compile(
    r"^[\s,.;:!?()—–-]*(?:%s)" % _YEAR_WORDS, re.IGNORECASE
)


def _correction_conflict(clean: str) -> bool:
    """Есть ли в остатке текста самоисправление привязки.

    Число глушится временем/датой только при СОГЛАСОВАНИИ: 1-2 цифры —
    словом времени/суток или числовой датой (в т.ч. с пробелами «23 / 07»),
    4 цифры — словом «год». «К 155 вечером» и «к 155 23 июля» — номера
    заявок (ревью ULTRA-7/10).
    """
    if _CORRECTION_KEYWORD_RE.search(clean):
        return True
    for match in _CORRECTION_RE.finditer(clean):
        num = match.group(1)
        rest = clean[match.end() :]
        if len(num) <= 2 and (
            _TIME_SUPPRESS_RE.match(rest)
            or re.match(r"^\s*[(\s]*[:/.\-—–]\s*\d", rest)
        ):
            continue
        if len(num) == 4 and _YEAR_SUPPRESS_RE.match(rest):
            continue
        return True
    return False

# Исправление с ЛЮБЫМ словом-маркером ссылки («нет, к последней…», «нет,
# по телефону…», «нет, к номеру…») — всегда уточняем кнопками: угадать,
# что имел в виду человек, нельзя (ревью ULTRA-5).
_CORRECTION_KEYWORD_RE = re.compile(
    r"\bнет\b[\s,.:;!?—–-]*(?:лучше\s+)?(?:(?:к|по|на|для)\s+)?"
    r"(?:заявк|последн|телефон|номер|№)",
    re.IGNORECASE,
)

# Ответ на вопрос «к какой заявке?»: отказ от привязки.
_ANSWER_NONE_RE = re.compile(
    r"^(?:без\s+привязк\w*|обычн\w*(?:\s+напоминани\w*)?|нет|не\s+надо|не\s+нужно)[.!]?$",
    re.IGNORECASE,
)

# «Последняя» как ссылка на заявку — только когда ответ ЦЕЛИКОМ об этом:
# «последняя», «к последней заявке», «по последней». Вхождение слова внутри
# названия («ООО Последний шанс», «Последняя миля») — поисковый запрос.
_ANSWER_LAST_RE = re.compile(
    r"^(?:(?:к|по|для)\s+)?(?:самой\s+)?последн\w*(?:\s+заявк\w*)?\s*[.!]*$",
    re.IGNORECASE,
)

# Служебные слова перед идентификатором в ответе: «к заявке номер 154»,
# «номер заявки: 154», «по телефону 8914…», «№154». Порядок свободный, а
# STT вставляет знаки препинания («номер заявки: 154»), поэтому слова
# срезаются итеративно по одному, вместе с пунктуацией-разделителем после.
# Падежные формы — ЗАКРЫТЫМ списком: произвольный кириллический хвост
# съедал бы смысловые слова («Номерной», «Телефонов» — это названия,
# а не служебные слова; ревью R5).
# Однобуквенные предлоги срезаются только перед ПРОБЕЛОМ: «К-12» — это
# название, а не «к» + номер 12 (ревью ULTRA). Существительные принимают
# и типографские тире («заявке—154», «телефону–8914…»).
_SERVICE_WORD_RE = re.compile(
    r"^(?:(?:к|по|для)(?=\s)"
    r"|(?:заявк(?:а|и|е|у|ой|ою)?"
    r"|номер(?:а|у|е|ом)?"
    r"|телефон(?:а|у|е|ом)?"
    r")(?=[\s.,:;№\d—–-]|$)|№)",
    re.IGNORECASE,
)

# Пунктуация-разделитель между служебными словами и идентификатором.
_SERVICE_SEP = " \t.,:;-—–"


def _strip_service_words(text: str) -> str:
    """Снимает ведущие служебные слова ответа: «к заявке номер: …» → «…»."""
    rest = text
    while True:
        match = _SERVICE_WORD_RE.match(rest)
        if match is None or match.end() == 0:
            return rest
        rest = rest[match.end() :].lstrip(_SERVICE_SEP)


def _cut(text: str, start: int, end: int) -> str:
    """Вырезает совпадение и схлопывает оставшиеся пробелы."""
    rest = (text[:start] + " " + text[end:]).strip()
    return re.sub(r"\s+", " ", rest)


def _normalize_ref_phone(raw: str) -> str | None:
    """Телефон ссылки: явный «+» уважается как есть (E.164), без RU-догадок.

    normalize_phone превращает «+45 12 34 56 78» (10 цифр) в «+7451…» —
    для явных международных номеров это ложь (ревью ULTRA-4). Без «+»
    работает штатная российская нормализация.
    """
    text = (raw or "").strip()
    # Добавочный не входит в E.164 ни в одной форме («+7… доб. 12», «EXT»,
    # «extension», «x12») и в любом регистре — маркеры те же, что у ядра
    # (bitrix._EXTENSION_RE ищется по lower-копии, позиции совпадают;
    # ревью ULTRA-7).
    marker = _EXTENSION_RE.search(text.lower())
    if marker is not None:
        text = text[: marker.start()]
    if text.lstrip(" 	(").startswith("+"):
        digits = re.sub(r"\D", "", text)
        return "+" + digits if 10 <= len(digits) <= 15 else None
    return normalize_phone(text)


def _digit_prefix_end(group: str, need: int) -> int:
    """Позиция сразу после need-й цифры группы."""
    count = 0
    for index, char in enumerate(group):
        if char.isdigit():
            count += 1
            if count == need:
                return index + 1
    return len(group)


def _rest_is_dateish(rest: str, tail: str) -> bool:
    """Остаток захвата после номера — согласованная дата, а не чужие цифры."""
    match = re.match(r"^[\s.,;:()—–-]*(\d{1,4})(.*)$", rest, re.S)
    if match is None:
        return False
    num, after = match.group(1), match.group(2)
    if _NUMERIC_DATE_RE.match(after):
        return True
    return _tail_group_is_dateish(num, after if after.strip() else tail)


def _resolve_phone_span(group: str, tail: str) -> tuple[str | None, int, bool]:
    """(телефон, потреблено, вопрос?) для захваченной цифровой группы.

    Детерминированная схема вместо эвристик разнотипности (ревью ULTRA-10):
    - российские формы (старт 7/8 → 11 цифр, иначе → 10; «+7» → 11):
      длина носителя известна, остаток-цифры допустимы ТОЛЬКО как
      согласованная дата («23 июля», «23-07-2026», «2026 года») — иначе
      граница номера недостоверна и бот спрашивает;
    - прочие «+»-номера: длину страны угадать нельзя — номер валиден
      целиком; пробельно-отделённый цифровой хвост при валидном префиксе —
      дата (срезается) или вопрос.
    """
    digits_all = re.sub(r"\D", "", group)
    if not digits_all:
        return None, 0, False
    plus = group.lstrip(" \t(").startswith("+")
    if plus and digits_all[0] != "7":
        # Перебор разрезов по границам токенов ОТ НАИМЕНЬШЕГО валидного
        # префикса: «+45 12 34 56 78 23 – 07-2026» — номер 10 цифр, остаток
        # целиком согласованная дата (ревью ULTRA-11). Валидный префикс с
        # несогласованным цифровым остатком — вопрос.
        parts = list(re.finditer(r"\S+", group))
        prefix_valid_seen = False
        for index in range(1, len(parts)):
            prefix = group[: parts[index - 1].end()].rstrip(" .,;:()—–-")
            prefix_digits = re.sub(r"\D", "", prefix)
            if not 10 <= len(prefix_digits) <= 15:
                continue
            prefix_valid_seen = True
            if _rest_is_dateish(group[len(prefix) :], tail):
                return _normalize_ref_phone(prefix), len(prefix), False
        if prefix_valid_seen:
            return None, 0, True
        if 10 <= len(digits_all) <= 15:
            return _normalize_ref_phone(group), len(group), False
        return None, 0, True
    # Российские формы (включая «+7...»).
    need = 11 if digits_all[0] in "78" or plus else 10
    if len(digits_all) < need:
        return None, 0, False
    end = _digit_prefix_end(group, need)
    rest = group[end:]
    if re.search(r"\d", rest):
        if not _rest_is_dateish(rest, tail):
            return None, 0, True
    phone = _normalize_ref_phone(group[:end])
    return phone, end, False


def _extract_one(text: str) -> tuple[str, BindingRef | None]:
    """Первая инлайн-ссылка в тексте: (текст без фразы, ссылка | None)."""
    raw = (text or "").strip()
    if not raw:
        return raw, None
    match = _NONE_RE.search(raw)
    if match:
        return _cut(raw, match.start(), match.end()), BindingRef("none")
    match = _LAST_RE.search(raw)
    if match:
        return _cut(raw, match.start(), match.end()), BindingRef("last")
    keyword_match = _PHONE_KEYWORD_RE.search(raw)
    match = keyword_match or _PHONE_BARE_RE.search(raw)
    if match is not None and keyword_match is None:
        # Голая форма: если ПЕРВАЯ цифровая группа сама валидный ID (≤9
        # цифр), телефон складывается только вместе с датой — это «номер
        # заявки + срок», решает ветка номера («к заявке 91 23-07-2026»;
        # ревью ULTRA-11).
        first = re.match(r"[\s(+]*(\d+)", match.group(1))
        if first is not None and len(first.group(1)) <= MAX_DEAL_ID_DIGITS:
            match = None
    if match:
        group = match.group(1)
        after_group = raw[match.end(1) :]
        phone, consumed, ask = _resolve_phone_span(group, after_group)
        if ask:
            # Граница номера недостоверна (чужие цифры вплотную к номеру):
            # уточняем кнопками, а не гадаем (ревью ULTRA-5/10).
            return raw, BindingRef("conflict")
        if phone is not None:
            end = match.start(1) + consumed
            ext = _INLINE_EXTENSION_RE.match(raw[end:])
            if ext is not None:
                # Добавочный — служебный хвост номера, тексту не принадлежит.
                end += ext.end()
            if _ambiguous_digit_tail(raw[end:]):
                # За номером цифры, не похожие на дату/время: достоверной
                # границы номера нет — уточняем кнопками (ревью ULTRA-5).
                return raw, BindingRef("conflict")
            return _cut(raw, match.start(), end), BindingRef("phone", phone)
    match = _DEAL_NUM_RE.search(raw)
    if match:
        digits = match.group(1)
        end = match.end(1)
        if _ambiguous_digit_tail(raw[end:]):
            # «154 2 договора», «123 456 789…»: отличить разбитый номер от
            # «номер + количество» нельзя — спрашиваем, а не гадаем.
            return raw, BindingRef("conflict")
        return _cut(raw, match.start(), end), BindingRef("deal_id", digits)
    return raw, None


def extract_inline_binding(text: str) -> tuple[str, BindingRef | None]:
    """Находит привязку прямо в тексте напоминания.

    Возвращает (текст без фразы привязки, ссылка | None). None — привязка
    не названа, нужно спросить. Распознаются только однозначные формы
    (последняя, номер, телефон, «обычное») — свободное название заявки в
    потоке текста не угадывается, чтобы не съесть само напоминание.

    Разные ссылки в одном сообщении («к заявке 154, нет, к заявке 155»)
    и голосовые самоисправления без слова «заявка» («…, нет, к 155») —
    kind="conflict": выбор не угадывается, его задаёт явный вопрос
    (ревью ULTRA). Повтор одной и той же ссылки конфликтом не считается.
    """
    refs: list[BindingRef] = []
    clean = (text or "").strip()
    while True:
        remainder, ref = _extract_one(clean)
        if ref is not None and ref.kind == "conflict":
            return (text or "").strip(), ref
        if ref is None or len(remainder) >= len(clean):
            # Каждая найденная ссылка строго укорачивает текст — второе
            # условие лишь страхует от зацикливания.
            break
        refs.append(ref)
        clean = remainder
    if not refs:
        return clean, None
    if len({(ref.kind, ref.value) for ref in refs}) > 1:
        return (text or "").strip(), BindingRef("conflict")
    if _correction_conflict(clean):
        return (text or "").strip(), BindingRef("conflict")
    return clean, refs[0]


def is_vague_query(text: str) -> bool:
    """Ответ слишком пуст для текстового поиска заявки.

    Односимвольные ответы и ответы из одних служебных слов («к заявке»)
    запускали бы широкий CRM-поиск, где единственное случайное совпадение
    молча привязало бы напоминание не туда. Такое честнее переспросить.
    """
    bare = _strip_service_words((text or "").strip()).strip(_SERVICE_SEP + "!?")
    return len(bare) < MIN_QUERY_LEN


# Минимальная содержательная длина текстового запроса — как у поиска
# (search.MIN_TEXT_QUERY_LEN): короче не ищем, переспрашиваем.
MIN_QUERY_LEN = 2


def parse_binding_answer(text: str) -> BindingRef:
    """Разбирает ответ на вопрос «к какой заявке привязать?».

    Любой не распознанный явно ответ — поисковый запрос (название,
    организация, имя клиента): решает то же ядро, что и «Найти».
    """
    raw = (text or "").strip()
    # STT автоматически ставит точку/запятую в конце реплики: «154.»,
    # «Без привязки.» — завершающая пунктуация не меняет смысла ответа.
    raw = raw.rstrip(" .,!?;:")
    if _ANSWER_NONE_RE.match(raw):
        return BindingRef("none")
    if _ANSWER_LAST_RE.match(raw):
        return BindingRef("last")
    bare = _strip_service_words(raw).strip()
    compact = re.sub(r"[\s()\-]", "", bare)
    if compact.isdecimal() and len(compact) <= MAX_DEAL_ID_DIGITS:
        return BindingRef("deal_id", compact)
    # Телефоном считается только ответ, ЦЕЛИКОМ являющийся номером: фраза
    # с цифрами внутри — поисковый запрос, а не телефон. Юникод-тире
    # приводятся к дефису до проверки — ядро (extract_bare_phone) знает
    # только ASCII-пунктуацию (ревью ULTRA-9).
    bare_norm = re.sub(r"[—–]", "-", bare)
    if extract_bare_phone(bare_norm) is not None:
        phone = _normalize_ref_phone(bare_norm)
        if phone is not None:
            return BindingRef("phone", phone)
    # Текстовый поиск получает ПОЛНЫЙ ответ: настоящее название может
    # начинаться со служебного слова («Телефон доверия»), и точное
    # совпадение полным ответом обязано победить поиск по ядру (ревью
    # ULTRA-2). Ядро без префикса — мягкий кандидат (core_text_query).
    return BindingRef("text", raw)


def core_text_query(text: str) -> str:
    """Ядро ответа без служебного префикса — мягкий поисковый кандидат.

    «К заявке Ромашка» → «Ромашка»: общий поиск служебных слов не знает и
    с префиксом ответил бы «не нашёл» (ревью R5). Совпадение по ядру —
    неточное: шаг привязки подтверждает его кнопкой, а не молча.
    """
    return _strip_service_words((text or "").strip()).strip(_SERVICE_SEP + "!?")
