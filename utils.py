import re

from datetime import datetime

# Дата заявки

def get_date():

    return datetime.now().strftime("%d.%m.%Y")

# Приведение телефона к единому виду

def normalize_phone(phone):

    if not phone:

        return "-"

    # оставляем только цифры

    digits = re.sub(r"\D", "", phone)

    # +7XXXXXXXXXX → 8XXXXXXXXXX

    if digits.startswith("7") and len(digits) == 11:

        digits = "8" + digits[1:]

    # 10 цифр без 8 или 7

    elif len(digits) == 10:

        digits = "8" + digits

    return digits

# Если поле пустое

def clean_text(text):

    if not text:

        return "-"

    text = text.strip()

    if text == "":

        return "-"

    return text

# Поиск телефона внутри текста заявки

def extract_phone(text):

    if not text:

        return "-"

    numbers = re.findall(

        r"(?:\+7|8|7)?[\s\-]?\d{3}[\s\-]?\d{2,3}[\s\-]?\d{2,3}",

        text

    )

    if numbers:

        return normalize_phone(numbers[0])

    return "-"
