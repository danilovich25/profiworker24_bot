import telebot
from telebot import types
from datetime import datetime


# ВСТАВЬ СВОЙ ТОКЕН СЮДА
TOKEN = "8854265598:AAGPVTMw3zJ_QCOaQIP5cP8Gpnh-bM07ilI"


bot = telebot.TeleBot(TOKEN)


# Пользователи, которые сейчас создают заявку
waiting_orders = set()


# Старт
@bot.message_handler(commands=["start"])
def start(message):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    btn_new = types.KeyboardButton("🆕 Новая заявка")
    btn_search = types.KeyboardButton("🔎 Найти заявку")
    btn_status = types.KeyboardButton("✏️ Изменить статус")

    markup.add(btn_new, btn_search, btn_status)

    bot.send_message(
        message.chat.id,
        "👷 ProfiWorker24 Manager запущен.\n\n"
        "Выберите действие:",
        reply_markup=markup
    )


# Запуск новой заявки
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


# Получение заявки только после кнопки "Новая заявка"
@bot.message_handler(func=lambda message: message.chat.id in waiting_orders)
def receive_order(message):

    date = datetime.now().strftime("%d.%m.%Y")

    order = (
        "✅ Проверьте заявку:\n\n"
        f"Дата: {date}\n\n"
        f"{message.text}\n\n"
        "Сохранить?"
    )

    markup = types.InlineKeyboardMarkup()

    save = types.InlineKeyboardButton(
        "✅ Сохранить",
        callback_data="save"
    )

    cancel = types.InlineKeyboardButton(
        "❌ Отмена",
        callback_data="cancel"
    )

    markup.add(save, cancel)

    bot.send_message(
        message.chat.id,
        order,
        reply_markup=markup
    )


# Сохранение
@bot.callback_query_handler(func=lambda call: call.data == "save")
def save_order(call):

    waiting_orders.discard(call.message.chat.id)

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "✅ Заявка сохранена.\n\n"
        "Следующий этап — подключение базы CRM."
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


# Заглушки будущих функций
@bot.message_handler(func=lambda message: message.text == "🔎 Найти заявку")
def search_order(message):

    bot.send_message(
        message.chat.id,
        "🔎 Поиск заявок подключим следующим этапом."
    )


@bot.message_handler(func=lambda message: message.text == "✏️ Изменить статус")
def change_status(message):

    bot.send_message(
        message.chat.id,
        "✏️ Изменение статуса подключим следующим этапом."
    )


print("ProfiWorker24 Bot запущен")


bot.infinity_polling()
