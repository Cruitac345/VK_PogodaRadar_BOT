import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from vk_bot import bot

# Создаем lifespan manager для запуска бота
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем бота при старте приложения
    task = asyncio.create_task(bot.run())
    yield
    # Останавливаем бота при выключении
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    return {"status": "VK Bot is running!"}

@app.get("/health")
async def health():
    return {"status": "OK"}
