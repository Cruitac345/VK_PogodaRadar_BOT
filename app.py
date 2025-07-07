import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from vk_bot import bot, start_bot

# Создаем lifespan manager для запуска бота
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем бота в фоновом режиме
    task = asyncio.create_task(start_bot())  # Используем правильную функцию!
    yield
    # Останавливаем бота при завершении
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("Бот остановлен")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    return {"status": "VK Bot is running!"}

@app.get("/health")
async def health():
    return {"status": "OK"}
