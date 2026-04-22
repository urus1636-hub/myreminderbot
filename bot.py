import asyncio
import logging
import os
import re
import time
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
BOT_TOKEN = "8651845065:AAHOi3NvhT0YrgHzomFO1TTwgquZ3tPykrU"
DATABASE = "reminders.db"
PORT = 8000

ADMIN_IDS = [1820245156]
ADMIN_LOG_CHAT = 1820245156
# ================================

os.environ['TZ'] = 'Europe/Moscow'
try:
    time.tzset()
except:
    pass

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
    delegate_target = State()
    delegate_text = State()
    delegate_time = State()

async def log_to_admin(text: str):
    try:
        await bot.send_message(ADMIN_LOG_CHAT, text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки лога: {e}")

def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders"),
        InlineKeyboardButton(text="➕ Создать", callback_data="create_reminder")
    )
    builder.row(
        InlineKeyboardButton(text="👥 Делегировать", callback_data="delegate_reminder"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="help")
    )
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
                remind_at TIMESTAMP NOT NULL,
                from_user_id INTEGER DEFAULT NULL,
                from_username TEXT DEFAULT NULL
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована")

async def add_reminder(user_id: int, text: str, remind_at: datetime, from_user_id: int = None, from_username: str = None) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO reminders (user_id, text, remind_at, from_user_id, from_username) VALUES (?, ?, ?, ?, ?)",
            (user_id, text, remind_at, from_user_id, from_username)
        )
        await db.commit()
        return cursor.lastrowid

async def get_user_reminders(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_at, from_user_id, from_username FROM reminders WHERE user_id = ? ORDER BY remind_at",
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
            "SELECT id, text, remind_at, from_user_id, from_username FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        return await cursor.fetchone()

async def load_scheduled_jobs():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, user_id, text, remind_at, from_user_id, from_username FROM reminders")
        rows = await cursor.fetchall()
        logging.info(f"Найдено {len(rows)} напоминаний в БД")
        for rem_id, user_id, text, remind_at_str, from_user_id, from_username in rows:
            remind_at = datetime.fromisoformat(remind_at_str)
            if remind_at > datetime.now():
                scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=remind_at,
                    args=[user_id, text, rem_id, from_user_id, from_username],
                    id=f"rem_{rem_id}"
                )
                logging.info(f"Запланировано напоминание {rem_id} на {remind_at}")
            else:
                await delete_reminder(rem_id, user_id)
                logging.info(f"Удалено просроченное напоминание {rem_id}")

async def send_reminder(user_id: int, text: str, reminder_id: int, from_user_id: int = None, from_username: str = None):
    logging.info(f"Сработало напоминание {reminder_id} для {user_id}")
    try:
        message = f"⏰ НАПОМИНАНИЕ!\n\n"
        if from_username:
            message += f"👤 От пользователя @{from_username}\n"
        elif from_user_id:
            message += f"👤 От пользователя ID: {from_user_id}\n"
        message += f"\n📝 «{text}»"
        await bot.send_message(user_id, message)
        logging.info(f"Напоминание {reminder_id} отправлено")
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
    finally:
        await delete_reminder(reminder_id, user_id)

def is_likely_datetime(text: str) -> bool:
    return bool(re.search(r'\d', text)) and bool(re.search(r'[.\-:/]', text))

# ---------- Админ-команды ----------
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer("🔧 Админ-команды:\n\n/stats — статистика\n/admin — это сообщение")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM reminders")
        users_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM reminders")
        reminders_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT user_id, text, remind_at, from_username FROM reminders ORDER BY id DESC LIMIT 5")
        last_reminders = await cursor.fetchall()
    text = f"📊 <b>Статистика бота</b>\n\n👥 Пользователей: {users_count}\n⏰ Напоминаний: {reminders_count}\n\n"
    if last_reminders:
        text += "📋 <b>Последние:</b>\n"
        for user_id, rem_text, remind_at, from_username in last_reminders:
            dt = datetime.fromisoformat(remind_at)
            via = f" (от @{from_username})" if from_username else ""
            text += f"• <code>{user_id}</code> — {rem_text[:20]}... — {dt.strftime('%d.%m %H:%M')}{via}\n"
    await message.answer(text, parse_mode="HTML")

# ---------- Делегирование через кнопку ----------
@dp.callback_query(F.data == "delegate_reminder")
async def delegate_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.delegate_target)
    await callback.message.edit_text(
        "👤 Введите @username или числовой ID пользователя, которому хотите отправить напоминание:",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(ReminderForm.delegate_target)
async def delegate_target(message: types.Message, state: FSMContext):
    target = message.text.strip()
    target_id = None
    target_username = None

    if target.startswith("@"):
        target_username = target[1:]
    elif target.isdigit():
        target_id = int(target)
    else:
        await message.answer("❌ Пожалуйста, введите @username или числовой ID.")
        return

    await state.update_data(target_id=target_id, target_username=target_username)
    await state.set_state(ReminderForm.delegate_text)
    await message.answer(
        f"📝 Введите текст напоминания для {target}:",
        reply_markup=back_to_menu_button()
    )

@dp.message(ReminderForm.delegate_text)
async def delegate_text(message: types.Message, state: FSMContext):
    reminder_text = message.text.strip()
    await state.update_data(reminder_text=reminder_text)
    await state.set_state(ReminderForm.delegate_time)
    await message.answer(
        "📅 Когда напомнить?\nВведи дату и время: *ДД.ММ.ГГГГ ЧЧ:ММ*\nНапример: `25.12.2026 15:30`",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )

@dp.message(ReminderForm.delegate_time)
async def delegate_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    if not is_likely_datetime(time_str):
        await message.answer("❌ Пожалуйста, введи дату и время в формате *ДД.ММ.ГГГГ ЧЧ:ММ*.", parse_mode="Markdown")
        return

    try:
        remind_at = parser.parse(time_str, dayfirst=True)
        if remind_at < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее время:")
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: *ДД.ММ.ГГГГ ЧЧ:ММ*", parse_mode="Markdown")
        return

    data = await state.get_data()
    reminder_text = data["reminder_text"]
    target_id = data.get("target_id")
    target_username = data.get("target_username")
    from_user_id = message.from_user.id
    from_username = message.from_user.username

    # Если ID известен, используем его, иначе сохраняем username
    actual_target_id = target_id

    rem_id = await add_reminder(
        user_id=actual_target_id if actual_target_id else 0,
        text=reminder_text,
        remind_at=remind_at,
        from_user_id=from_user_id,
        from_username=from_username
    )

    if actual_target_id is None and target_username:
        scheduler.add_job(
            send_reminder_by_username,
            trigger="date",
            run_date=remind_at,
            args=[target_username, reminder_text, rem_id, from_username],
            id=f"rem_{rem_id}"
        )
    else:
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=remind_at,
            args=[actual_target_id, reminder_text, rem_id, from_user_id, from_username],
            id=f"rem_{rem_id}"
        )

    await log_to_admin(
        f"🔄 <b>Делегированное напоминание</b>\n"
        f"👤 От: @{from_username} (ID: <code>{from_user_id}</code>)\n"
        f"🎯 Кому: @{target_username or target_id}\n"
        f"📝 Текст: {reminder_text}\n"
        f"⏰ Напомнить: {remind_at.strftime('%d.%m.%Y %H:%M')}"
    )

    await message.answer(
        f"✅ Напоминание для @{target_username or target_id} создано!\n"
        f"Оно придёт {remind_at.strftime('%d.%m.%Y в %H:%M')}.",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

async def send_reminder_by_username(username: str, text: str, reminder_id: int, from_username: str = None):
    logging.info(f"Попытка отправить напоминание {reminder_id} пользователю @{username}")
    try:
        message = f"⏰ НАПОМИНАНИЕ!\n\n"
        if from_username:
            message += f"👤 От пользователя @{from_username}\n"
        message += f"\n📝 «{text}»"
        await bot.send_message(f"@{username}", message)
        logging.info(f"Напоминание {reminder_id} отправлено по username @{username}")
    except Exception as e:
        logging.error(f"Ошибка отправки по username @{username}: {e}")

# ---------- Обычные команды ----------
@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот-напоминалка.\n\n"
        "📌 Команды:\n"
        "/remind @user текст — отправить напоминание другу\n"
        "/list — твои напоминания\n"
        "/delete ID — удалить напоминание\n"
        "Или используй кнопки ниже:",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 Главное меню", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    text = (
        "📌 *Как пользоваться:*\n\n"
        "• Напиши текст — я спрошу когда напомнить.\n"
        "• Кнопка «Делегировать» — отправить напоминание другу.\n"
        "• /list — твои напоминания.\n"
        "• /delete ID — удалить напоминание."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "create_reminder")
async def start_create(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.waiting_for_text)
    await callback.message.edit_text("📝 О чём тебе напомнить? Напиши текст:", reply_markup=back_to_menu_button())
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
        await message.answer("❌ Пожалуйста, введи дату и время в формате *ДД.ММ.ГГГГ ЧЧ:ММ*.", parse_mode="Markdown", reply_markup=back_to_menu_button())
        return
    try:
        remind_at = parser.parse(time_str, dayfirst=True)
        if remind_at < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее время:", reply_markup=back_to_menu_button())
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: *ДД.ММ.ГГГГ ЧЧ:ММ*", parse_mode="Markdown", reply_markup=back_to_menu_button())
        return

    data = await state.get_data()
    reminder_text = data["reminder_text"]
    user_id = message.from_user.id

    rem_id = await add_reminder(user_id, reminder_text, remind_at)
    scheduler.add_job(send_reminder, trigger="date", run_date=remind_at, args=[user_id, reminder_text, rem_id], id=f"rem_{rem_id}")
    logging.info(f"Добавлено напоминание {rem_id} на {remind_at}")

    username = message.from_user.username or "нет username"
    await log_to_admin(
        f"🆕 <b>Новое напоминание</b>\n"
        f"👤 Пользователь: @{username} (ID: <code>{user_id}</code>)\n"
        f"📝 Текст: {reminder_text}\n"
        f"⏰ Напомнить: {remind_at.strftime('%d.%m.%Y %H:%M')}"
    )

    await message.answer(
        f"✅ Запомнил! Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}:\n«{reminder_text}»",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    reminders = await get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У тебя пока нет активных напоминаний.", reply_markup=main_menu_keyboard())
        return
    lines = []
    for rem_id, rem_text, remind_at, from_user_id, from_username in reminders:
        dt = datetime.fromisoformat(remind_at)
        via = f" (от @{from_username})" if from_username else ""
        lines.append(f"🆔 {rem_id} | {dt.strftime('%d.%m %H:%M')} — {rem_text[:30]}{via}")
    await message.answer("📋 *Твои напоминания:*\n" + "\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_keyboard())

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /delete <ID>")
        return
    try:
        rem_id = int(args[1])
    except:
        await message.answer("ID должен быть числом.")
        return
    deleted = await delete_reminder(rem_id, message.from_user.id)
    if deleted:
        try:
            scheduler.remove_job(f"rem_{rem_id}")
        except:
            pass
        await message.answer(f"✅ Напоминание {rem_id} удалено.")
    else:
        await message.answer("❌ Напоминание не найдено.")

# ---------- Веб-сервер и самопинг ----------
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
    await asyncio.gather(dp.start_polling(bot), run_web_server())

if __name__ == "__main__":
    asyncio.run(main())
