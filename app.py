import threading
from flask import Flask
from vk_bot import start_bot  # импортируйте функцию запуска бота

app = Flask(__name__)

@app.route('/')
def home():
    return "VK Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

def run_bot():
    """Запуск VK бота в отдельном потоке"""
    start_bot()  # ваша функция запуска бота

# Запускаем бота в фоновом потоке
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
