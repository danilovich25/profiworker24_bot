"""Разбор привязки напоминания к заявке из текста сотрудника.

Заказчик называет заявку как удобно: «к последней заявке», «к заявке 154»,
«к заявке по телефону 8914…», названием или организацией, либо явно просит
обычное напоминание («без привязки»). Разбор детерминированный: и фраза
внутри текста напоминания (extract_inline_binding), и ответ на вопрос
«к какой заявке?» (parse_binding_answer) считаются кодом, не моделью.
"""

import re
from dataclasses import dataclass

from app.services.bitrix import extract_bare_phone, normalize_phone

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
_DATE_WORDS = (
    r"январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|"
    r"декабр|числ|год(?:а|у|ов|ам)?\b|час(?:а|у|ов|ам|ах)?\b|"
    r"минут(?:а|ы|у|ам|ах)?\b"
)
_DATE_WORD_RE = re.compile(r"^[\s.,:;—–-]*(?:%s)" % _DATE_WORDS, re.IGNORECASE)

# Слова времени суток после числа — «к 15 вечера» это время, не заявка.
_TIME_STOP_WORDS = _DATE_WORDS + r"|утр|вечер|дн[яеё]м?\b|ноч"

# Начало ЧИСЛОВОЙ даты или времени сразу после группы цифр: «23.07», «15:00»,
# «23/07» — группа принадлежит сроку, а не номеру (ревью ULTRA-3).
_NUMERIC_DATE_RE = re.compile(r"^[.:/]\d")

# «К заявке по телефону 8914…» — телефон назван явно (тире тоже пунктуация).
_PHONE_KEYWORD_RE = re.compile(
    _REF_PREFIX + r"(?:по\s+)?(?:телефону?|номеру\s+телефона)[\s.,:;—–-]*"
    r"(\+?\d[\d\s()\-]{4,}\d)",
    re.IGNORECASE,
)

# «К заявке 89141234567» — длинная цифровая строка после «к заявке» это
# телефон, а не номер сделки (тот короче MAX_DEAL_ID_DIGITS). Десять цифр
# (без восьмёрки) — тоже штатная российская форма (normalize_phone),
# поэтому минимум — 10 знаков (ревью R5).
_PHONE_BARE_RE = re.compile(
    _REF_PREFIX + r"(\+?\d[\d\s()\-]{8,}\d)", re.IGNORECASE
)

# «К заявке 154», «к заявке №154», «к заявке номер: 154». Захватывается
# ПЕРВАЯ цифровая группа; STT-хвосты («123 456 789») подклеивает код,
# останавливаясь перед днём даты («154 23 июля» — это ID и дата).
_DEAL_NUM_RE = re.compile(
    _REF_PREFIX + r"(?:№\s*|номер[ау]?[\s.,:;—–-]*)?(\d{1,%d})(?!\d)"
    % MAX_DEAL_ID_DIGITS,
    re.IGNORECASE,
)

# Продолжение разбитого STT числа: короткая цифровая группа через пробел.
_NUM_GROUP_RE = re.compile(r"^\s+(\d{1,4})(?=[\s.,;:!?)]|$)")

# Самоисправление голосом без слова «заявка»: «…, нет, к 155», «нет — к
# 155», «нет. к 155». Числа, похожие на время («к 15:00», «к 15 вечера»)
# или дату («к 23 июля»), исправлением ПРИВЯЗКИ не считаются.
_CORRECTION_RE = re.compile(
    r"\bнет\b[\s,.:;—–-]*(?:лучше\s+)?(?:к|по|на)\s+"
    r"(?:заявк[а-яё]*[\s.,:;—–-]*)?"
    r"(?:№\s*)?\d{1,9}(?![:.,]?\d)(?!\s*(?:%s))" % _TIME_STOP_WORDS,
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
    if text.startswith("+"):
        digits = re.sub(r"\D", "", text)
        return "+" + digits if 10 <= len(digits) <= 15 else None
    return normalize_phone(text)


def _trim_phone_span(group: str, tail: str) -> tuple[str | None, int]:
    """Телефон из захваченной группы и длина реально потреблённой части.

    STT может приклеить к номеру следующую дату («89141234567 23 июля»,
    «+375291234567 23 июля»): если сразу за захватом идёт слово даты,
    последняя короткая цифровая группа — это день, он возвращается тексту.
    Для российских форм дополнительно работает штатная длина (11 цифр с
    8/7, 10 цифр с 9) — на случай приклеенного числа без слова даты.
    """
    end = len(group)
    if _DATE_WORD_RE.match(tail) or _NUMERIC_DATE_RE.match(tail):
        day = re.search(r"[\s.,;:()—–-]+\d{1,2}\s*$", group)
        if day is not None:
            end = day.start()
            group = group[:end]
    digits = re.sub(r"\D", "", group)
    limit = None
    if group.lstrip().startswith("+"):
        # Явный международный формат: длину страны угадывать нельзя,
        # обрезка только по маркерам даты выше (ревью ULTRA-3).
        limit = None
    elif len(digits) > 11 and digits[0] in "78":
        limit = 11
    elif len(digits) > 10 and digits[0] == "9":
        limit = 10
    if limit is not None:
        count = 0
        for index, char in enumerate(group):
            if char.isdigit():
                count += 1
                if count == limit:
                    end = index + 1
                    break
    return _normalize_ref_phone(group[:end]), end


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
    match = _PHONE_KEYWORD_RE.search(raw) or _PHONE_BARE_RE.search(raw)
    if match:
        phone, consumed = _trim_phone_span(match.group(1), raw[match.end(1) :])
        if phone is not None:
            end = match.start(1) + consumed
            return _cut(raw, match.start(), end), BindingRef("phone", phone)
    match = _DEAL_NUM_RE.search(raw)
    if match:
        digits = match.group(1)
        end = match.end(1)
        # Первая группа сама может быть номером, за которым идёт числовая
        # дата/время («154 15:00») — такие группы не подклеиваются.
        while True:
            nxt = _NUM_GROUP_RE.match(raw[end:])
            if nxt is None:
                break
            after = raw[end + nxt.end() :]
            if _DATE_WORD_RE.match(after) or _NUMERIC_DATE_RE.match(after):
                break
            digits += nxt.group(1)
            end += nxt.end()
        if len(digits) <= MAX_DEAL_ID_DIGITS:
            return _cut(raw, match.start(), end), BindingRef("deal_id", digits)
        # 10-15 цифр — это телефон, названный без слова «телефон».
        phone = _normalize_ref_phone(digits)
        if phone is not None:
            return _cut(raw, match.start(), end), BindingRef("phone", phone)
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
    if _CORRECTION_RE.search(clean):
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
    # с цифрами внутри — поисковый запрос, а не телефон.
    if extract_bare_phone(bare) is not None:
        phone = _normalize_ref_phone(bare)
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
