# profiworker24_botimport telebot

TOKEN=8854265598:AAFqwVOT_EHCqtV7XnzsTKbw-v93qm-WX1k

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "👷 ProfiWorker24 Manager запущен!\n\nВыберите действие:\n\n🆕 Новая заявка\n🔎 Найти заявку"
    )

bot.infinity_polling()
