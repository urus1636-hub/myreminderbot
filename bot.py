import asyncio
import logging
import os
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8651845065:AAHID4sB8_efYdkt8vKcj9Eq_c6YV6n-u2E"  # <-- ВСТАВЬ ТОКЕН
PORT = 8000
# ================================

os.environ['TZ'] = 'Europe/Moscow'
try:
    time.tzset()
except:
    pass

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list"))
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я тестовый бот.\nЕсли ты это видишь — я работаю!",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(lambda c: c.data == "list")
async def callback_list(callback: types.CallbackQuery):
    await callback.message.answer("📋 У тебя пока нет напоминаний (тестовый режим).")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "help")
async def callback_help(callback: types.CallbackQuery):
    await callback.message.answer("❓ Это тестовый бот. Скоро здесь будет полный функционал.")
    await callback.answer()

async def healthcheck(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Web server started on port {PORT}")

async def main():
    logging.info("Бот запускается...")
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
