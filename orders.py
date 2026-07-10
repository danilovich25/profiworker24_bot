import database

from utils import normalize_phone, clean_text, extract_phone

# Разбор заявки из текста

def parse_order(text):

    data = {

        "name": "-",

        "organization": "-",

        "phone": "-",

        "service": "-",

        "status": "Новая",

        "income": 0,

        "comment": "-"

    }

    lines = text.split("\n")

    for line in lines:

        line = line.strip()

        if ":" not in line:

            continue

        key, value = line.split(":", 1)

        key = key.lower().strip()

        value = value.strip()

        if "имя" in key:

            data["name"] = clean_text(value)

        elif "орган" in key:

            data["organization"] = clean_text(value)

        elif "тел" in key:

            data["phone"] = normalize_phone(value)

        elif "услуг" in key:

            data["service"] = clean_text(value)

        elif "статус" in key:

            data["status"] = clean_text(value)

        elif "доход" in key:

            numbers = "".join(

                x for x in value if x.isdigit()

            )

            if numbers:

                data["income"] = int(numbers)

        elif "коммент" in key:

            data["comment"] = clean_text(value)

    # Если телефон не нашли в поле —

    # пробуем найти его во всём тексте

    if data["phone"] == "-":

        data["phone"] = extract_phone(text)

    return data

# Сохранение заявки

def create_order(text):

    order = parse_order(text)

    database.save_order(

        order["name"],

        order["organization"],

        order["phone"],

        order["service"],

        order["status"],

        order["income"],

        order["comment"]

    )

    return order

# Формат вывода заявки

def format_order(order):

    return (

        f"👤 Имя: {order['name']}\n"

        f"🏢 Организация: {order['organization']}\n"

        f"📞 Телефон: {order['phone']}\n"

        f"🔧 Услуга: {order['service']}\n"

        f"📌 Статус: {order['status']}\n"

        f"💰 Доход: {order['income']}\n"

        f"💬 Комментарий: {order['comment']}"

    )
