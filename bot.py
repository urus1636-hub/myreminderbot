import asyncio
import logging
import os
import random
import time
import hashlib
import secrets
from datetime import datetime, date, timedelta

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

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8568815241:AAEam_4F28Host-vnQ0pXVjcCzMldUtVACo"
CARD_NUMBER = "22022084264326435781"
CHANNEL_ID = "@luckyfortune4"
ADMIN_IDS = [1820245156]
COMMISSION_PERCENT = 20
REFERRAL_BONUS = 5
MAX_FREE_SLOTS_PER_DAY = 1

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE = os.path.join(DATA_DIR, "lottery.db")

PORT = 8000
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

class LotteryForm(StatesGroup):
    waiting_for_prize = State()
    waiting_for_price = State()
    waiting_for_slots = State()

# ---------- Клавиатуры ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎲 Смотреть активные лотереи", callback_data="list_lotteries"))
    builder.row(InlineKeyboardButton(text="📊 Мои участия и выигрыши", callback_data="my_participations"))
    builder.row(InlineKeyboardButton(text="👥 Пригласить друзей (рефералы)", callback_data="ref_info"))
    builder.row(InlineKeyboardButton(text="📋 Список команд", callback_data="show_commands"))
    builder.row(InlineKeyboardButton(text="❓ Как это работает?", callback_data="help"))
    return builder.as_markup()

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать новую лотерею", callback_data="admin_create"))
    builder.row(InlineKeyboardButton(text="📋 Управление активными лотереями", callback_data="admin_list"))
    builder.row(InlineKeyboardButton(text="📊 Статистика и доходы", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="👥 Мои рефералы", callback_data="ref_info"))
    builder.row(InlineKeyboardButton(text="🔙 Вернуться в главное меню", callback_data="main_menu"))
    return builder.as_markup()

def back_to_menu_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Вернуться назад", callback_data="main_menu"))
    return builder.as_markup()

def subscribe_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Подписаться на наш канал", url="https://t.me/luckyfortune4"))
    builder.row(InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="check_subscription"))
    return builder.as_markup()

# ---------- Проверка подписки ----------
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        if member.status in ["creator", "administrator", "member"]:
            return True
        return False
    except:
        return True  # Если ошибка — разрешаем

# ---------- База данных ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lotteries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prize_name TEXT NOT NULL,
                slot_price INTEGER NOT NULL,
                total_slots INTEGER NOT NULL,
                taken_slots INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                winner_id INTEGER,
                secret_seed TEXT,
                public_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                paid INTEGER DEFAULT 0,
                slot_number INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                free_slots INTEGER DEFAULT 0,
                last_free_slot_used DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована")

async def save_user(user_id: int, username: str = None, first_name: str = None, referrer_id: int = None):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = await cursor.fetchone()
        if not exists:
            await db.execute(
                "INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, referrer_id)
            )
            if referrer_id and referrer_id != user_id:
                await db.execute(
                    "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                    (referrer_id, user_id)
                )
                cursor = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,))
                count = (await cursor.fetchone())[0]
                if count >= REFERRAL_BONUS:
                    await db.execute(
                        "UPDATE users SET free_slots = free_slots + 1 WHERE user_id = ?",
                        (referrer_id,)
                    )
        else:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id)
            )
        await db.commit()

async def get_user_free_slots(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT free_slots, last_free_slot_used FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row and row[0] > 0:
            if row[1] == date.today():
                return 0
            return row[0]
        return 0

async def use_free_slot(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT free_slots FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row and row[0] > 0:
            await db.execute(
                "UPDATE users SET free_slots = free_slots - 1, last_free_slot_used = ? WHERE user_id = ?",
                (date.today(), user_id)
            )
            await db.commit()
            return True
        return False

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def create_lottery(prize_name: str, slot_price: int, total_slots: int) -> tuple[int, str, str]:
    secret_seed = secrets.token_hex(16)
    public_hash = hashlib.sha256(secret_seed.encode()).hexdigest()
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO lotteries (prize_name, slot_price, total_slots, secret_seed, public_hash) VALUES (?, ?, ?, ?, ?)",
            (prize_name, slot_price, total_slots, secret_seed, public_hash)
        )
        await db.commit()
        return cursor.lastrowid, secret_seed, public_hash

async def get_active_lotteries():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, prize_name, slot_price, total_slots, taken_slots FROM lotteries WHERE status = 'active' ORDER BY id DESC"
        )
        return await cursor.fetchall()

async def get_lottery(lottery_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, prize_name, slot_price, total_slots, taken_slots, status, winner_id, secret_seed, public_hash FROM lotteries WHERE id = ?",
            (lottery_id,)
        )
        return await cursor.fetchone()

async def get_lottery_slots(lottery_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT slot_number, user_id, username FROM slots WHERE lottery_id = ? AND paid = 1 ORDER BY slot_number",
            (lottery_id,)
        )
        return await cursor.fetchall()

async def add_slot(lottery_id: int, user_id: int, username: str, slot_number: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO slots (lottery_id, user_id, username, slot_number, paid) VALUES (?, ?, ?, ?, 0)",
            (lottery_id, user_id, username, slot_number)
        )
        await db.commit()
        return cursor.lastrowid

async def mark_slot_paid(slot_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE slots SET paid = 1 WHERE id = ?", (slot_id,))
        cursor = await db.execute("SELECT lottery_id FROM slots WHERE id = ?", (slot_id,))
        row = await cursor.fetchone()
        if row:
            lottery_id = row[0]
            await db.execute("UPDATE lotteries SET taken_slots = taken_slots + 1 WHERE id = ?", (lottery_id,))
        await db.commit()

async def get_user_participations(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            SELECT s.lottery_id, l.prize_name, s.slot_number, l.status, l.winner_id
            FROM slots s
            JOIN lotteries l ON s.lottery_id = l.id
            WHERE s.user_id = ? AND s.paid = 1
            ORDER BY s.created_at DESC
        """, (user_id,))
        return await cursor.fetchall()

async def get_free_slot_number(lottery_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT slot_number FROM slots WHERE lottery_id = ? AND paid = 1 ORDER BY slot_number",
            (lottery_id,)
        )
        taken = [row[0] for row in await cursor.fetchall()]
        cursor = await db.execute("SELECT total_slots FROM lotteries WHERE id = ?", (lottery_id,))
        total = (await cursor.fetchone())[0]
        for i in range(1, total + 1):
            if i not in taken:
                return i
        return None

async def check_lottery_full(lottery_id: int) -> bool:
    lottery = await get_lottery(lottery_id)
    return lottery[4] >= lottery[3]

async def pick_winner(lottery_id: int) -> tuple:
    lottery = await get_lottery(lottery_id)
    secret_seed = lottery[7]
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT user_id, username FROM slots WHERE lottery_id = ? AND paid = 1",
            (lottery_id,)
        )
        slots = await cursor.fetchall()
        if not slots:
            return None, None, None
        random.seed(secret_seed)
        winner = random.choice(slots)
        await db.execute(
            "UPDATE lotteries SET status = 'finished', winner_id = ? WHERE id = ?",
            (winner[0], lottery_id)
        )
        await db.commit()
        return winner[0], winner[1], secret_seed

async def get_slot_info(slot_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            SELECT s.user_id, s.username, s.lottery_id, l.prize_name, l.slot_price
            FROM slots s
            JOIN lotteries l ON s.lottery_id = l.id
            WHERE s.id = ?
        """, (slot_id,))
        return await cursor.fetchone()

async def notify_admin(text: str, reply_markup: InlineKeyboardMarkup = None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, referrer_id)

    if await is_subscribed(message.from_user.id):
        if message.from_user.id in ADMIN_IDS:
            await message.answer(
                "👑 <b>Админ-панель Lucky Fortune</b>\n\n"
                "Добро пожаловать, Босс!",
                parse_mode="HTML",
                reply_markup=admin_menu_keyboard()
            )
        else:
            await message.answer(
                "🎲 <b>Добро пожаловать в Lucky Fortune!</b>\n\n"
                "Это бот для честных розыгрышей призов.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
    else:
        await message.answer(
            "📢 <b>Чтобы пользоваться ботом, нужно подписаться на наш канал!</b>",
            parse_mode="HTML",
            reply_markup=subscribe_keyboard()
        )

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ <b>Спасибо за подписку!</b>\n\n"
            "Теперь ты можешь пользоваться ботом. 🍀",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ты ещё не подписался!</b>",
            parse_mode="HTML",
            reply_markup=subscribe_keyboard()
        )
        await callback.answer("❌ Ты ещё не подписался!", show_alert=True)

@dp.message(Command("ref"))
async def cmd_ref(message: types.Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer("❌ Сначала подпишись на канал!", reply_markup=subscribe_keyboard())
        return

    user_id = message.from_user.id
    count = await get_referral_count(user_id)
    free_slots = await get_user_free_slots(user_id)
    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"

    text = (
        f"🔗 <b>Твоя реферальная ссылка</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Приглашено: {count}/{REFERRAL_BONUS}\n"
        f"🎁 Бесплатных слотов: {free_slots}\n"
        f"⚠️ Лимит: 1 слот в сутки."
    )
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "ref_info")
async def ref_info(callback: types.CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
        return

    user_id = callback.from_user.id
    count = await get_referral_count(user_id)
    free_slots = await get_user_free_slots(user_id)
    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"

    text = (
        f"🔗 <b>Твоя реферальная ссылка</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Приглашено: {count}/{REFERRAL_BONUS}\n"
        f"🎁 Бесплатных слотов: {free_slots}\n"
        f"⚠️ Лимит: 1 слот в сутки."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_to_menu_button())
    await callback.answer()

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "show_commands")
async def show_commands(callback: types.CallbackQuery):
    text = (
        "📋 <b>Доступные команды</b>\n\n"
        "/start — Главное меню\n"
        "/ref — Твоя реферальная ссылка\n"
        "/myid — Узнать свой Telegram ID"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_to_menu_button())
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "📢 Чтобы пользоваться ботом, подпишись на наш канал!",
            reply_markup=subscribe_keyboard()
        )
        await callback.answer()
        return

    if callback.from_user.id in ADMIN_IDS:
        await callback.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    else:
        await callback.message.edit_text("🎲 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    text = (
        "📌 <b>Как участвовать</b>\n\n"
        "1️⃣ Выбери лотерею.\n"
        "2️⃣ Займи слот.\n"
        "3️⃣ Оплати на карту.\n"
        "4️⃣ Нажми «Я оплатил».\n"
        "5️⃣ Жди результата!\n\n"
        "🔒 Честность проверяется через хеш."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

# ---------- Админские команды (FSM с StateFilter) ----------
@dp.callback_query(F.data == "admin_create")
async def admin_create_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(LotteryForm.waiting_for_prize)
    await callback.message.edit_text(
        "🎁 <b>Создание новой лотереи</b>\n\n"
        "Введите название приза:",
        parse_mode="HTML",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(LotteryForm.waiting_for_prize)
async def admin_prize(message: types.Message, state: FSMContext):
    await state.update_data(prize_name=message.text.strip())
    await state.set_state(LotteryForm.waiting_for_price)
    await message.answer("💰 Введите цену слота в рублях:", reply_markup=back_to_menu_button())

@dp.message(LotteryForm.waiting_for_price)
async def admin_price(message: types.Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите целое положительное число.")
        return
    await state.update_data(slot_price=price)
    await state.set_state(LotteryForm.waiting_for_slots)
    await message.answer("🎰 Введите количество слотов:", reply_markup=back_to_menu_button())

@dp.message(LotteryForm.waiting_for_slots)
async def admin_slots(message: types.Message, state: FSMContext):
    try:
        slots = int(message.text.strip())
        if slots <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите целое положительное число.")
        return

    data = await state.get_data()
    prize_name = data["prize_name"]
    slot_price = data["slot_price"]

    lottery_id, secret_seed, public_hash = await create_lottery(prize_name, slot_price, slots)

    await message.answer(
        f"✅ <b>Лотерея создана!</b>\n\n"
        f"🎁 Приз: {prize_name}\n"
        f"💰 Цена слота: {slot_price} ₽\n"
        f"🎰 Слотов: {slots}\n\n"
        f"🔒 <b>Хеш:</b> <code>{public_hash}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_list")
async def admin_list(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    lotteries = await get_active_lotteries()
    if not lotteries:
        await callback.message.edit_text("Нет активных лотерей.", reply_markup=admin_menu_keyboard())
        await callback.answer()
        return

    text = "📋 <b>Активные лотереи</b>\n\n"
    for lid, prize, price, total, taken in lotteries:
        text += f"🆔 {lid} | {prize}\n💰 {price}₽ | 🎰 {taken}/{total} слотов\n\n"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM lotteries")
        total_lotteries = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM slots WHERE paid = 1")
        total_slots = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT SUM(l.slot_price) FROM slots s JOIN lotteries l ON s.lottery_id = l.id WHERE s.paid = 1")
        total_revenue = (await cursor.fetchone())[0] or 0
        commission = int(total_revenue * COMMISSION_PERCENT / 100)
        cursor = await db.execute("SELECT COUNT(*) FROM referrals")
        total_refs = (await cursor.fetchone())[0]

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"🎰 Лотерей: {total_lotteries}\n"
        f"🎲 Слотов: {total_slots}\n"
        f"👥 Рефералов: {total_refs}\n"
        f"💰 Оборот: {total_revenue} ₽\n"
        f"💎 Прибыль: {commission} ₽"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu_keyboard())
    await callback.answer()

# ---------- Пользовательские функции ----------
@dp.callback_query(F.data == "list_lotteries")
async def list_lotteries(callback: types.CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
        return

    lotteries = await get_active_lotteries()
    if not lotteries:
        await callback.message.edit_text("😕 Пока нет активных лотерей.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for lid, prize, price, total, taken in lotteries:
        builder.row(InlineKeyboardButton(
            text=f"🎁 {prize} | {price}₽ | {taken}/{total} слотов",
            callback_data=f"view_lottery_{lid}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main_menu"))

    await callback.message.edit_text(
        "🎲 <b>Активные лотереи</b>\n\nВыбери, чтобы посмотреть детали!",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_lottery_"))
async def view_lottery(callback: types.CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
        return

    lottery_id = int(callback.data.split("_")[2])
    lottery = await get_lottery(lottery_id)

    if not lottery:
        await callback.message.edit_text("❌ Лотерея не найдена.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    lid, prize, price, total, taken, status, winner_id, secret_seed, public_hash = lottery
    slots = await get_lottery_slots(lottery_id)

    text = f"🎁 <b>{prize}</b>\n\n💰 Цена слота: {price} ₽\n🎰 Занято: {taken}/{total} слотов\n\n"
    if slots:
        text += "👥 <b>Участники:</b>\n"
        for snum, uid, uname in slots:
            display = f"@{uname}" if uname else f"ID: {uid}"
            text += f"🎲 Слот #{snum}: {display}\n"

    user_id = callback.from_user.id
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM slots WHERE lottery_id = ? AND user_id = ? AND paid = 1",
            (lottery_id, user_id)
        )
        user_slots_count = (await cursor.fetchone())[0]

    if status == 'active' and taken > 0 and user_slots_count > 0:
        chance = user_slots_count / taken * 100
        text += f"\n🍀 <b>Твой шанс на победу:</b> {chance:.1f}% (у тебя {user_slots_count} слотов)"

    builder = InlineKeyboardBuilder()
    if status == 'active' and taken < total:
        builder.row(InlineKeyboardButton(text="🎲 Занять ещё один слот", callback_data=f"take_slot_{lottery_id}"))
    elif status == 'finished' and winner_id:
        winner_display = f"@{slots[0][2]}" if slots and slots[0][2] else f"ID: {winner_id}"
        text += f"\n\n🏆 <b>Победитель:</b> {winner_display}"
        builder.row(InlineKeyboardButton(text="📋 Посмотреть всех участников", callback_data=f"participants_{lottery_id}"))
    builder.row(InlineKeyboardButton(text="🔙 К списку лотерей", callback_data="list_lotteries"))

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("participants_"))
async def show_participants(callback: types.CallbackQuery):
    lottery_id = int(callback.data.split("_")[1])
    slots = await get_lottery_slots(lottery_id)
    lottery = await get_lottery(lottery_id)

    if not slots:
        await callback.answer("Нет участников.", show_alert=True)
        return

    text = f"📋 <b>Участники лотереи «{lottery[1]}»</b>\n\n"
    for snum, uid, uname in slots:
        display = f"@{uname}" if uname else f"ID: {uid}"
        text += f"🎲 Слот #{snum}: {display}\n"

    if lottery[6]:
        winner_display = f"@{slots[0][2]}" if slots and slots[0][2] else f"ID: {lottery[6]}"
        text += f"\n🏆 <b>Победитель:</b> {winner_display}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад к лотерее", callback_data=f"view_lottery_{lottery_id}"))

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("take_slot_"))
async def take_slot(callback: types.CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
        return

    lottery_id = int(callback.data.split("_")[2])
    lottery = await get_lottery(lottery_id)

    if not lottery or lottery[5] != 'active':
        await callback.answer("❌ Лотерея недоступна", show_alert=True)
        return

    if lottery[4] >= lottery[3]:
        await callback.answer("❌ Все слоты уже заняты", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username

    slot_number = await get_free_slot_number(lottery_id)
    if slot_number is None:
        await callback.answer("❌ Нет свободных слотов", show_alert=True)
        return

    free_slots = await get_user_free_slots(user_id)
    if free_slots > 0:
        if await use_free_slot(user_id):
            slot_id = await add_slot(lottery_id, user_id, username, slot_number)
            await mark_slot_paid(slot_id)
            await callback.message.edit_text(
                f"🎉 <b>Бесплатный слот использован!</b>\n\n"
                f"Лотерея: «{lottery[1]}»\n"
                f"🎲 Слот #{slot_number} активирован.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
            await callback.answer("✅ Бесплатный слот!", show_alert=True)

            if await check_lottery_full(lottery_id):
                await finish_lottery(lottery_id)
            return
        else:
            await callback.answer("❌ Лимит на сегодня исчерпан.", show_alert=True)
            return

    slot_id = await add_slot(lottery_id, user_id, username, slot_number)
    amount = lottery[2]
    payment_text = (
        f"💳 <b>Оплата слота #{slot_number}</b>\n\n"
        f"🏦 <b>Перевод на карту:</b> <code>{CARD_NUMBER}</code>\n"
        f"💰 <b>Сумма к оплате:</b> {amount} ₽\n\n"
        f"👇 После перевода нажми кнопку «Я оплатил».\n"
        f"Админ проверит и подтвердит слот."
    )

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{slot_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_lottery_{lottery_id}"))

    await callback.message.edit_text(payment_text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

async def finish_lottery(lottery_id: int):
    winner_id, winner_username, secret_seed = await pick_winner(lottery_id)
    if not winner_id:
        return

    lottery = await get_lottery(lottery_id)
    public_hash = lottery[8]
    slots = await get_lottery_slots(lottery_id)
    participants_text = "\n".join([f"Слот #{snum}: @{uname or uid}" for snum, uid, uname in slots])
    winner_display = f"@{winner_username}" if winner_username else f"ID: {winner_id}"

    verify_instruction = (
        "🔑 <b>Как проверить честность:</b>\n"
        "1. Зайди на сайт emn178.github.io/
