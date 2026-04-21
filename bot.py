import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil import parser
import aiosqlite

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8651845065:AAF5UKZ_zJ5zm12ykMNxT3quEaIgZqnhL9k"  # Твой токен
DATABASE = "reminders.db"
# ================================

logging.basicConfig(level=logging.INFO)

# Инициализация бота (без прокси!)
bot = Bot(token=BOT_TOKEN)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# FSM для создания напоминания
class ReminderForm(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()

async def init_db():
    """Создаёт таблицу в БД, если её нет."""
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

async def add_reminder(user_id: int, text: str, remind_at: datetime):
    """Добавляет напоминание в БД и возвращает его ID."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)",
            (user_id, text, remind_at)
        )
        await db.commit()
        return cursor.lastrowid

async def get_user_reminders(user_id: int):
    """Возвращает список всех напоминаний пользователя."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_at FROM reminders WHERE user_id = ? ORDER BY remind_at",
            (user_id,)
        )
        return await cursor.fetchall()

async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    """Удаляет напоминание. Возвращает True, если удалено."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def load_scheduled_jobs():
    """Загружает все напоминания из БД и ставит их в планировщик при старте."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, user_id, text, remind_at FROM reminders")
        rows = await cursor.fetchall()
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
        logging.info(f"Загружено {len(rows)} напоминаний из БД.")

async def send_reminder(user_id: int, text: str, reminder_id: int):
    """Отправляет напоминание пользователю и удаляет его из БД."""
    try:
        await bot.send_message(
            user_id,
            f"⏰ НАПОМИНАНИЕ!\n\n"
            f"Ты просил напомнить:\n"
            f"📝 «{text}»"
        )
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
    finally:
        await delete_reminder(reminder_id, user_id)

# ========== КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот-напоминалка.\n\n"
        "Просто напиши, о чём напомнить, и я спрошу когда.\n"
        "Команды:\n"
        "/list — мои напоминания\n"
        "/delete — удалить напоминание\n"
        "/edit — изменить время напоминания"
    )

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    reminders = await get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У тебя нет активных напоминаний.")
        return
    lines = []
    for rem_id, text, remind_at in reminders:
        dt = datetime.fromisoformat(remind_at)
        lines.append(f"🆔 {rem_id} | {dt.strftime('%d.%m.%Y %H:%M')} — {text[:30]}...")
    await message.answer("📋 Твои напоминания:\n" + "\n".join(lines))

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message, state: FSMContext):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /delete <ID>\nID можно узнать в /list")
        return
    try:
        rem_id = int(args[1])
    except ValueError:
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
        await message.answer("❌ Напоминание не найдено или не принадлежит тебе.")

@dp.message(Command("edit"))
async def cmd_edit(message: types.Message, state: FSMContext):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /edit <ID>\nID можно узнать в /list")
        return
    try:
        rem_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    reminders = await get_user_reminders(message.from_user.id)
    target = None
    for r in reminders:
        if r[0] == rem_id:
            target = r
            break
    if not target:
        await message.answer("❌ Напоминание не найдено или не принадлежит тебе.")
        return

    await state.update_data(edit_id=rem_id, old_text=target[1])
    await state.set_state("edit_time")
    await message.answer(
        f"Текущее напоминание: «{target[1]}» на {datetime.fromisoformat(target[2]).strftime('%d.%m.%Y %H:%M')}\n"
        f"Введи новое время в формате ДД.ММ.ГГГГ ЧЧ:ММ"
    )

@dp.message(StateFilter("edit_time"))
async def process_edit_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        new_time = parser.parse(time_str, dayfirst=True)
        if new_time < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее время.")
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй: ДД.ММ.ГГГГ ЧЧ:ММ")
        return

    data = await state.get_data()
    rem_id = data.get("edit_id")
    old_text = data.get("old_text")
    user_id = message.from_user.id

    # Удаляем старое и создаём новое
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
    await message.answer(f"✅ Время обновлено! Новое напоминание в {new_time.strftime('%d.%m.%Y %H:%M')}")
    await state.clear()

@dp.message(StateFilter(None))
async def handle_any_text(message: types.Message, state: FSMContext):
    # Первое сообщение – текст напоминания
    await state.update_data(reminder_text=message.text)
    await state.set_state(ReminderForm.waiting_for_time)
    await message.answer(
        "📅 Когда тебе напомнить?\n"
        "Введи дату и время в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        "Например: 25.12.2026 15:30"
    )

@dp.message(ReminderForm.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        remind_at = parser.parse(time_str, dayfirst=True)
        if remind_at < datetime.now():
            await message.answer("⏳ Это время уже прошло. Укажи будущее время.")
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: ДД.ММ.ГГГГ ЧЧ:ММ")
        return

    data = await state.get_data()
    reminder_text = data.get("reminder_text")
    user_id = message.from_user.id

    rem_id = await add_reminder(user_id, reminder_text, remind_at)
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_at,
        args=[user_id, reminder_text, rem_id],
        id=f"rem_{rem_id}"
    )

    await message.answer(
        f"✅ Запомнил! Я напомню тебе {remind_at.strftime('%d.%m.%Y в %H:%M')}:\n"
        f"«{reminder_text}»"
    )
    await state.clear()

async def main():
    await init_db()
    await load_scheduled_jobs()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())