import telebot
from telebot import types
from datetime import datetime
import database


TOKEN = "8854265598:AAGPVTMw3zJ_QCOaQIP5cP8Gpnh-bM07ilI"


bot = telebot.TeleBot(TOKEN)


# Создаем базу при запуске
database.create_database()


# Главное меню
@bot.message_handler(commands=["start"])
def start(message):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    btn1 = types.KeyboardButton("🆕 Новая заявка")
    btn2 = types.KeyboardButton("🔎 Найти заявку")
    btn3 = types.KeyboardButton("✏️ Изменить статус")

    markup.add(btn1, btn2, btn3)

    bot.send_message(
        message.chat.id,
        "👷 ProfiWorker24 Manager запущен.\n\n"
        "Выберите действие:",
        reply_markup=markup
    )


# Новая заявка
@bot.message_handler(func=lambda message: message.text == "🆕 Новая заявка")
def new_order(message):

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


# Состояние ожидания заявки
user_orders = {}


@bot.message_handler(func=lambda message: message.chat.id in user_orders)
def get_order(message):

    date = datetime.now().strftime("%d.%m.%Y")

    user_orders[message.chat.id] = message.text

    text = (
        "✅ Проверьте заявку:\n\n"
        f"Дата: {date}\n\n"
        f"{message.text}\n\n"
        "Сохранить заявку?"
    )

    markup = types.InlineKeyboardMarkup()

    btn_yes = types.InlineKeyboardButton(
        "✅ Сохранить",
        callback_data="save"
    )

    btn_no = types.InlineKeyboardButton(
        "❌ Отмена",
        callback_data="cancel"
    )

    markup.add(btn_yes, btn_no)

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=markup
    )


# Запуск режима новой заявки
@bot.message_handler(func=lambda message: message.text == "🆕 Новая заявка")
def start_order(message):

    user_orders[message.chat.id] = True

    bot.send_message(
        message.chat.id,
        "Введите данные заявки:"
    )


# Сохранение
@bot.callback_query_handler(func=lambda call: call.data == "save")
def save_order(call):

    text = user_orders.get(call.message.chat.id)

    if text:

        database.save_order(
            name="",
            organization="",
            phone="",
            service=text,
            status="Новая",
            income=0,
            comment=""
        )

        bot.answer_callback_query(call.id)

        bot.send_message(
            call.message.chat.id,
            "✅ Заявка сохранена в базе.\n\n"
            "Следующий этап — поиск и CRM."
        )

        del user_orders[call.message.chat.id]

    else:

        bot.send_message(
            call.message.chat.id,
            "Нет активной заявки."
        )


# Отмена
@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel_order(call):

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "❌ Заявка отменена."
    )

    if call.message.chat.id in user_orders:
        del user_orders[call.message.chat.id]


# Поиск заявки
@bot.message_handler(func=lambda message: message.text == "🔎 Найти заявку")
def search_order(message):

    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента:"
    )


print("ProfiWorker24 Bot запущен")

bot.infinity_polling()
