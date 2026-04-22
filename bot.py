import asyncio
import logging
import os
import random
import time
import hashlib
import secrets
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

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8568815241:AAEr4IZhui7EUJO-F54-bx_Pb-W_ufU0WDM"
YOOMONEY_WALLET = "4100119518943796"
ADMIN_IDS = [1820245156]
COMMISSION_PERCENT = 20
REFERRAL_BONUS = 3

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
    builder.row(InlineKeyboardButton(text="🎲 Активные лотереи", callback_data="list_lotteries"))
    builder.row(InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations"))
    builder.row(InlineKeyboardButton(text="📋 Команды", callback_data="show_commands"))
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать лотерею", callback_data="admin_create"))
    builder.row(InlineKeyboardButton(text="📋 Все лотереи", callback_data="admin_list"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="📋 Команды", callback_data="show_commands"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return builder.as_markup()

def back_to_menu_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    return builder.as_markup()

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages_to_delete (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL
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
            if referrer_id:
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
        cursor = await db.execute("SELECT free_slots FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def use_free_slot(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT free_slots FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row and row[0] > 0:
            await db.execute("UPDATE users SET free_slots = free_slots - 1 WHERE user_id = ?", (user_id,))
            await db.commit()
            return True
        return False

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def add_message_to_delete(lottery_id: int, user_id: int, message_id: int, chat_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT INTO messages_to_delete (lottery_id, user_id, message_id, chat_id) VALUES (?, ?, ?, ?)",
            (lottery_id, user_id, message_id, chat_id)
        )
        await db.commit()

async def delete_old_lottery_messages(lottery_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT message_id, chat_id FROM messages_to_delete WHERE lottery_id = ?",
            (lottery_id,)
        )
        rows = await cursor.fetchall()
        for message_id, chat_id in rows:
            try:
                await bot.delete_message(chat_id, message_id)
            except:
                pass
        await db.execute("DELETE FROM messages_to_delete WHERE lottery_id = ?", (lottery_id,))
        await db.commit()

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
            "SELECT slot_number, user_id, username, paid FROM slots WHERE lottery_id = ? ORDER BY slot_number",
            (lottery_id,)
        )
        return await cursor.fetchall()

async def user_has_slot_in_lottery(lottery_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id FROM slots WHERE lottery_id = ? AND user_id = ?",
            (lottery_id, user_id)
        )
        row = await cursor.fetchone()
        return row is not None

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
            cursor = await db.execute("SELECT total_slots, taken_slots FROM lotteries WHERE id = ?", (lottery_id,))
            total, taken = await cursor.fetchone()
            if total - taken == 1:
                cursor = await db.execute("SELECT DISTINCT user_id FROM slots WHERE lottery_id = ? AND paid = 1", (lottery_id,))
                participants = await cursor.fetchall()
                lottery_name = await get_lottery_name(lottery_id)
                for (uid,) in participants:
                    try:
                        await bot.send_message(
                            uid,
                            f"⚡️ В лотерее «{lottery_name}» остался 1 слот! Торопись!"
                        )
                    except:
                        pass
        await db.commit()

async def get_lottery_name(lottery_id: int) -> str:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT prize_name FROM lotteries WHERE id = ?", (lottery_id,))
        row = await cursor.fetchone()
        return row[0] if row else ""

async def get_user_participations(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            SELECT s.lottery_id, l.prize_name, s.slot_number, l.status, l.winner_id, s.paid
            FROM slots s
            JOIN lotteries l ON s.lottery_id = l.id
            WHERE s.user_id = ?
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

async def get_all_winners():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            SELECT l.prize_name, s.username, s.user_id, l.created_at
            FROM lotteries l
            JOIN slots s ON l.winner_id = s.user_id AND l.id = s.lottery_id
            WHERE l.status = 'finished'
            ORDER BY l.created_at DESC
            LIMIT 20
        """)
        return await cursor.fetchall()

async def finish_lottery(lottery_id: int):
    winner_id, winner_username, secret_seed = await pick_winner(lottery_id)
    if not winner_id:
        return

    lottery = await get_lottery(lottery_id)
    public_hash = lottery[8]
    slots = await get_lottery_slots(lottery_id)
    participants_text = "\n".join([f"Слот #{snum}: @{uname or uid} ✅" for snum, uid, uname, paid in slots])
    winner_display = f"@{winner_username}" if winner_username else f"ID: {winner_id}"

    verify_instruction = (
        "🔑 <b>Как проверить честность:</b>\n"
        "1. Зайди на сайт emn178.github.io/online-tools/sha256.html\n"
        "2. В поле «Input» вставь секретный ключ ниже.\n"
        "3. Настройки оставь по умолчанию (UTF-8, Hex).\n"
        "4. Сравни «Output» с публичным хешем, который был объявлен до розыгрыша."
    )

    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT DISTINCT user_id FROM slots WHERE lottery_id = ? AND paid = 1", (lottery_id,))
        participants = await cursor.fetchall()

    for (uid,) in participants:
        try:
            msg = await bot.send_message(
                uid,
                f"🎉 Лотерея «{lottery[1]}» завершена!\n\n"
                f"🏆 Победитель: {winner_display}\n"
                f"🎁 Приз: {lottery[1]}\n\n"
                f"{verify_instruction}\n\n"
                f"🔒 <b>Публичный хеш:</b> <code>{public_hash}</code>\n"
                f"🔑 <b>Секретный ключ:</b> <code>{secret_seed}</code>",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
            await add_message_to_delete(lottery_id, uid, msg.message_id, uid)
        except:
            pass

    if winner_id:
        try:
            await bot.send_message(
                winner_id,
                f"🏆 <b>Поздравляю, ты победил в лотерее «{lottery[1]}»!</b>\n\n"
                f"🎁 Твой приз: {lottery[1]}\n\n"
                f"📩 Чтобы получить приз, напиши админу: @fourwayeu\n"
                f"Укажи ID лотереи: {lottery_id}",
                parse_mode="HTML"
            )
        except:
            pass

    await notify_admin(
        f"🏆 <b>Лотерея «{lottery[1]}» завершена!</b>\n\n"
        f"<b>Участники:</b>\n{participants_text}\n\n"
        f"<b>Победитель:</b> {winner_display}\n"
        f"<b>Секретный ключ:</b> {secret_seed}\n"
        f"<b>Публичный хеш:</b> {public_hash}"
    )

    await delete_old_lottery_messages(lottery_id)

# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, referrer_id)

    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "👋 Привет, Админ!\n\n"
            "🎲 Добро пожаловать в лотерейного бота.\n"
            "Здесь ты можешь создавать розыгрыши и управлять ими.",
            reply_markup=admin_menu_keyboard()
        )
    else:
        await message.answer(
            "👋 Привет!\n\n"
            "🎲 Это бот-лотерея. Участвуй в розыгрышах призов по честным правилам.\n"
            "💰 Выбери активную лотерею, займи слот, оплати — и жди результата!\n\n"
            "Победитель выбирается случайно, всё прозрачно.",
            reply_markup=main_menu_keyboard()
        )

@dp.message(Command("ref"))
async def cmd_ref(message: types.Message):
    user_id = message.from_user.id
    count = await get_referral_count(user_id)
    free_slots = await get_user_free_slots(user_id)
    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"

    text = (
        f"🔗 *Твоя реферальная ссылка:*\n`{ref_link}`\n\n"
        f"👥 Приглашено друзей: {count}/{REFERRAL_BONUS}\n"
        f"🎁 Бесплатных слотов: {free_slots}\n\n"
        f"Пригласи ещё {max(0, REFERRAL_BONUS - count)} друзей и получи бесплатный слот!"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("winners"))
async def cmd_winners(message: types.Message):
    winners = await get_all_winners()
    if not winners:
        await message.answer("🏆 Пока никто не выигрывал. Стань первым!")
        return

    text = "🏆 *Последние победители:*\n\n"
    for prize, username, user_id, created_at in winners[:10]:
        display = f"@{username}" if username else f"ID: {user_id}"
        text += f"🎁 {prize} — {display}\n"

    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "show_commands")
async def show_commands(callback: types.CallbackQuery):
    text = (
        "📋 *Доступные команды:*\n\n"
        "/start — главное меню\n"
        "/ref — реферальная ссылка\n"
        "/winners — список победителей\n"
        "/myid — узнать свой ID"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu_button())
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        await callback.message.edit_text("🏠 Главное меню", reply_markup=admin_menu_keyboard())
    else:
        await callback.message.edit_text("🏠 Главное меню", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    text = (
        "📌 *Как участвовать:*\n\n"
        "1️⃣ Выбери активную лотерею.\n"
        "2️⃣ Нажми «Занять слот» — бот выдаст ссылку на оплату.\n"
        "3️⃣ Оплати слот (банковской картой или ЮMoney).\n"
        "4️⃣ Нажми «Я оплатил» — админ проверит и подтвердит.\n"
        "5️⃣ Когда все слоты заняты, бот случайно выберет победителя.\n\n"
        "🎁 Победитель получает приз!\n\n"
        "🔒 *Честность:* бот заранее публикует хеш. После розыгрыша ты можешь проверить результат на сайте emn178.github.io/online-tools/sha256.html"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

# ---------- Админские команды ----------
@dp.callback_query(F.data == "admin_create")
async def admin_create_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(LotteryForm.waiting_for_prize)
    await callback.message.edit_text(
        "🎁 Введите название приза (например, «Discord Nitro 1 месяц»):",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(LotteryForm.waiting_for_prize)
async def admin_prize(message: types.Message, state: FSMContext):
    await state.update_data(prize_name=message.text.strip())
    await state.set_state(LotteryForm.waiting_for_price)
    await message.answer(
        "💰 Введите цену одного слота в рублях (например, 120):",
        reply_markup=back_to_menu_button()
    )

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
    await message.answer(
        "🎰 Введите количество слотов (например, 5):",
        reply_markup=back_to_menu_button()
    )

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
        f"✅ Лотерея создана!\n\n"
        f"🎁 Приз: {prize_name}\n"
        f"💰 Цена слота: {slot_price} ₽\n"
        f"🎰 Слотов: {slots}\n\n"
        f"🔒 <b>Хеш для проверки честности:</b> <code>{public_hash}</code>",
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

    text = "📋 *Активные лотереи:*\n\n"
    for lid, prize, price, total, taken in lotteries:
        text += f"🆔 {lid} | {prize}\n💰 {price}₽ | 🎰 {taken}/{total} слотов\n\n"

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_menu_keyboard())
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
        f"📊 *Статистика:*\n\n"
        f"🎰 Всего лотерей: {total_lotteries}\n"
        f"🎲 Занято слотов: {total_slots}\n"
        f"👥 Рефералов: {total_refs}\n"
        f"💰 Общий оборот: {total_revenue} ₽\n"
        f"💎 Твоя комиссия (20%): {commission} ₽"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_menu_keyboard())
    await callback.answer()

# ---------- Пользовательские функции ----------
@dp.callback_query(F.data == "list_lotteries")
async def list_lotteries(callback: types.CallbackQuery):
    lotteries = await get_active_lotteries()
    if not lotteries:
        await callback.message.edit_text("😕 Пока нет активных лотерей. Загляни позже!", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for lid, prize, price, total, taken in lotteries:
        builder.row(InlineKeyboardButton(
            text=f"{prize} | {price}₽ | {taken}/{total}",
            callback_data=f"view_lottery_{lid}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))

    await callback.message.edit_text("🎲 *Активные лотереи:*\n\nВыбери, чтобы посмотреть детали:", parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("view_lottery_"))
async def view_lottery(callback: types.CallbackQuery):
    lottery_id = int(callback.data.split("_")[2])
    lottery = await get_lottery(lottery_id)

    if not lottery:
        await callback.message.edit_text("❌ Лотерея не найдена.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    lid, prize, price, total, taken, status, winner_id, secret_seed, public_hash = lottery
    slots = await get_lottery_slots(lottery_id)

    text = f"🎁 *{prize}*\n\n💰 Цена слота: {price} ₽\n🎰 Слотов: {taken}/{total}\n\n"
    if slots:
        text += "👥 *Участники:*\n"
        for snum, uid, uname, paid in slots:
            display = f"@{uname}" if uname else f"ID: {uid}"
            paid_icon = "✅" if paid else "⏳"
            text += f"🎲 Слот #{snum}: {display} {paid_icon}\n"

    builder = InlineKeyboardBuilder()
    if status == 'active' and taken < total:
        builder.row(InlineKeyboardButton(text="🎲 Занять слот", callback_data=f"take_slot_{lottery_id}"))
    elif status == 'finished' and winner_id:
        winner_display = f"@{slots[0][2]}" if slots and slots[0][2] else f"ID: {winner_id}"
        text += f"\n🏆 *Победитель:* {winner_display}"
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="list_lotteries"))

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("take_slot_"))
async def take_slot(callback: types.CallbackQuery):
    lottery_id = int(callback.data.split("_")[2])
    lottery = await get_lottery(lottery_id)

    if not lottery or lottery[5] != 'active':
        await callback.answer("❌ Лотерея недоступна", show_alert=True)
        return

    if lottery[4] >= lottery[3]:
        await callback.answer("❌ Все слоты заняты", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username

    if await user_has_slot_in_lottery(lottery_id, user_id):
        await callback.answer("❌ Ты уже занял слот в этой лотерее!", show_alert=True)
        return

    slot_number = await get_free_slot_number(lottery_id)
    if slot_number is None:
        await callback.answer("❌ Нет свободных слотов", show_alert=True)
        return

    # Проверяем бесплатные слоты
    free_slots = await get_user_free_slots(user_id)
    if free_slots > 0:
        await use_free_slot(user_id)
        slot_id = await add_slot(lottery_id, user_id, username, slot_number)
        await mark_slot_paid(slot_id)
        await callback.message.edit_text(
            f"🎉 Ты использовал бесплатный слот в лотерее «{lottery[1]}»!\n"
            f"🎲 Слот #{slot_number} активирован. Жди завершения розыгрыша!",
            reply_markup=main_menu_keyboard()
        )
        await callback.answer("✅ Бесплатный слот использован!", show_alert=True)

        if await check_lottery_full(lottery_id):
            await finish_lottery(lottery_id)
        return

    slot_id = await add_slot(lottery_id, user_id, username, slot_number)
    amount = lottery[2]
    payment_link = f"https://yoomoney.ru/transfer/quickpay?requestId=slot_{slot_id}&sum={amount}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Оплатить слот", url=payment_link))
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{slot_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_lottery_{lottery_id}"))

    await callback.message.edit
