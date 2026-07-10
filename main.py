import telebot
from telebot import types
from datetime import datetime


# ВСТАВЬ НОВЫЙ ТОКЕН В КАВЫЧКАХ
TOKEN = "8854265598:AAGPVTMw3zJ_QCOaQIP5cP8Gpnh-bM07ilI"


bot = telebot.TeleBot(TOKEN)


# Пользователи, которые начали создание заявки
waiting_orders = set()


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

    waiting_orders.add(message.chat.id)

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


# Получение заявки только после нажатия "Новая заявка"
@bot.message_handler(func=lambda message: message.chat.id in waiting_orders)
def get_order(message):

    date = datetime.now().strftime("%d.%m.%Y")

    order_text = (
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
        order_text,
        reply_markup=markup
    )


# Сохранение заявки
@bot.callback_query_handler(func=lambda call: call.data == "save")
def save_order(call):

    waiting_orders.discard(call.message.chat.id)

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "✅ Заявка сохранена.\n\n"
        "Следующий этап — подключение CRM."
    )


# Отмена
@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel_order(call):

    waiting_orders.discard(call.message.chat.id)

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "❌ Заявка отменена."
    )


# Поиск заявки (пока заглушка)
@bot.message_handler(func=lambda message: message.text == "🔎 Найти заявку")
def search_order(message):

    bot.send_message(
        message.chat.id,
        "🔎 Поиск подключим следующим этапом."
    )


# Изменение статуса (пока заглушка)
@bot.message_handler(func=lambda message: message.text == "✏️ Изменить статус")
def change_status(message):

    bot.send_message(
        message.chat.id,
        "✏️ Изменение статуса подключим следующим этапом."
    )


print("ProfiWorker24 Bot запущен")

bot.infinity_polling()
