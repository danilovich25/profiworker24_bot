"""Сроки заявок: часовой пояс, детерминированный разбор и формат дд.мм.гггг чч:мм.

Модель получает текущее время в промпте и возвращает срок в ISO, но
арифметику относительных дат («через 5 дней») она может посчитать неверно.
Поэтому поверх её ответа работает детерминированная подстраховка: явные
обороты в самом сообщении («завтра», «послезавтра», «через 3 дня», «через
неделю в 10:00», «24.07 в 10:00») пересчитываются кодом от текущего времени
и имеют приоритет над ответом модели.

Часовой пояс один на всё приложение — settings.tz (по умолчанию
Asia/Vladivostok): от него считаются относительные даты, напоминания и
отображение. «Сейчас» функции принимают параметром, поэтому тесты фиксируют
момент и не зависят от времени запуска.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

# Час Telegram-напоминания для сроков без времени («завтра»): утро рабочего дня.
DEFAULT_REMINDER_HOUR = 9

_WORD_NUMBERS = {
    "один": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "пару": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}

_NUM = r"(\d{1,3}|" + "|".join(_WORD_NUMBERS) + r")"

_IN_DAYS_RE = re.compile(rf"\bчерез\s+{_NUM}\s+(?:день|дня|дней)\b")
_IN_WEEKS_RE = re.compile(rf"\bчерез\s+{_NUM}\s+недел\w*\b")
_IN_ONE_DAY_RE = re.compile(r"\bчерез\s+день\b")
_IN_ONE_WEEK_RE = re.compile(r"\bчерез\s+неделю\b")
_IN_HOURS_RE = re.compile(rf"\bчерез\s+{_NUM}\s+час\w*\b")
_IN_ONE_HOUR_RE = re.compile(r"\bчерез\s+час\b")
_IN_HALF_HOUR_RE = re.compile(r"\bчерез\s+полчаса\b")
_IN_MINUTES_RE = re.compile(rf"\bчерез\s+{_NUM}\s+минут\w*\b")

_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}
_WEEKDAY_RE = re.compile(
    r"\bво?\s+(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресенье)\b"
)

# Явная дата с точкой: «24.07», «24.07.2026». Числа без точки («Ленина 24»)
# датой не считаются.
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{2})(?:\.(\d{4}))?\b")

# Время принимается только в однозначной форме: с минутами («в 10:00»),
# со словом «час…» («к 9 часам») или с уточнением суток («в 9 утра»).
# Голое «в 10» временем не считается — это может быть «в 10 метрах».
_TIME_RE = re.compile(
    r"\b(?:в|к|до)\s*(\d{1,2})"
    r"(?::(\d{2})|\s*(час(?:а|ов|ам)?\b)|\s*(утра|вечера|дня|ночи)\b"
    r"|(?::(\d{2}))?\s*(?:час(?:а|ов|ам)?\s*)?(утра|вечера|дня|ночи)\b)"
)


def local_tz() -> ZoneInfo:
    """Часовой пояс приложения (одна настройка TZ, без «+10» по коду)."""
    return ZoneInfo(settings.tz)


def now_local() -> datetime:
    """Текущее время в часовом поясе приложения."""
    return datetime.now(local_tz())


def _word_to_int(raw: str) -> int:
    return int(raw) if raw.isdigit() else _WORD_NUMBERS[raw]


def _extract_time(text: str) -> tuple[int, int] | None:
    """(час, минута) из текста или None, если время не названо однозначно."""
    match = _TIME_RE.search(text)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or match.group(5) or 0)
    qualifier = match.group(4) or match.group(6)
    if qualifier in ("вечера", "дня") and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _extract_date(text: str, now: datetime) -> date | None:
    """Календарный день из текста (без времени) или None."""
    match = _DATE_RE.search(text)
    if match is not None:
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else now.year
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if match.group(3) is None and found < now.date():
            # «15.01» в июле — про следующий январь, а не про прошедший.
            found = date(year + 1, month, day)
        return found

    match = _IN_DAYS_RE.search(text)
    if match is not None:
        return now.date() + timedelta(days=_word_to_int(match.group(1)))
    match = _IN_WEEKS_RE.search(text)
    if match is not None:
        return now.date() + timedelta(weeks=_word_to_int(match.group(1)))
    if _IN_ONE_WEEK_RE.search(text):
        return now.date() + timedelta(weeks=1)
    if _IN_ONE_DAY_RE.search(text):
        return now.date() + timedelta(days=1)
    if re.search(r"\bпослезавтра\b", text):
        return now.date() + timedelta(days=2)
    if re.search(r"\bзавтра\b", text):
        return now.date() + timedelta(days=1)
    if re.search(r"\bсегодня\b", text):
        return now.date()
    match = _WEEKDAY_RE.search(text)
    if match is not None:
        target = _WEEKDAYS[match.group(1)]
        ahead = (target - now.weekday()) % 7
        # «В воскресенье», сказанное в воскресенье, — про следующее.
        return now.date() + timedelta(days=ahead or 7)
    return None


def parse_human_date(text: str | None, now: datetime) -> str | None:
    """Срок из свободного текста в ISO или None, если срока в тексте нет.

    Возвращает полный ISO с зоной («2026-07-24T10:00:00+10:00»), если время
    названо, и дату без времени («2026-07-24»), если назван только день:
    придумывать время (например «00:00») нельзя — оно попало бы в карточку.
    """
    lowered = (text or "").lower().replace("ё", "е")
    if not lowered.strip():
        return None
    tz = now.tzinfo or local_tz()

    # Смещения от «сейчас» с точностью до минут — сразу полный момент.
    for regex, unit in ((_IN_HOURS_RE, 3600), (_IN_MINUTES_RE, 60)):
        match = regex.search(lowered)
        if match is not None:
            moment = now + timedelta(seconds=_word_to_int(match.group(1)) * unit)
            return moment.replace(second=0, microsecond=0).isoformat()
    if _IN_HALF_HOUR_RE.search(lowered):
        return (now + timedelta(minutes=30)).replace(second=0, microsecond=0).isoformat()
    if _IN_ONE_HOUR_RE.search(lowered) and not _IN_HOURS_RE.search(lowered):
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0).isoformat()

    day = _extract_date(lowered, now)
    time_part = _extract_time(lowered)
    if day is None and time_part is None:
        return None
    if time_part is None:
        return day.isoformat()
    hour, minute = time_part
    if day is None:
        # Названо только время: до конца дня — сегодня, прошедшее — завтра.
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment <= now:
            moment += timedelta(days=1)
        return moment.isoformat()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).isoformat()


def _valid_iso(raw: str) -> bool:
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        return False
    return True


def resolve_deadline(llm_deadline: str | None, text: str, now: datetime) -> str | None:
    """Итоговый срок заявки: детерминированный разбор важнее ответа модели.

    Порядок:
    1. явный срок в самом сообщении («через 5 дней в 10:00») — пересчёт кодом;
    2. если код уверен только в дне (время не названо цифрами), а модель
       вернула полный момент — берётся день из кода и время модели: она
       понимает словесное время («в десять утра»), а день считает код;
    3. валидный ISO от модели — как есть;
    4. не-ISO ответ модели («завтра») — детерминированный разбор этого текста;
    5. совсем непонятный срок сохраняется как есть: он уйдёт в комментарий
       сделки, а не потеряется молча.
    """
    cleaned = (llm_deadline or "").strip()
    from_text = parse_human_date(text, now)
    if from_text is not None:
        if "T" in from_text:
            return from_text
        if cleaned and "T" in cleaned and _valid_iso(cleaned):
            llm_moment = datetime.fromisoformat(cleaned)
            if (llm_moment.hour, llm_moment.minute) == (now.hour, now.minute):
                # Время «как сейчас» с точностью до минуты — не названный
                # срок, а копия текущего времени из промпта (живая модель
                # так отвечает на голое «завтра»): день без времени честнее.
                return from_text
            day = date.fromisoformat(from_text)
            combined = llm_moment.replace(year=day.year, month=day.month, day=day.day)
            if combined.tzinfo is None:
                combined = combined.replace(tzinfo=now.tzinfo or local_tz())
            return combined.isoformat()
        return from_text
    if not cleaned:
        return None
    if _valid_iso(cleaned):
        return cleaned
    return parse_human_date(cleaned, now) or cleaned


def format_deadline(raw: str | None) -> str | None:
    """Срок для показа: «24.07.2026 10:00», «24.07.2026» или исходный текст."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        parsed = datetime.fromisoformat(cleaned)
        return parsed.strftime("%d.%m.%Y")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return raw
    return parsed.strftime("%d.%m.%Y %H:%M")


def format_bitrix_datetime(raw: object) -> str:
    """Дата-время Bitrix («2026-07-18T10:00:00+03:00») → «18.07.2026 20:00».

    Время с зоной переводится в часовой пояс приложения: сотрудник видит
    своё, владивостокское время, независимо от зоны портала.
    """
    text = str(raw or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(local_tz())
    return parsed.strftime("%d.%m.%Y %H:%M")


def epoch_to_iso(ts: int) -> str:
    """Unix-момент → ISO в часовом поясе приложения (для полей Bitrix)."""
    return datetime.fromtimestamp(ts, local_tz()).isoformat()


def reminder_epoch(deadline_iso: str | None) -> int | None:
    """Unix-момент Telegram-напоминания по сроку заявки.

    Срок без времени («завтра») напоминает утром в DEFAULT_REMINDER_HOUR по
    местному времени; ISO без зоны считается местным временем приложения.
    Непонятный срок напоминания не получает (None).
    """
    cleaned = (deadline_iso or "").strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        parsed = parsed.replace(hour=DEFAULT_REMINDER_HOUR)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz())
    return int(parsed.timestamp())
