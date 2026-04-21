import asyncio
import logging
import os
import re
from datetime import datetime

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil import parser

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8651845065:AAFYzociP_Wojmwg4MnWOn3JXunzTC-2PdM"  # <-- ВСТАВЬ СВОЙ ТОКЕН
DATABASE = "reminders.db"
PORT = 8000
# ================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# FSM
class ReminderForm(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()
    editing_time = State()

# ---------- Клавиатуры ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders"),
        InlineKeyboardButton(text="➕ Создать", callback_data="create_reminder")
    )
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def back_to_menu_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    return builder.as_markup()

# ---------- База данных ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_at TIMESTAMP NOT NULL
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована")

async def add_reminder(user_id: int, text: str, remind_at: datetime) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)",
            (user_id, text, remind_at)
        )
        await db.commit()
        return cursor.lastrowid

async def get_user_reminders(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_at FROM reminders WHERE user_id = ? ORDER BY remind_at",
            (user_id,)
        )
        return await cursor.fetchall()

async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_reminder(reminder_id: int, user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_at FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        return await cursor.fetchone()

async def load_scheduled_jobs():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, user_id, text, remind_at FROM reminders")
        rows = await cursor.fetchall()
        logging.info(f"Найдено {len(rows)} напоминаний в БД")
        for rem_id, user_id, text, remind_at_str in rows:
            remind_at = datetime.fromisoformat(remind_at_str)
            if remind_at > datetime.now():
                scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=remind_at,
                    args=[user_id, text, rem_id],
                    id=f"rem_{rem_id}"
                )
                logging.info(f"Запланировано напоминание {rem_id} на {remind_at}")
            else:
                await delete_reminder(rem_id, user_id)
                logging.info(f"Удалено просроченное напоминание {rem_id}")

async def send_reminder(user_id: int, text: str, reminder_id: int):
    logging.info(f"Сработало напоминание {reminder_id} для {user_id}")
    try:
        await bot.send_message(
            user_id,
            f"⏰ НАПОМИНАНИЕ!\n\n"
            f"Ты просил напомнить:\n"
            f"📝 «{text}»"
        )
        logging.info(f"Напоминание {reminder_id} отправлено")
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
    finally:
        await delete_reminder(reminder_id, user_id)

def is_likely_datetime(text: str) -> bool:
    return bool(re.search(r'\d', text)) and bool(re.search(r'[.\-:/]', text))

# ---------- Команды ----------
@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот-напоминалка с кнопками.\nВыбери действие:",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🏠 Главное меню",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    text = "📌 *Как пользоваться:*\n\n• *📋 Мои напоминания* – посмотреть список и управлять.\n• *➕ Создать* – добавить новое напоминание.\n• Просто напиши мне текст, и я сразу предложу ввести время!"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "create_reminder")
async def start_create(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.waiting_for_text)
    await callback.message.edit_text(
        "📝 О чём тебе напомнить? Напиши текст:",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(ReminderForm.waiting_for_text)
async def process_text(message: types.Message, state: FSMContext):
    await state.update_data(reminder_text=message.text)
    await state.set_state(ReminderForm.waiting_for_time)
    await message.answer(
        "📅 Когда напомнить?\nВведи дату и время: *ДД.ММ.ГГГГ ЧЧ:ММ*\nНапример: `25.12.2026 15:30`",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )

@dp.message(ReminderForm.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    if not is_likely_datetime(time_str):
        await message.answer(
            "❌ Пожалуйста, введи дату и время в формате *ДД.ММ.ГГГГ ЧЧ:ММ*.",
            parse_mode="Markdown",
            reply_markup=back_to_menu_button()
        )
        return

    try:
        remind_at = parser.parse(time_str, dayfirst=True)
        if remind_at < datetime.now():
            await message.answer(
                "⏳ Это время уже прошло. Введи будущее время:",
                reply_markup=back_to_menu_button()
            )
            return
    except:
        await message.answer(
            "❌ Неверный формат. Попробуй снова: *ДД.ММ.ГГГГ ЧЧ:ММ*",
            parse_mode="Markdown",
            reply_markup=back_to_menu_button()
        )
        return

    data = await state.get_data()
    reminder_text = data["reminder_text"]
    user_id = message.from_user.id

    rem_id = await add_reminder(user_id, reminder_text, remind_at)
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_at,
        args=[user_id, reminder_text, rem_id],
        id=f"rem_{rem_id}"
    )
    logging.info(f"Добавлено напоминание {rem_id} на {remind_at}")

    await message.answer(
        f"✅ Запомнил! Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}:\n«{reminder_text}»",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "list_reminders")
async def list_reminders(callback: types.CallbackQuery):
    reminders = await get_user_reminders(callback.from_user.id)
    if not reminders:
        await callback.message.edit_text(
            "У тебя пока нет активных напоминаний.",
            reply_markup=back_to_menu_button()
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for rem_id, rem_text, remind_at in reminders:
        dt = datetime.fromisoformat(remind_at)
        label = f"{rem_text[:30]}{'…' if len(rem_text)>30 else ''} — {dt.strftime('%d.%m.%Y %H:%M')}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"remdetail_{rem_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))

    await callback.message.edit_text(
        "📋 *Твои напоминания:*\n\nВыбери одно, чтобы управлять.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("remdetail_"))
async def reminder_detail(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    reminder = await get_reminder(rem_id, callback.from_user.id)
    if not reminder:
        await callback.message.edit_text("❌ Напоминание не найдено.", reply_markup=back_to_menu_button())
        await callback.answer()
        return

    _, text, remind_at_str = reminder
    dt = datetime.fromisoformat(remind_at_str)
    text_preview = text[:100] + ('…' if len(text) > 100 else '')

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{rem_id}"),
        InlineKeyboardButton(text="✏️ Изменить время", callback_data=f"edit_{rem_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад к списку", callback_data="list_reminders"))

    await callback.message.edit_text(
        f"📌 *{text_preview}*\n\n⏰ Напомнить: {dt.strftime('%d.%m.%Y в %H:%M')}",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_callback(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    deleted = await delete_reminder(rem_id, callback.from_user.id)
    if deleted:
        try:
            scheduler.remove_job(f"rem_{rem_id}")
        except:
            pass
        await callback.answer("✅ Удалено", show_alert=False)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await list_reminders(callback)

@dp.callback_query(F.data.startswith("edit_"))
async def edit_callback(callback: types.CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_")[1])
    reminder = await get_reminder(rem_id, callback.from_user.id)
    if not reminder:
        await callback.message.edit_text("❌ Напоминание не найдено.", reply_markup=back_to_menu_button())
        await callback.answer()
        return

    await state.update_data(edit_id=rem_id, old_text=reminder[1])
    await state.set_state(ReminderForm.editing_time)
    await callback.message.edit_text(
        f"Текущее: «{reminder[1]}» на {datetime.fromisoformat(reminder[2]).strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Введи *новое время*: ДД.ММ.ГГГГ ЧЧ:ММ",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(ReminderForm.editing_time)
async def process_edit_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    if not is_likely_datetime(time_str):
        await message.answer(
            "❌ Пожалуйста, введи дату и время в формате *ДД.ММ.ГГГГ ЧЧ:ММ*.",
            parse_mode="Markdown",
            reply_markup=back_to_menu_button()
        )
        return

    try:
        new_time = parser.parse(time_str, dayfirst=True)
        if new_time < datetime.now():
            await message.answer(
                "⏳ Это время уже прошло. Введи будущее:",
                reply_markup=back_to_menu_button()
            )
            return
    except:
        await message.answer(
            "❌ Неверный формат. Попробуй снова: *ДД.ММ.ГГГГ ЧЧ:ММ*",
            parse_mode="Markdown",
            reply_markup=back_to_menu_button()
        )
        return

    data = await state.get_data()
    rem_id = data["edit_id"]
    old_text = data["old_text"]
    user_id = message.from_user.id

    await delete_reminder(rem_id, user_id)
    try:
        scheduler.remove_job(f"rem_{rem_id}")
    except:
        pass

    new_id = await add_reminder(user_id, old_text, new_time)
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=new_time,
        args=[user_id, old_text, new_id],
        id=f"rem_{new_id}"
    )

    await message.answer(
        f"✅ Время обновлено! Новое напоминание в {new_time.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@dp.message(StateFilter(None))
async def handle_any_text(message: types.Message, state: FSMContext):
    await state.update_data(reminder_text=message.text)
    await state.set_state(ReminderForm.waiting_for_time)
    await message.answer(
        "📅 Когда напомнить?\nВведи дату и время: *ДД.ММ.ГГГГ ЧЧ:ММ*",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )

# ---------- Веб-сервер ----------
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

# ---------- Самопинг ----------
async def self_ping(port: int):
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://0.0.0.0:{port}/") as resp:
                    logging.info(f"Self-ping: {resp.status}")
        except Exception as e:
            logging.warning(f"Self-ping failed: {e}")
        await asyncio.sleep(300)

# ---------- Запуск ----------
async def main():
    await init_db()
    await load_scheduled_jobs()
    scheduler.start()
    asyncio.create_task(self_ping(PORT))
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
