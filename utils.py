import re

from datetime import datetime

# Дата заявки

def get_date():

    return datetime.now().strftime("%d.%m.%Y")

# Приведение телефона к единому виду

def normalize_phone(phone):

    if not phone:

        return "-"

    # Оставляем только цифры

    digits = re.sub(r"\D", "", phone)

    # +7XXXXXXXXXX → 8XXXXXXXXXX

    if len(digits) == 11:

        if digits.startswith("7"):

            digits = "8" + digits[1:]

        elif digits.startswith("8"):

            pass

        else:

            return "-"

    # Если ввели только 10 цифр

    elif len(digits) == 10:

        digits = "8" + digits

    else:

        return "-"

    return digits

# Проверка пустого текста

def clean_text(text):

    if not text:

        return "-"

    text = text.strip()

    if text == "":

        return "-"

    return text

# Поиск телефона внутри текста

def extract_phone(text):

    if not text:

        return "-"

    phones = re.findall(

        r"(?:\+7|8|7)?[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2}",

        text

    )

    if not phones:

        return "-"

    return normalize_phone(phones[0])
