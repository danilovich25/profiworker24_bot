import telebot
from telebot import types

import database
import orders

from config import TOKEN


bot = telebot.TeleBot(TOKEN)


# Создание базы при запуске
database.create_database()


# Временное хранение заявок до подтверждения
waiting_orders = {}

# Режим поиска
search_mode = {}



# =========================
# СТАРТ
# =========================

@bot.message_handler(commands=["start"])
def start(message):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    btn1 = types.KeyboardButton("🆕 Новая заявка")
    btn2 = types.KeyboardButton("🔎 Найти заявку")
    btn3 = types.KeyboardButton("📋 Последние заявки")

    markup.add(btn1, btn2, btn3)


    bot.send_message(
        message.chat.id,
        "👷 ProfiWorker24 Manager v1.0 запущен.\n\n"
        "Выберите действие:",
        reply_markup=markup
    )



# =========================
# НОВАЯ ЗАЯВКА
# =========================

@bot.message_handler(
    func=lambda m: m.text == "🆕 Новая заявка"
)
def new_order(message):

    bot.send_message(
        message.chat.id,
        "Введите заявку:\n\n"

        "Имя:\n"
        "Организация:\n"
        "Телефон:\n"
        "Услуга:\n"
        "Статус:\n"
        "Доход:\n"
        "Комментарий:"
    )


    waiting_orders[message.chat.id] = True



# Получение заявки

@bot.message_handler(
    func=lambda m: waiting_orders.get(m.chat.id) == True
)
def receive_order(message):

    waiting_orders[message.chat.id] = message.text


    parsed = orders.parse_order(message.text)


    preview = orders.format_order(parsed)


    markup = types.InlineKeyboardMarkup()


    yes = types.InlineKeyboardButton(
        "✅ Сохранить",
        callback_data="save_order"
    )


    no = types.InlineKeyboardButton(
        "❌ Отмена",
        callback_data="cancel_order"
    )


    markup.add(yes, no)



    bot.send_message(
        message.chat.id,
        "Проверьте заявку:\n\n"
        + preview,

        reply_markup=markup
    )



# =========================
# СОХРАНЕНИЕ
# =========================

@bot.callback_query_handler(
    func=lambda c: c.data == "save_order"
)
def save_order(call):

    text = waiting_orders.get(
        call.message.chat.id
    )


    if text:

        orders.create_order(text)


        bot.answer_callback_query(call.id)


        bot.send_message(
            call.message.chat.id,
            "✅ Заявка сохранена."
        )


        del waiting_orders[
            call.message.chat.id
        ]



# Отмена

@bot.callback_query_handler(
    func=lambda c: c.data == "cancel_order"
)
def cancel_order(call):

    bot.answer_callback_query(call.id)


    bot.send_message(
        call.message.chat.id,
        "❌ Заявка отменена."
    )


    if call.message.chat.id in waiting_orders:

        del waiting_orders[
            call.message.chat.id
        ]



# =========================
# ПОИСК
# =========================

@bot.message_handler(
    func=lambda m: m.text == "🔎 Найти заявку"
)
def start_search(message):

    search_mode[message.chat.id] = True


    bot.send_message(
        message.chat.id,
        "Введите телефон, имя или организацию:"
    )



@bot.message_handler(
    func=lambda m: search_mode.get(m.chat.id) == True
)
def search_order(message):

    search_mode[message.chat.id] = False


    query = message.text


    result = database.search_text(query)



    if not result:

        bot.send_message(
            message.chat.id,
            "❌ Ничего не найдено."
        )

        return



    answer = "🔎 Найдено:\n\n"



    for item in result:

        answer += (

            f"№{item[0]}\n"
            f"Дата: {item[1]}\n"
            f"Имя: {item[2]}\n"
            f"Организация: {item[3]}\n"
            f"Телефон: {item[4]}\n"
            f"Услуга: {item[5]}\n"
            f"Статус: {item[6]}\n"
            f"Доход: {item[7]}\n"
            f"Комментарий: {item[8]}\n\n"

        )


    bot.send_message(
        message.chat.id,
        answer
    )



# =========================
# ПОСЛЕДНИЕ ЗАЯВКИ
# =========================

@bot.message_handler(
    func=lambda m: m.text == "📋 Последние заявки"
)
def last_orders(message):

    data = database.get_statistics()


    bot.send_message(
        message.chat.id,

        "📊 Статистика:\n\n"
        f"Всего заявок: {data[0]}\n"
        f"Общий доход: {data[1] or 0}"
    )



print("ProfiWorker24 Manager v1.0 запущен")


bot.infinity_polling()
