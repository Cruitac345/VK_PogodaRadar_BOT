import threading
import asyncio
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
    """Запуск VK бота в отдельном потоке с созданием нового event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_bot())  # предполагается, что start_bot() - асинхронная функция
    except Exception as e:
        print(f"Ошибка в боте: {e}")
    finally:
        loop.close()

# Запускаем бота в фоновом потоке
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)  # Для Render используйте порт 10000
