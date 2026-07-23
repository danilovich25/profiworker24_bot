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
# которую вставляет STT («к заявке: 154», «по заявке 154»; ревью ULTRA).
_REF_PREFIX = r"\b(?:к|по|для)\s+заявк[а-яё]*[\s.,:;-]*"

# «К заявке по телефону 8914…» — телефон назван явно.
_PHONE_KEYWORD_RE = re.compile(
    _REF_PREFIX + r"(?:по\s+)?(?:телефону?|номеру\s+телефона)[\s.,:;-]*"
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

# «К заявке 154», «к заявке №154», «к заявке номер: 154». Цифровой блок
# может содержать STT-пробелы («123 456 789» — это один номер, не первый
# его кусок; ревью ULTRA), поэтому захватывается целиком и сжимается кодом.
_DEAL_NUM_RE = re.compile(
    _REF_PREFIX + r"(?:№\s*|номер[ау]?[\s.,:;-]*)?(\d(?:[ \d]{0,18}\d)?)(?!\d)",
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
# название, а не «к» + номер 12 (ревью ULTRA).
_SERVICE_WORD_RE = re.compile(
    r"^(?:(?:к|по|для)(?=\s)"
    r"|(?:заявк(?:а|и|е|у|ой|ою)?"
    r"|номер(?:а|у|е|ом)?"
    r"|телефон(?:а|у|е|ом)?"
    r")(?=[\s.,:;№\d-]|$)|№)",
    re.IGNORECASE,
)

# Пунктуация-разделитель между служебными словами и идентификатором.
_SERVICE_SEP = " \t.,:;-—"


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


def _trim_phone_span(group: str) -> tuple[str | None, int]:
    """Телефон из захваченной группы и длина реально потреблённой части.

    STT может приклеить к номеру следующую дату («89141234567 23 июля»):
    для российских форм берётся штатная длина (11 цифр с 8/7, 10 цифр с 9),
    остаток остаётся тексту напоминания (ревью ULTRA).
    """
    digits = re.sub(r"\D", "", group)
    limit = None
    if len(digits) > 11 and digits[0] in "78":
        limit = 11
    elif len(digits) > 10 and digits[0] == "9":
        limit = 10
    if limit is None:
        return normalize_phone(group), len(group)
    count = 0
    end = len(group)
    for index, char in enumerate(group):
        if char.isdigit():
            count += 1
            if count == limit:
                end = index + 1
                break
    return normalize_phone(group[:end]), end


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
        phone, consumed = _trim_phone_span(match.group(1))
        if phone is not None:
            end = match.start(1) + consumed
            return _cut(raw, match.start(), end), BindingRef("phone", phone)
    match = _DEAL_NUM_RE.search(raw)
    if match:
        compact = re.sub(r"\s", "", match.group(1))
        if len(compact) <= MAX_DEAL_ID_DIGITS:
            return _cut(raw, match.start(), match.end()), BindingRef(
                "deal_id", compact
            )
        # 10-15 цифр — это телефон, названный без слова «телефон».
        phone = normalize_phone(compact)
        if phone is not None:
            return _cut(raw, match.start(), match.end()), BindingRef("phone", phone)
    return raw, None


def extract_inline_binding(text: str) -> tuple[str, BindingRef | None]:
    """Находит привязку прямо в тексте напоминания.

    Возвращает (текст без фразы привязки, ссылка | None). None — привязка
    не названа, нужно спросить. Распознаются только однозначные формы
    (последняя, номер, телефон, «обычное») — свободное название заявки в
    потоке текста не угадывается, чтобы не съесть само напоминание.

    Две РАЗНЫЕ ссылки в одном сообщении («к заявке 154, нет, к заявке
    155») — kind="conflict": самоисправление не угадывается, выбор задаёт
    явный вопрос (ревью ULTRA). Повтор ОДНОЙ И ТОЙ ЖЕ ссылки конфликтом
    не считается.
    """
    clean, ref = _extract_one(text)
    if ref is None:
        return clean, None
    remainder, second = _extract_one(clean)
    if second is not None and (second.kind, second.value) != (ref.kind, ref.value):
        return (text or "").strip(), BindingRef("conflict")
    if second is not None:
        # Дубль той же ссылки: вычищаем и его, текст короче и честнее.
        clean = remainder
    return clean, ref


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
    phone = extract_bare_phone(bare)
    if phone is not None:
        return BindingRef("phone", phone)
    # Текстовый поиск идёт по ядру без служебного префикса: «к заявке
    # Ромашка» ищется как «Ромашка» — общий поиск таких слов не знает и с
    # префиксом отвечал бы «не нашёл» (ревью R5). Подстрочный LIKE находит
    # ядро и внутри полного названия, потеря префикса безопасна.
    return BindingRef("text", bare if len(bare) >= MIN_QUERY_LEN else raw)
