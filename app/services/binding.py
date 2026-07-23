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

# «К заявке по телефону 8914…» — телефон назван явно.
_PHONE_KEYWORD_RE = re.compile(
    r"\bк\s+заявк\w*\s+(?:по\s+)?(?:телефону?|номеру\s+телефона)\s*[:\s]"
    r"\s*(\+?\d[\d\s()\-]{4,}\d)",
    re.IGNORECASE,
)

# «К заявке 89141234567» — длинная цифровая строка после «к заявке» это
# телефон, а не номер сделки (тот короче MAX_DEAL_ID_DIGITS).
_PHONE_BARE_RE = re.compile(
    r"\bк\s+заявк\w*\s+(\+?\d[\d\s()\-]{9,}\d)", re.IGNORECASE
)

# «К заявке 154», «к заявке №154», «к заявке номер 154».
_DEAL_NUM_RE = re.compile(
    r"\bк\s+заявк\w*\s+(?:№\s*|номер[ау]?\s+)?(\d{1,%d})\b" % MAX_DEAL_ID_DIGITS,
    re.IGNORECASE,
)

# Ответ на вопрос «к какой заявке?»: отказ от привязки.
_ANSWER_NONE_RE = re.compile(
    r"^(?:без\s+привязк\w*|обычн\w*(?:\s+напоминани\w*)?|нет|не\s+надо|не\s+нужно)[.!]?$",
    re.IGNORECASE,
)

_ANSWER_LAST_RE = re.compile(r"последн", re.IGNORECASE)

# Служебные слова в ответе перед самим идентификатором: «к заявке 154»,
# «заявка №154», «по телефону 8914…» — отбрасываются перед разбором.
_ANSWER_PREFIX_RE = re.compile(
    r"^(?:к\s+)?(?:заявк\w*\s+)?(?:по\s+)?(?:телефону?\s+|номер[ау]?\s+|№\s*)?",
    re.IGNORECASE,
)


def _cut(text: str, start: int, end: int) -> str:
    """Вырезает совпадение и схлопывает оставшиеся пробелы."""
    rest = (text[:start] + " " + text[end:]).strip()
    return re.sub(r"\s+", " ", rest)


def extract_inline_binding(text: str) -> tuple[str, BindingRef | None]:
    """Находит привязку прямо в тексте напоминания.

    Возвращает (текст без фразы привязки, ссылка | None). None — привязка
    не названа, нужно спросить. Распознаются только однозначные формы
    (последняя, номер, телефон, «обычное») — свободное название заявки в
    потоке текста не угадывается, чтобы не съесть само напоминание.
    """
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
        phone = normalize_phone(match.group(1))
        if phone is not None:
            return _cut(raw, match.start(), match.end()), BindingRef("phone", phone)
    match = _DEAL_NUM_RE.search(raw)
    if match:
        return _cut(raw, match.start(), match.end()), BindingRef(
            "deal_id", match.group(1)
        )
    return raw, None


def parse_binding_answer(text: str) -> BindingRef:
    """Разбирает ответ на вопрос «к какой заявке привязать?».

    Любой не распознанный явно ответ — поисковый запрос (название,
    организация, имя клиента): решает то же ядро, что и «Найти».
    """
    raw = (text or "").strip()
    if _ANSWER_NONE_RE.match(raw):
        return BindingRef("none")
    if _ANSWER_LAST_RE.search(raw):
        return BindingRef("last")
    bare = _ANSWER_PREFIX_RE.sub("", raw).strip()
    compact = re.sub(r"[\s()\-]", "", bare)
    if compact.isdecimal() and len(compact) <= MAX_DEAL_ID_DIGITS:
        return BindingRef("deal_id", compact)
    # Телефоном считается только ответ, ЦЕЛИКОМ являющийся номером: фраза
    # с цифрами внутри — поисковый запрос, а не телефон.
    phone = extract_bare_phone(bare)
    if phone is not None:
        return BindingRef("phone", phone)
    return BindingRef("text", raw)
