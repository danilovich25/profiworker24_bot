import telebot
from telebot import types
import database


TOKEN = "8854265598:AAGPVTMw3zJ_QCOaQIP5cP8Gpnh-bM07ilI"

bot = telebot.TeleBot(TOKEN)


# создаём базу при запуске
database.create_database()


# временное хранение заявки до подтверждения
user_orders = {}


# Главное меню
@bot.message_handler(commands=["start"])
def start(message):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    btn1 = types.KeyboardButton("🆕 Новая заявка")
    btn2 = types.KeyboardButton("🔎 Найти заявку")

    markup.add(btn1, btn2)

    bot.send_message(
        message.chat.id,
        "👷 ProfiWorker24 Manager запущен.\n\nВыберите действие:",
        reply_markup=markup
    )


# Новая заявка
@bot.message_handler(func=lambda m: m.text == "🆕 Новая заявка")
def new_order(message):

    user_orders[message.chat.id] = True

    bot.send_message(
        message.chat.id,
        "Введите заявку по шаблону:\n\n"
        "Имя:\n"
        "Организация:\n"
        "Телефон:\n"
        "Услуга:\n"
        "Статус:\n"
        "Доход:\n"
        "Комментарий:"
    )


# Получение текста заявки
@bot.message_handler(
    func=lambda m: m.chat.id in user_orders
)
def get_order(message):

    if message.text == "🆕 Новая заявка":
        return


    user_orders[message.chat.id] = message.text


    markup = types.InlineKeyboardMarkup()

    yes = types.InlineKeyboardButton(
        "✅ Сохранить",
        callback_data="save"
    )

    no = types.InlineKeyboardButton(
        "❌ Отмена",
        callback_data="cancel"
    )

    markup.add(yes, no)


    bot.send_message(
        message.chat.id,
        "Проверьте заявку:\n\n"
        f"{message.text}\n\n"
        "Сохранить?",
        reply_markup=markup
    )


# Сохранение заявки
@bot.callback_query_handler(
    func=lambda call: call.data == "save"
)
def save_order(call):

    text = user_orders.get(call.message.chat.id)


    if not text:
        return


    # пока сохраняем весь текст как услугу
    # дальше сделаем автоматическое разделение полей

    database.save_order(
        name="-",
        organization="-",
        phone="-",
        service=text,
        status="Новая",
        income="-",
        comment="-"
    )


    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "✅ Заявка сохранена в базе."
    )


    del user_orders[call.message.chat.id]



# Отмена
@bot.callback_query_handler(
    func=lambda call: call.data == "cancel"
)
def cancel_order(call):

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "❌ Заявка отменена."
    )


    if call.message.chat.id in user_orders:
        del user_orders[call.message.chat.id]



# Поиск заявки
@bot.message_handler(
    func=lambda m: m.text == "🔎 Найти заявку"
)
def search_start(message):

    bot.send_message(
        message.chat.id,
        "Введите телефон клиента:"
    )


# Получение телефона для поиска
@bot.message_handler(
    func=lambda m: m.text.isdigit()
)
def search_phone(message):

    orders = database.find_order(
        message.text
    )


    if not orders:

        bot.send_message(
            message.chat.id,
            "❌ Заявок с таким номером нет."
        )

        return


    result = "🔎 Найдена заявка:\n\n"


    for order in orders:

        result += (
            f"№{order[0]}\n"
            f"Дата: {order[1]}\n"
            f"Телефон: {order[4]}\n"
            f"Услуга: {order[5]}\n"
            f"Статус: {order[6]}\n"
            f"Доход: {order[7]}\n\n"
        )


    bot.send_message(
        message.chat.id,
        result
    )



print("ProfiWorker24 Bot запущен")

bot.infinity_polling()
