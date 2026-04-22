import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil import parser

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "ТВОЙ_ТОКЕН_СЮДА"
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
    waiting_for_repeat = State()
    editing_time = State()
    delegate_share = State()
    delegate_text = State()
    delegate_time = State()
    delegate_repeat = State()

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

def repeat_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔂 Каждый день", callback_data="repeat_day"),
        InlineKeyboardButton(text="⏰ Каждый час", callback_data="repeat_hour")
    )
    builder.row(
        InlineKeyboardButton(text="📅 Раз в неделю", callback_data="repeat_week"),
        InlineKeyboardButton(text="❌ Не повторять", callback_data="repeat_none")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    return builder.as_markup()

def share_contact_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="👤 Выбрать получателя", request_user=types.KeyboardButtonRequestUser(request_id=1)))
    builder.row(KeyboardButton(text="🔙 Отмена"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)

def reminder_actions_keyboard(rem_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{rem_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_{rem_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад к списку", callback_data="list_reminders"))
    return builder.as_markup()

# ---------- База данных ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_at TIMESTAMP NOT NULL,
                repeat_type TEXT DEFAULT NULL,
                from_user_id INTEGER DEFAULT NULL,
                from_username TEXT DEFAULT NULL
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована")

async def save_user(user_id: int, username: str = None, first_name: str = None):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, first_name, last_activity) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, datetime.now())
        )
        await db.commit()

async def add_reminder(user_id: int, text: str, remind_at: datetime, repeat_type: str = None, from_user_id: int = None, from_username: str = None) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO reminders (user_id, text, remind_at, repeat_type, from_user_id, from_username) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, remind_at, repeat_type, from_user_id, from_username)
        )
        await db.commit()
        return cursor.lastrowid

async def get_user_reminders(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_at, repeat_type, from_user_id, from_username FROM reminders WHERE user_id = ? ORDER BY remind_at",
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
            "SELECT id, text, remind_at, repeat_type, from_user_id, from_username FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        return await cursor.fetchone()

async def load_scheduled_jobs():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, user_id, text, remind_at, repeat_type, from_user_id, from_username FROM reminders")
        rows = await cursor.fetchall()
        logging.info(f"Найдено {len(rows)} напоминаний в БД")
        for rem_id, user_id, text, remind_at_str, repeat_type, from_user_id, from_username in rows:
            remind_at = datetime.fromisoformat(remind_at_str)
            if repeat_type:
                await schedule_repeat_reminder(rem_id, user_id, text, remind_at, repeat_type, from_user_id, from_username)
            elif remind_at > datetime.now():
                scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=remind_at,
                    args=[user_id, text, rem_id, None, from_user_id, from_username],
                    id=f"rem_{rem_id}"
                )
            else:
                await delete_reminder(rem_id, user_id)

async def schedule_repeat_reminder(rem_id: int, user_id: int, text: str, remind_at: datetime, repeat_type: str, from_user_id: int = None, from_username: str = None):
    if repeat_type == "hour":
        scheduler.add_job(
            send_reminder,
            trigger=CronTrigger(minute=remind_at.minute),
            args=[user_id, text, rem_id, "hour", from_user_id, from_username],
            id=f"rem_{rem_id}"
        )
    elif repeat_type == "day":
        scheduler.add_job(
            send_reminder,
            trigger=CronTrigger(hour=remind_at.hour, minute=remind_at.minute),
            args=[user_id, text, rem_id, "day", from_user_id, from_username],
            id=f"rem_{rem_id}"
        )
    elif repeat_type == "week":
        scheduler.add_job(
            send_reminder,
            trigger=CronTrigger(day_of_week=remind_at.weekday(), hour=remind_at.hour, minute=remind_at.minute),
            args=[user_id, text, rem_id, "week", from_user_id, from_username],
            id=f"rem_{rem_id}"
        )
    logging.info(f"Запланировано повторяющееся напоминание {rem_id} (тип: {repeat_type})")

async def send_reminder(user_id: int, text: str, reminder_id: int, repeat_type: str = None, from_user_id: int = None, from_username: str = None):
    logging.info(f"Сработало напоминание {reminder_id} для {user_id}")
    try:
        message = f"⏰ НАПОМИНАНИЕ!\n\n"
        if from_username:
            message += f"👤 От пользователя @{from_username}\n"
        elif from_user_id:
            message += f"👤 От пользователя ID: {from_user_id}\n"
        message += f"\n📝 «{text}»"
        if repeat_type:
            message += f"\n\n🔄 Повтор: {repeat_type}"
        await bot.send_message(user_id, message)
        logging.info(f"Напоминание {reminder_id} отправлено")
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
        if "bot was blocked" in str(e) or "user is deactivated" in str(e):
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
        cursor = await db.execute("SELECT user_id, text, remind_at, repeat_type, from_username FROM reminders ORDER BY id DESC LIMIT 5")
        last_reminders = await cursor.fetchall()
    text = f"📊 <b>Статистика бота</b>\n\n👥 Пользователей: {users_count}\n⏰ Напоминаний: {reminders_count}\n\n"
    if last_reminders:
        text += "📋 <b>Последние:</b>\n"
        for user_id, rem_text, remind_at, repeat_type, from_username in last_reminders:
            dt = datetime.fromisoformat(remind_at)
            via = f" (от @{from_username})" if from_username else ""
            rep = f" [{repeat_type}]" if repeat_type else ""
            text += f"• <code>{user_id}</code> — {rem_text[:20]}... — {dt.strftime('%d.%m %H:%M')}{rep}{via}\n"
    await message.answer(text, parse_mode="HTML")

# ---------- Обычные напоминания ----------
@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "👋 Привет! Я бот-напоминалка.\n\n"
        "Используй кнопки ниже или команды:\n"
        "/list — мои напоминания\n"
        "/delete ID — удалить напоминание\n"
        "/myid — узнать свой ID",
        reply_markup=main_menu_keyboard()
    )

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"Твой ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    reminders = await get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У тебя пока нет активных напоминаний.")
        return
    lines = []
    for rem_id, rem_text, remind_at, repeat_type, from_user_id, from_username in reminders:
        dt = datetime.fromisoformat(remind_at)
        via = f" (от @{from_username})" if from_username else ""
        rep = f" [{repeat_type}]" if repeat_type else ""
        lines.append(f"🆔 {rem_id} | {dt.strftime('%d.%m %H:%M')}{rep} — {rem_text[:30]}{via}")
    await message.answer("📋 *Твои напоминания:*\n" + "\n".join(lines), parse_mode="Markdown")

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

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 Главное меню", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "📌 *Как пользоваться:*\n\n"
        "• Кнопка «Создать» — напоминание для себя.\n"
        "• Кнопка «Делегировать» — отправить напоминание другу.\n"
        "• Кнопка «Мои напоминания» — посмотреть и управлять.\n\n"
        "Команды:\n"
        "/myid — узнать свой ID\n"
        "/list — список напоминаний\n"
        "/delete ID — удалить"
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

    await state.update_data(remind_at=remind_at)
    await state.set_state(ReminderForm.waiting_for_repeat)
    await message.answer(
        "🔄 Как часто напоминать?",
        reply_markup=repeat_keyboard()
    )

@dp.callback_query(ReminderForm.waiting_for_repeat, F.data.startswith("repeat_"))
async def process_repeat(callback: types.CallbackQuery, state: FSMContext):
    repeat_type = callback.data.replace("repeat_", "")
    data = await state.get_data()
    reminder_text = data["reminder_text"]
    remind_at = data["remind_at"]
    user_id = callback.from_user.id

    repeat_str = {"hour": "каждый час", "day": "каждый день", "week": "раз в неделю", "none": "без повтора"}.get(repeat_type, "")
    rem_id = await add_reminder(user_id, reminder_text, remind_at, repeat_type if repeat_type != "none" else None)

    if repeat_type == "none":
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=remind_at,
            args=[user_id, reminder_text, rem_id, None],
            id=f"rem_{rem_id}"
        )
    else:
        await schedule_repeat_reminder(rem_id, user_id, reminder_text, remind_at, repeat_type)

    await log_to_admin(
        f"🆕 <b>Новое напоминание</b>\n"
        f"👤 Пользователь: @{callback.from_user.username or 'нет'} (ID: <code>{user_id}</code>)\n"
        f"📝 Текст: {reminder_text}\n"
        f"⏰ Напомнить: {remind_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"🔄 Повтор: {repeat_str if repeat_type != 'none' else 'нет'}"
    )

    await callback.message.edit_text(
        f"✅ Запомнил! Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}:\n«{reminder_text}»\n"
        f"🔄 Повтор: {repeat_str if repeat_type != 'none' else 'нет'}",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()
    await callback.answer()

# ---------- Делегирование ----------
@dp.callback_query(F.data == "delegate_reminder")
async def delegate_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.delegate_share)
    await callback.message.answer(
        "👤 Нажмите кнопку ниже и выберите получателя из своих контактов:",
        reply_markup=share_contact_keyboard()
    )
    await callback.answer()

@dp.message(ReminderForm.delegate_share, F.text == "🔙 Отмена")
async def delegate_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Делегирование отменено.", reply_markup=types.ReplyKeyboardRemove())
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@dp.message(ReminderForm.delegate_share, F.user_shared)
async def delegate_user_shared(message: types.Message, state: FSMContext):
    user_shared = message.user_shared
    target_id = user_shared.user_id
    await state.update_data(target_id=target_id)
    await state.set_state(ReminderForm.delegate_text)
    await message.answer(
        f"📝 Введите текст напоминания:",
        reply_markup=types.ReplyKeyboardRemove()
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

    await state.update_data(remind_at=remind_at)
    await state.set_state(ReminderForm.delegate_repeat)
    await message.answer(
        "🔄 Как часто напоминать?",
        reply_markup=repeat_keyboard()
    )

@dp.callback_query(ReminderForm.delegate_repeat, F.data.startswith("repeat_"))
async def delegate_repeat(callback: types.CallbackQuery, state: FSMContext):
    repeat_type = callback.data.replace("repeat_", "")
    data = await state.get_data()
    reminder_text = data["reminder_text"]
    remind_at = data["remind_at"]
    target_id = data.get("target_id")
    from_user_id = callback.from_user.id
    from_username = callback.from_user.username

    repeat_str = {"hour": "каждый час", "day": "каждый день", "week": "раз в неделю", "none": "без повтора"}.get(repeat_type, "")

    # Уведомление получателю
    notify_message = (
        f"📬 Вам пришло напоминание от @{from_username or from_user_id}:\n"
        f"📝 «{reminder_text}»\n"
        f"⏰ Оно сработает {remind_at.strftime('%d.%m.%Y в %H:%M')}\n"
        f"🔄 Повтор: {repeat_str if repeat_type != 'none' else 'нет'}"
    )
    try:
        await bot.send_message(target_id, notify_message)
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление {target_id}: {e}")
        await callback.message.edit_text("❌ Не удалось отправить уведомление пользователю. Возможно, он заблокировал бота.")
        await callback.answer()
        return

    rem_id = await add_reminder(target_id, reminder_text, remind_at, repeat_type if repeat_type != "none" else None, from_user_id, from_username)

    if repeat_type == "none":
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=remind_at,
            args=[target_id, reminder_text, rem_id, None, from_user_id, from_username],
            id=f"rem_{rem_id}"
        )
    else:
        await schedule_repeat_reminder(rem_id, target_id, reminder_text, remind_at, repeat_type, from_user_id, from_username)

    await log_to_admin(
        f"🔄 <b>Делегированное напоминание</b>\n"
        f"👤 От: @{from_username} (ID: <code>{from_user_id}</code>)\n"
        f"🎯 Кому: ID: <code>{target_id}</code>\n"
        f"📝 Текст: {reminder_text}\n"
        f"⏰ Напомнить: {remind_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"🔄 Повтор: {repeat_str if repeat_type != 'none' else 'нет'}"
    )

    await callback.message.edit_text(
        f"✅ Напоминание создано!\n"
        f"Получатель уже уведомлён. Само напоминание придёт {remind_at.strftime('%d.%m.%Y в %H:%M')}.\n"
        f"🔄 Повтор: {repeat_str if repeat_type != 'none' else 'нет'}",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()
    await callback.answer()

# ---------- Просмотр и управление ----------
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
    for rem_id, rem_text, remind_at, repeat_type, from_user_id, from_username in reminders:
        dt = datetime.fromisoformat(remind_at)
        via = f" (от @{from_username})" if from_username else ""
        rep = f" [{repeat_type}]" if repeat_type else ""
        label = f"{rem_text[:30]}{'…' if len(rem_text)>30 else ''} — {dt.strftime('%d.%m %H:%M')}{rep}{via}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"detail_{rem_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))

    await callback.message.edit_text(
        "📋 *Твои напоминания:*\n\nВыбери одно, чтобы управлять.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("detail_"))
async def reminder_detail(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    reminder = await get_reminder(rem_id, callback.from_user.id)
    if not reminder:
        await callback.message.edit_text("❌ Напоминание не найдено.", reply_markup=back_to_menu_button())
        await callback.answer()
        return

    _, text, remind_at_str, repeat_type, from_user_id, from_username = reminder
    dt = datetime.fromisoformat(remind_at_str)
    via = f"\n👤 От: @{from_username}" if from_username else ""
    rep = f"\n🔄 Повтор: {repeat_type}" if repeat_type else ""

    await callback.message.edit_text(
        f"📌 *Напоминание*\n\n📝 {text}\n⏰ {dt.strftime('%d.%m.%Y в %H:%M')}{rep}{via}",
        parse_mode="Markdown",
        reply_markup=reminder_actions_keyboard(rem_id)
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
        await message.answer("❌ Пожалуйста, введи дату и время в формате *ДД.ММ.ГГГГ ЧЧ:ММ*.", parse_mode="Markdown", reply_markup=back_to_menu_button())
        return
    try:
        new_time = parser.parse(time_str, dayfirst=True)
        if new_time < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее:", reply_markup=back_to_menu_button())
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: *ДД.ММ.ГГГГ ЧЧ:ММ*", parse_mode="Markdown", reply_markup=back_to_menu_button())
        return

    data = await state.get_data()
    rem_id = data["edit_id"]
    old_text = data["old_text"]
    user_id = message.from_user.id

    # Удаляем старую задачу
    try:
        scheduler.remove_job(f"rem_{rem_id}")
    except:
        pass

    # Получаем старый repeat_type
    old_reminder = await get_reminder(rem_id, user_id)
    repeat_type = old_reminder[3] if old_reminder else None

    await delete_reminder(rem_id, user_id)
    new_id = await add_reminder(user_id, old_text, new_time, repeat_type)

    if repeat_type:
        await schedule_repeat_reminder(new_id, user_id, old_text, new_time, repeat_type)
    else:
        scheduler.add_job(send_reminder, trigger="date", run_date=new_time, args=[user_id, old_text, new_id, None], id=f"rem_{new_id}")

    await message.answer(
        f"✅ Время обновлено! Новое напоминание в {new_time.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

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
