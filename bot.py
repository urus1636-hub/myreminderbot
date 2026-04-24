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
CARD_NUMBER = "2202208426435781"
CHANNEL_ID = "@luckyfortune4"
ADMIN_IDS = [1820245156]
COMMISSION_PERCENT = 20
REFERRAL_BONUS = 5
MAX_FREE_SLOTS_PER_DAY = 1

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE = os.path.join(DATA_DIR, "lottery.db")
PORT = 8000

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
    edit_value = State()

# ---------- КЛАВИАТУРЫ ----------
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎲 Активные лотереи", callback_data="list_lotteries"))
    builder.row(InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations"))
    builder.row(InlineKeyboardButton(text="👥 Рефералы", callback_data="ref_info"))
    return builder.as_markup()

def admin_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать лотерею", callback_data="admin_create"))
    builder.row(InlineKeyboardButton(text="📋 Управление", callback_data="admin_list"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="👥 Моя рефералка", callback_data="ref_info"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return builder.as_markup()

def back_btn():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    return builder.as_markup()

def subscribe_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Перейти на канал", url="https://t.me/luckyfortune4"))
    builder.row(InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription"))
    return builder.as_markup()

async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["creator", "administrator", "member"]
    except:
        return True

# ---------- БАЗА ДАННЫХ ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS lotteries (id INTEGER PRIMARY KEY AUTOINCREMENT, prize_name TEXT, slot_price INTEGER, total_slots INTEGER, taken_slots INTEGER DEFAULT 0, status TEXT DEFAULT 'active', winner_id INTEGER, secret_seed TEXT, public_hash TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id INTEGER, user_id INTEGER, username TEXT, paid INTEGER DEFAULT 0, slot_number INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referrer_id INTEGER, free_slots INTEGER DEFAULT 0, last_free_slot_used DATE)")
        await db.execute("CREATE TABLE IF NOT EXISTS referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_id INTEGER)")
        await db.commit()

async def save_user(user_id, username=None, first_name=None, referrer_id=None):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?,?,?,?)", (user_id, username, first_name, referrer_id))
            if referrer_id and referrer_id != user_id:
                await db.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (?,?)", (referrer_id, user_id))
                cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (referrer_id,))
                if (await cur.fetchone())[0] >= REFERRAL_BONUS:
                    await db.execute("UPDATE users SET free_slots=free_slots+1 WHERE user_id=?", (referrer_id,))
        else:
            await db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
        await db.commit()

async def get_user_free_slots(user_id):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT free_slots, last_free_slot_used FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row and row[0] > 0:
            return 0 if row[1] == date.today() else row[0]
        return 0

async def use_free_slot(user_id):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT free_slots FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row and row[0] > 0:
            await db.execute("UPDATE users SET free_slots=free_slots-1, last_free_slot_used=? WHERE user_id=?", (date.today(), user_id))
            await db.commit()
            return True
        return False

async def get_referral_count(user_id):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,))
        return (await cur.fetchone())[0]

async def create_lottery(name, price, total):
    seed = secrets.token_hex(16)
    h = hashlib.sha256(seed.encode()).hexdigest()
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("INSERT INTO lotteries (prize_name, slot_price, total_slots, secret_seed, public_hash, status, taken_slots) VALUES (?,?,?,?,?,?,?)", 
                               (name, price, total, seed, h, 'active', 0))
        await db.commit()
        return cur.lastrowid, seed, h

async def get_active_lotteries():
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT id, prize_name, slot_price, total_slots, taken_slots FROM lotteries WHERE status = 'active' AND taken_slots < total_slots ORDER BY id DESC")
        return await cur.fetchall()

async def get_all_lotteries():
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT id, prize_name, slot_price, total_slots, taken_slots, status FROM lotteries ORDER BY id DESC")
        return await cur.fetchall()

async def get_lottery(lid):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT * FROM lotteries WHERE id=?", (lid,))
        return await cur.fetchone()

async def get_lottery_slots(lid):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT slot_number, user_id, username FROM slots WHERE lottery_id=? AND paid=1 ORDER BY slot_number", (lid,))
        return await cur.fetchall()

async def add_slot(lid, uid, uname, snum):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("INSERT INTO slots (lottery_id, user_id, username, slot_number) VALUES (?,?,?,?)", (lid, uid, uname, snum))
        await db.commit()
        return cur.lastrowid

async def mark_slot_paid(sid):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE slots SET paid=1 WHERE id=?", (sid,))
        cur = await db.execute("SELECT lottery_id FROM slots WHERE id=?", (sid,))
        row = await cur.fetchone()
        if row:
            lid = row[0]
            cur = await db.execute("SELECT COUNT(*) FROM slots WHERE lottery_id=? AND paid=1", (lid,))
            taken = (await cur.fetchone())[0]
            await db.execute("UPDATE lotteries SET taken_slots=? WHERE id=?", (taken, lid))
        await db.commit()

async def get_user_parts(uid):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT s.lottery_id, l.prize_name, s.slot_number, l.status, l.winner_id FROM slots s JOIN lotteries l ON s.lottery_id=l.id WHERE s.user_id=? AND s.paid=1 ORDER BY s.rowid DESC", (uid,))
        return await cur.fetchall()

async def get_free_slot(lid):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT slot_number FROM slots WHERE lottery_id=? AND paid=1", (lid,))
        taken = [r[0] for r in await cur.fetchall()]
        cur = await db.execute("SELECT total_slots FROM lotteries WHERE id=?", (lid,))
        total = (await cur.fetchone())[0]
        for i in range(1, total+1):
            if i not in taken:
                return i
        return None

async def is_full(lid):
    l = await get_lottery(lid)
    if not l:
        return False
    return l[4] >= l[3]

async def pick_winner(lid):
    l = await get_lottery(lid)
    if not l:
        return None, None, None
    random.seed(l[7])
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT user_id, username FROM slots WHERE lottery_id=? AND paid=1", (lid,))
        slots = await cur.fetchall()
        if not slots:
            return None, None, None
        w = random.choice(slots)
        await db.execute("UPDATE lotteries SET status='finished', winner_id=? WHERE id=?", (w[0], lid))
        await db.commit()
        return w[0], w[1], l[7]

async def get_slot_info(sid):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT s.user_id, s.username, s.lottery_id, l.prize_name, l.slot_price FROM slots s JOIN lotteries l ON s.lottery_id=l.id WHERE s.id=?", (sid,))
        return await cur.fetchone()

async def notify_admin(text, markup=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML", reply_markup=markup)
        except:
            pass

async def update_lottery_field(lid, field, value):
    async with aiosqlite.connect(DATABASE) as db:
        if field == "name":
            await db.execute("UPDATE lotteries SET prize_name=? WHERE id=?", (value, lid))
        elif field == "price":
            await db.execute("UPDATE lotteries SET slot_price=? WHERE id=?", (int(value), lid))
        elif field == "total":
            await db.execute("UPDATE lotteries SET total_slots=? WHERE id=?", (int(value), lid))
        await db.commit()

async def delete_lottery(lid):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("DELETE FROM slots WHERE lottery_id=?", (lid,))
        await db.execute("DELETE FROM lotteries WHERE id=?", (lid,))
        await db.commit()

# ---------- КОМАНДЫ ----------
@dp.message(Command("start"))
async def start(message: types.Message):
    args = message.text.split()
    ref = int(args[1]) if len(args)>1 and args[1].isdigit() else None
    await save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, ref)
    
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "📢 <b>Подпишись на канал</b>\n\nЧтобы участвовать в лотереях, подпишись:\n" + CHANNEL_ID + "\n\nПосле подписки нажми /start",
            parse_mode="HTML",
            reply_markup=subscribe_keyboard()
        )
        return
    
    text = (
        "🎲 <b>LUCKY FORTUNE — лотерея, которую можно проверить</b>\n\n"
        "Обычные лотереи в ТГ — чёрный ящик. Кто выиграл — хер узнаешь. Тут по-другому.\n\n"
        "<b>🔒 Как проверяется честность</b>\n"
        "Перед стартом лотереи я публикую SHA256-хеш — зашифрованный слепок результата.\n"
        "Ты покупаешь слот. Видишь всех участников.\n"
        "После розыгрыша я даю секретный ключ.\n"
        "Ты сам вбиваешь ключ в любой SHA256-калькулятор — и сравниваешь с моим хешем.\n"
        "Совпало? Значит, я не мухлевал. Не совпало? Закрываю проект.\n\n"
        "<b>👥 Реферальная система</b>\n"
        "Пригласил 5 друзей — получил 1 бесплатный слот каждый день.\n"
        "Да, каждый день. Серьёзно.\n\n"
        "<b>💳 Оплата</b>\n"
        "Переводом на карту. Без посредников. Нажал «Я оплатил» — админ подтвердил — слот твой.\n\n"
        "👇 <b>Выбирай лотерею и пробуй</b>"
    )
    
    if message.from_user.id in ADMIN_IDS:
        await message.answer(text + "\n\n👑 Ты админ. Панель управления ниже.", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data=="check_subscription")
async def check_sub(call: types.CallbackQuery):
    if await is_subscribed(call.from_user.id):
        text = "🎲 <b>LUCKY FORTUNE — лотерея, которую можно проверить</b>\n\nОбычные лотереи в ТГ — чёрный ящик. Кто выиграл — хер узнаешь. Тут по-другому.\n\n<b>🔒 Как проверяется честность</b>\nПеред стартом лотереи я публикую SHA256-хеш — зашифрованный слепок результата.\nТы покупаешь слот. Видишь всех участников.\nПосле розыгрыша я даю секретный ключ.\nТы сам вбиваешь ключ в любой SHA256-калькулятор — и сравниваешь с моим хешем.\nСовпало? Значит, я не мухлевал. Не совпало? Закрываю проект.\n\n<b>👥 Реферальная система</b>\nПригласил 5 друзей — получил 1 бесплатный слот каждый день.\nДа, каждый день. Серьёзно.\n\n<b>💳 Оплата</b>\nПереводом на карту. Без посредников. Нажал «Я оплатил» — админ подтвердил — слот твой.\n\n👇 <b>Выбирай лотерею и пробуй</b>"
        if call.from_user.id in ADMIN_IDS:
            await call.message.edit_text(text + "\n\n👑 Ты админ. Панель управления ниже.", parse_mode="HTML", reply_markup=admin_menu_keyboard())
        else:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    else:
        await call.answer("❌ Ты ещё не подписался!", show_alert=True)

@dp.message(Command("ref"))
async def ref(message: types.Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer("❌ Сначала подпишись!", reply_markup=subscribe_keyboard())
        return
    uid = message.from_user.id
    cnt = await get_referral_count(uid)
    free = await get_user_free_slots(uid)
    uname = (await bot.me()).username
    link = f"https://t.me/{uname}?start={uid}"
    await message.answer(f"🔗 <b>Твоя реферальная ссылка</b>\n<code>{link}</code>\n\n👥 Приглашено: {cnt}/{REFERRAL_BONUS}\n🎁 Бесплатных слотов: {free}\n⚠️ 1 слот в сутки", parse_mode="HTML")

@dp.callback_query(F.data=="ref_info")
async def ref_info(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Сначала подпишись!", show_alert=True)
        return
    uid = call.from_user.id
    cnt = await get_referral_count(uid)
    free = await get_user_free_slots(uid)
    uname = (await bot.me()).username
    link = f"https://t.me/{uname}?start={uid}"
    await call.message.edit_text(f"🔗 <b>Твоя реферальная ссылка</b>\n<code>{link}</code>\n\n👥 Приглашено: {cnt}/{REFERRAL_BONUS}\n🎁 Бесплатных слотов: {free}\n⚠️ 1 слот в сутки", parse_mode="HTML", reply_markup=back_btn())
    await call.answer()

@dp.callback_query(F.data=="main_menu")
async def menu(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.message.edit_text("📢 Подпишись на канал!", reply_markup=subscribe_keyboard())
        await call.answer()
        return
    text = "🎲 <b>LUCKY FORTUNE — лотерея, которую можно проверить</b>\n\nОбычные лотереи в ТГ — чёрный ящик. Кто выиграл — хер узнаешь. Тут по-другому.\n\n<b>🔒 Как проверяется честность</b>\nПеред стартом лотереи я публикую SHA256-хеш — зашифрованный слепок результата.\nТы покупаешь слот. Видишь всех участников.\nПосле розыгрыша я даю секретный ключ.\nТы сам вбиваешь ключ в любой SHA256-калькулятор — и сравниваешь с моим хешем.\nСовпало? Значит, я не мухлевал. Не совпало? Закрываю проект.\n\n<b>👥 Реферальная система</b>\nПригласил 5 друзей — получил 1 бесплатный слот каждый день.\nДа, каждый день. Серьёзно.\n\n<b>💳 Оплата</b>\nПереводом на карту. Без посредников. Нажал «Я оплатил» — админ подтвердил — слот твой.\n\n👇 <b>Выбирай лотерею и пробуй</b>"
    if call.from_user.id in ADMIN_IDS:
        await call.message.edit_text(text + "\n\n👑 Ты админ.", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    else:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    await call.answer()

# ---------- АДМИН-ФУНКЦИИ ----------
@dp.callback_query(F.data=="admin_create")
async def admin_create(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(LotteryForm.waiting_for_prize)
    await call.message.edit_text("🎁 <b>Создание лотереи</b>\n\nВведите название приза:", parse_mode="HTML", reply_markup=back_btn())
    await call.answer()

@dp.message(LotteryForm.waiting_for_prize)
async def prize(message: types.Message, state: FSMContext):
    await state.update_data(prize_name=message.text.strip())
    await state.set_state(LotteryForm.waiting_for_price)
    await message.answer("💰 Введите цену слота в рублях:", reply_markup=back_btn())

@dp.message(LotteryForm.waiting_for_price)
async def price(message: types.Message, state: FSMContext):
    try:
        p = int(message.text.strip())
        if p <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите целое положительное число.")
        return
    await state.update_data(slot_price=p)
    await state.set_state(LotteryForm.waiting_for_slots)
    await message.answer("🎰 Введите количество слотов:", reply_markup=back_btn())

@dp.message(LotteryForm.waiting_for_slots)
async def slots(message: types.Message, state: FSMContext):
    try:
        s = int(message.text.strip())
        if s <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите целое положительное число.")
        return
    data = await state.get_data()
    lid, seed, h = await create_lottery(data["prize_name"], data["slot_price"], s)
    await message.answer(f"✅ <b>Лотерея создана!</b>\n\n🎁 Приз: {data['prize_name']}\n💰 Цена слота: {data['slot_price']} ₽\n🎰 Слотов: {s}\n\n🔒 <b>Хеш:</b> <code>{h}</code>\n\n⚠️ Лотерея появится в списке активных, когда начнётся приём слотов.", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data=="admin_list")
async def admin_list(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    lots = await get_all_lotteries()
    if not lots:
        await call.message.edit_text("Нет ни одной лотереи.", reply_markup=admin_menu_keyboard())
        await call.answer()
        return
    builder = InlineKeyboardBuilder()
    for l in lots:
        status = "🟢" if l[5]=='active' else "🔴"
        builder.row(InlineKeyboardButton(text=f"{status} {l[1]} | {l[2]}₽ | {l[4]}/{l[3]}", callback_data=f"admview_{l[0]}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await call.message.edit_text("📋 <b>Все лотереи</b>\n\nВыбери для управления:", parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("admview_"))
async def admview(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    lid = int(call.data.split("_")[1])
    l = await get_lottery(lid)
    if not l:
        await call.answer("❌ Лотерея не найдена", show_alert=True)
        return
    slots = await get_lottery_slots(lid)
    text = f"🎁 <b>{l[1]}</b>\n💰 Цена: {l[2]}₽\n🎰 Слоты: {l[4]}/{l[3]}\n📌 Статус: {l[5]}\n🆔 ID: {l[0]}\n\n"
    if slots:
        text += "👥 <b>Участники:</b>\n"
        for sn, uid, un in slots:
            text += f"🎲 Слот #{sn}: @{un or uid}\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_name_{lid}"), InlineKeyboardButton(text="💰 Цену", callback_data=f"edit_price_{lid}"))
    builder.row(InlineKeyboardButton(text="🎰 Кол-во", callback_data=f"edit_total_{lid}"), InlineKeyboardButton(text="👥 Участники", callback_data=f"parts_{lid}"))
    builder.row(InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{lid}"))
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="admin_list"))
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("edit_"))
async def edit_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = call.data.split("_")
    field = parts[1]
    lid = int(parts[2])
    names = {"name": "название приза", "price": "цену слота", "total": "количество слотов"}
    await state.update_data(edit_lid=lid, edit_field=field)
    await state.set_state(LotteryForm.edit_value)
    await call.message.edit_text(f"✏️ Введите новое значение для поля «{names.get(field, field)}»:", reply_markup=back_btn())
    await call.answer()

@dp.message(LotteryForm.edit_value)
async def edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lid = data.get("edit_lid")
    field = data.get("edit_field")
    
    if not lid or not field:
        await message.answer("❌ Ошибка: лотерея не найдена. Попробуй снова.", reply_markup=admin_menu_keyboard())
        await state.clear()
        return
    
    val = message.text.strip()
    
    if field in ["price", "total"]:
        try:
            val = int(val)
            if val <= 0:
                raise ValueError
        except:
            await message.answer("❌ Введите целое положительное число.")
            return
    
    await update_lottery_field(lid, field, val)
    
    updated_lot = await get_lottery(lid)
    if updated_lot:
        await message.answer(f"✅ Лотерея обновлена!\n\n🎁 Название: {updated_lot[1]}\n💰 Цена: {updated_lot[2]}₽\n🎰 Слотов: {updated_lot[3]}", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    else:
        await message.answer("✅ Лотерея обновлена!", reply_markup=admin_menu_keyboard())
    
    await state.clear()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_lot(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    lid = int(call.data.split("_")[1])
    await delete_lottery(lid)
    await call.message.edit_text(f"✅ Лотерея удалена.", reply_markup=admin_menu_keyboard())
    await call.answer("Удалено", show_alert=True)

@dp.callback_query(F.data=="admin_stats")
async def admin_stats(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM lotteries")
        tl = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM slots WHERE paid=1")
        ts = (await cur.fetchone())[0]
        cur = await db.execute("SELECT SUM(l.slot_price) FROM slots s JOIN lotteries l ON s.lottery_id=l.id WHERE s.paid=1")
        tr = (await cur.fetchone())[0] or 0
        com = int(tr * COMMISSION_PERCENT / 100)
        cur = await db.execute("SELECT COUNT(*) FROM referrals")
        refs = (await cur.fetchone())[0]
    await call.message.edit_text(f"📊 <b>Статистика</b>\n\n🎰 Лотерей: {tl}\n🎲 Слотов продано: {ts}\n👥 Рефералов: {refs}\n💰 Оборот: {tr} ₽\n💎 Прибыль: {com} ₽", parse_mode="HTML", reply_markup=admin_menu_keyboard())
    await call.answer()

# ---------- ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ----------
@dp.callback_query(F.data=="list_lotteries")
async def list_lotteries(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Сначала подпишись!", show_alert=True)
        return
    lots = await get_active_lotteries()
    if not lots:
        await call.message.answer("😕 Пока нет активных лотерей.", reply_markup=main_menu_keyboard())
        await call.message.delete()
        await call.answer()
        return
    builder = InlineKeyboardBuilder()
    for l in lots:
        builder.row(InlineKeyboardButton(text=f"🎁 {l[1]} | {l[2]}₽ | {l[4]}/{l[3]} слотов", callback_data=f"view_{l[0]}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await call.message.edit_text("🎲 <b>Активные лотереи</b>\n\nВыбери:", parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_lottery(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Сначала подпишись!", show_alert=True)
        return
    lid = int(call.data.split("_")[1])
    l = await get_lottery(lid)
    if not l:
        await call.message.edit_text("❌ Лотерея не найдена.", reply_markup=main_menu_keyboard())
        await call.answer()
        return
    slots = await get_lottery_slots(lid)
    text = f"🎁 <b>{l[1]}</b>\n\n💰 Цена: {l[2]} ₽\n🎰 Занято: {l[4]}/{l[3]} слотов\n\n"
    if slots:
        text += "👥 <b>Участники:</b>\n"
        for sn, uid, un in slots:
            text += f"🎲 Слот #{sn}: @{un or uid}\n"
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM slots WHERE lottery_id=? AND user_id=? AND paid=1", (lid, call.from_user.id))
        my = (await cur.fetchone())[0]
    if l[5]=='active' and l[4]>0 and my>0:
        text += f"\n🍀 <b>Твой шанс:</b> {my/l[4]*100:.1f}% ({my} слотов)"
    builder = InlineKeyboardBuilder()
    if l[5]=='active' and l[4]<l[3]:
        builder.row(InlineKeyboardButton(text="🎲 Занять слот", callback_data=f"take_{lid}"))
    elif l[5]=='finished' and l[6]:
        text += f"\n\n🏆 <b>Победитель:</b> @{slots[0][2] if slots else f'ID:{l[6]}'}"
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="list_lotteries"))
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("take_"))
async def take(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Сначала подпишись!", show_alert=True)
        return
    lid = int(call.data.split("_")[1])
    l = await get_lottery(lid)
    if not l or l[5]!='active':
        await call.answer("❌ Лотерея недоступна", show_alert=True)
        return
    if l[4]>=l[3]:
        await call.answer("❌ Все слоты заняты", show_alert=True)
        return
    sn = await get_free_slot(lid)
    if sn is None:
        await call.answer("❌ Нет свободных слотов", show_alert=True)
        return
    uid = call.from_user.id
    un = call.from_user.username
    free = await get_user_free_slots(uid)
    if free>0:
        if await use_free_slot(uid):
            sid = await add_slot(lid, uid, un, sn)
            await mark_slot_paid(sid)
            await call.message.edit_text(f"🎉 <b>Бесплатный слот!</b>\n\nЛотерея: «{l[1]}»\n🎲 Слот #{sn} активирован.", parse_mode="HTML", reply_markup=main_menu_keyboard())
            await call.answer("✅ Бесплатный слот активирован!", show_alert=True)
            if await is_full(lid):
                await finish_lottery(lid)
            return
        else:
            await call.answer("❌ Лимит на сегодня", show_alert=True)
            return
    sid = await add_slot(lid, uid, un, sn)
    amt = l[2]
    txt = f"💳 <b>Оплата слота #{sn}</b>\n\n🏦 <b>Перевод на карту:</b> <code>{CARD_NUMBER}</code>\n💰 <b>Сумма:</b> {amt} ₽\n\n👇 После перевода нажми «Я оплатил»"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"pay_{sid}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_{lid}"))
    await call.message.edit_text(txt, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

async def finish_lottery(lid):
    wid, wname, seed = await pick_winner(lid)
    if not wid:
        return
    l = await get_lottery(lid)
    if not l:
        return
    h = l[8]
    slots = await get_lottery_slots(lid)
    parts_text = "\n".join([f"Слот #{sn}: @{un or uid}" for sn, uid, un in slots])
    wdisp = f"@{wname}" if wname else f"ID:{wid}"
    vinstr = (
        "🔑 <b>Как проверить честность:</b>\n"
        "1. Зайди на emn178.github.io/online-tools/sha256.html\n"
        "2. Вставь секретный ключ в поле Input\n"
        "3. Настройки: UTF-8, Hex\n"
        "4. Сравни Output с публичным хешем"
    )
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT DISTINCT user_id FROM slots WHERE lottery_id=? AND paid=1", (lid,))
        for (uid,) in await cur.fetchall():
            try:
                await bot.send_message(uid, f"🎉 <b>Лотерея «{l[1]}» завершена!</b>\n\n🏆 Победитель: {wdisp}\n\n{vinstr}\n\n🔒 <b>Хеш:</b> <code>{h}</code>\n🔑 <b>Ключ:</b> <code>{seed}</code>", parse_mode="HTML", reply_markup=main_menu_keyboard())
            except:
                pass
    try:
        await bot.send_message(wid, f"🏆 <b>Поздравляю, ты победил!</b>\n\n🎁 Приз: {l[1]}\n\n📩 Получить приз: @fourwayeu\nID лотереи: {lid}", parse_mode="HTML")
    except:
        pass
    await notify_admin(f"🏆 <b>Лотерея «{l[1]}» завершена!</b>\n\n<b>Участники:</b>\n{parts_text}\n\n<b>Победитель:</b> {wdisp}\n<b>Ключ:</b> {seed}\n<b>Хеш:</b> {h}")

@dp.callback_query(F.data.startswith("pay_"))
async def pay(call: types.CallbackQuery):
    sid = int(call.data.split("_")[1])
    info = await get_slot_info(sid)
    if info:
        uid, uname, lid, prize, amt = info
        udisp = f"@{uname}" if uname else f"ID:{uid}"
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr_{sid}"))
        builder.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{sid}"))
        await notify_admin(f"🔔 <b>Новая оплата!</b>\n\n👤 {udisp}\n🎁 {prize}\n💰 {amt} ₽\n🆔 Слот: {sid}", builder.as_markup())
    await call.message.edit_text("⏳ Запрос отправлен админу. После подтверждения слот активируется.", reply_markup=main_menu_keyboard())
    await call.answer("✅ Отправлено!", show_alert=True)

@dp.callback_query(F.data.startswith("appr_"))
async def appr(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Только админ", show_alert=True)
        return
    sid = int(call.data.split("_")[1])
    await mark_slot_paid(sid)
    info = await get_slot_info(sid)
    if info:
        uid, uname, lid, prize, amt = info
        try:
            await bot.send_message(uid, f"✅ Оплата подтверждена! Слот в лотерее «{prize}» активирован.")
        except:
            pass
        if await is_full(lid):
            await finish_lottery(lid)
    await call.message.edit_text(f"✅ Оплата слота #{sid} подтверждена!")
    await call.answer("Подтверждено", show_alert=True)

@dp.callback_query(F.data.startswith("rej_"))
async def rej(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Только админ", show_alert=True)
        return
    sid = int(call.data.split("_")[1])
    info = await get_slot_info(sid)
    if info:
        uid, uname, lid, prize, amt = info
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("DELETE FROM slots WHERE id=?", (sid,))
            await db.commit()
        try:
            await bot.send_message(uid, f"❌ Оплата не подтверждена. Слот не активирован.")
        except:
            pass
    await call.message.edit_text(f"❌ Оплата слота #{sid} отклонена.")
    await call.answer("Отклонено", show_alert=True)

@dp.callback_query(F.data=="my_participations")
async def my_parts(call: types.CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Сначала подпишись!", show_alert=True)
        return
    parts = await get_user_parts(call.from_user.id)
    if not parts:
        await call.message.edit_text("😕 Ты пока не участвовал.", reply_markup=main_menu_keyboard())
        await call.answer()
        return
    text = "📊 <b>Твои участия</b>\n\n"
    for lid, prize, sn, status, wid in parts:
        emoji = "🏆" if status=='finished' and wid==call.from_user.id else "⏳" if status=='active' else "✅"
        text += f"{emoji} {prize} — Слот #{sn}\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("parts_"))
async def parts(call: types.CallbackQuery):
    lid = int(call.data.split("_")[1])
    slots = await get_lottery_slots(lid)
    l = await get_lottery(lid)
    if not l:
        await call.answer("Лотерея не найдена", show_alert=True)
        return
    t = f"📋 <b>Участники лотереи «{l[1]}»</b>\n\n"
    if slots:
        for sn, uid, un in slots:
            t += f"🎲 Слот #{sn}: @{un or uid}\n"
    else:
        t += "Нет участников.\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_{lid}"))
    await call.message.edit_text(t, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()

# ---------- ВЕБ-СЕРВЕР ----------
async def healthcheck(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Web server on port {PORT}")

async def self_ping(port):
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://0.0.0.0:{port}/") as resp:
                    logging.info(f"Ping: {resp.status}")
        except:
            pass
        await asyncio.sleep(300)

# ---------- ЗАПУСК ----------
async def main():
    await init_db()
    asyncio.create_task(self_ping(PORT))
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
