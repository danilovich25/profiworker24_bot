import telebot
from telebot import types
from datetime import datetime

TOKEN = "8854265598:AAFqwVOT_EHCqtV7XnzsTKbw-v93qm-WX1k"

bot = telebot.TeleBot(TOKEN)


# Стартовое меню
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

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
        "Клиент:\n"
        "Телефон:\n"
        "Услуга:\n"
        "Доход:\n"
        "Комментарий:"
    )


# Приём заявки
@bot.message_handler(func=lambda message: True)
def get_order(message):

    if message.text.startswith("/"):
        return

    date = datetime.now().strftime("%d.%m.%Y")

    answer = (
        "✅ Проверьте заявку:\n\n"
        f"Дата: {date}\n\n"
        f"{message.text}\n\n"
        "Сохранить заявку?"
    )

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
        answer,
        reply_markup=markup
    )


# Кнопка сохранить
@bot.callback_query_handler(func=lambda call: call.data == "save")
def save_order(call):

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "✅ Заявка сохранена.\n"
        "Следующий этап — подключение базы CRM."
    )


# Кнопка отмена
@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel_order(call):

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "❌ Заявка отменена."
    )


print("ProfiWorker24 Bot запущен")

bot.infinity_polling()
