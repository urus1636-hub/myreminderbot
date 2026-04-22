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
BOT_TOKEN = "8568815241:AAFnvDf6lo6biwG6tHbG3Sy5iNQwFmU8hcg"
YOOMONEY_WALLET = "4100119518943796"
ADMIN_IDS = [1820245156]
COMMISSION_PERCENT = 20

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
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать лотерею", callback_data="admin_create"))
    builder.row(InlineKeyboardButton(text="📋 Все лотереи", callback_data="admin_list"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована")

async def save_user(user_id: int, username: str = None, first_name: str = None):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name)
        )
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
            "SELECT slot_number, user_id, username FROM slots WHERE lottery_id = ? AND paid = 1 ORDER BY slot_number",
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
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

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

    text = (
        f"📊 *Статистика:*\n\n"
        f"🎰 Всего лотерей: {total_lotteries}\n"
        f"🎲 Занято слотов: {total_slots}\n"
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
        for snum, uid, uname in slots:
            display = f"@{uname}" if uname else f"ID: {uid}"
            text += f"🎲 Слот #{snum}: {display}\n"

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

    slot_id = await add_slot(lottery_id, user_id, username, slot_number)

    amount = lottery[2]
    payment_link = f"https://yoomoney.ru/transfer/quickpay?requestId=slot_{slot_id}&sum={amount}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Оплатить слот", url=payment_link))
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{slot_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_lottery_{lottery_id}"))

    await callback.message.edit_text(
        f"🎲 Ты занимаешь слот #{slot_number} в лотерее «{lottery[1]}».\n\n"
        f"💰 Сумма к оплате: {amount} ₽\n\n"
        f"👇 Нажми кнопку ниже, чтобы оплатить. После оплаты нажми «Я оплатил».\n"
        f"⏳ Админ проверит оплату и подтвердит слот.",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment(callback: types.CallbackQuery):
    slot_id = int(callback.data.split("_")[2])

    slot_info = await get_slot_info(slot_id)
    if slot_info:
        user_id, username, lottery_id, prize_name, amount = slot_info
        user_display = f"@{username}" if username else f"ID: {user_id}"
        admin_text = (
            f"🔔 <b>Новая оплата ожидает подтверждения!</b>\n\n"
            f"👤 Пользователь: {user_display}\n"
            f"🎁 Лотерея: {prize_name}\n"
            f"💰 Сумма: {amount} ₽\n"
            f"🆔 ID слота: {slot_id}\n\n"
            f"Проверь поступление в ЮMoney и нажми кнопку ниже для подтверждения."
        )
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"approve_payment_{slot_id}"))
        builder.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{slot_id}"))
        await notify_admin(admin_text, builder.as_markup())

    await callback.message.edit_text(
        "⏳ Запрос на подтверждение оплаты отправлен админу.\n"
        "Как только админ подтвердит, твой слот будет активирован!",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer("✅ Запрос отправлен! Ожидай подтверждения.", show_alert=True)

@dp.callback_query(F.data.startswith("approve_payment_"))
async def approve_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только админ может подтверждать оплату", show_alert=True)
        return

    slot_id = int(callback.data.split("_")[2])
    await mark_slot_paid(slot_id)

    slot_info = await get_slot_info(slot_id)
    if slot_info:
        user_id, username, lottery_id, prize_name, amount = slot_info

        try:
            await bot.send_message(
                user_id,
                f"✅ Твоя оплата за слот в лотерее «{prize_name}» подтверждена!\n"
                f"🎲 Слот успешно активирован. Жди завершения розыгрыша!"
            )
        except:
            pass

        if await check_lottery_full(lottery_id):
            winner_id, winner_username, secret_seed = await pick_winner(lottery_id)
            if winner_id:
                lottery = await get_lottery(lottery_id)
                public_hash = lottery[8]
                slots = await get_lottery_slots(lottery_id)
                participants_text = "\n".join([f"Слот #{snum}: @{uname or uid}" for snum, uid, uname in slots])
                winner_display = f"@{winner_username}" if winner_username else f"ID: {winner_id}"

                # Инструкция для проверки
                verify_instruction = (
                    "🔑 <b>Как проверить честность:</b>\n"
                    "1. Зайди на сайт emn178.github.io/online-tools/sha256.html\n"
                    "2. В поле «Input» вставь секретный ключ ниже.\n"
                    "3. Настройки оставь по умолчанию (UTF-8, Hex).\n"
                    "4. Сравни «Output» с публичным хешем, который был объявлен до розыгрыша."
                )

                # Уведомление для всех участников
                async with aiosqlite.connect(DATABASE) as db:
                    cursor = await db.execute("SELECT DISTINCT user_id FROM slots WHERE lottery_id = ? AND paid = 1", (lottery_id,))
                    participants = await cursor.fetchall()

                for (uid,) in participants:
                    try:
                        await bot.send_message(
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
                    except:
                        pass

                # Отдельное сообщение победителю
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

                # Отчёт админу
                admin_report = (
                    f"🏆 <b>Лотерея «{prize_name}» завершена!</b>\n\n"
                    f"<b>Участники:</b>\n{participants_text}\n\n"
                    f"<b>Победитель:</b> {winner_display}\n"
                    f"<b>Секретный ключ:</b> {secret_seed}\n"
                    f"<b>Публичный хеш:</b> {public_hash}"
                )
                await notify_admin(admin_report)

    await callback.message.edit_text(f"✅ Оплата слота #{slot_id} подтверждена!")
    await callback.answer("Оплата подтверждена", show_alert=True)

@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только админ может отклонять оплату", show_alert=True)
        return

    slot_id = int(callback.data.split("_")[2])
    slot_info = await get_slot_info(slot_id)

    if slot_info:
        user_id, username, lottery_id, prize_name, amount = slot_info
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
            await db.commit()

        try:
            await bot.send_message(
                user_id,
                f"❌ Твоя оплата за слот в лотерее «{prize_name}» не подтверждена.\n"
                f"Возможно, деньги не поступили. Попробуй ещё раз или свяжись с админом."
            )
        except:
            pass

    await callback.message.edit_text(f"❌ Оплата слота #{slot_id} отклонена.")
    await callback.answer("Оплата отклонена", show_alert=True)

@dp.callback_query(F.data == "my_participations")
async def my_participations(callback: types.CallbackQuery):
    parts = await get_user_participations(callback.from_user.id)
    if not parts:
        await callback.message.edit_text("😕 Ты пока не участвовал в лотереях.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    text = "📊 *Твои участия:*\n\n"
    for lid, prize, slot, status, winner_id in parts:
        status_emoji = "🏆" if status == 'finished' and winner_id == callback.from_user.id else "⏳" if status == 'active' else "✅"
        text += f"{status_emoji} {prize} — Слот #{slot}\n"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

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
    asyncio.create_task(self_ping(PORT))
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
