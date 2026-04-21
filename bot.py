import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil import parser
import aiosqlite

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8651845065:AAF5UKZ_zJ5zm12ykMNxT3quEaIgZqnhL9k"
DATABASE = "reminders.db"
# ================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# FSM для создания и редактирования
class ReminderForm(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()
    editing_time = State()  # для изменения времени

# ---------- Клавиатуры ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders"),
        InlineKeyboardButton(text="➕ Создать напоминание", callback_data="create_reminder")
    )
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def back_to_menu_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    return builder.as_markup()

def reminder_actions_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{reminder_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_{reminder_id}")
    )
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

# ---------- Обработчики команд и кнопок ----------
@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот-напоминалка с кнопками.\n"
        "Выбери действие в меню:",
        reply_markup=main_menu_keyboard()
    )

# Главное меню по callback
@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🏠 Главное меню. Выбери действие:",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    text = (
        "📌 *Команды и кнопки:*\n\n"
        "• *📋 Мои напоминания* – показать список активных напоминаний.\n"
        "• *➕ Создать напоминание* – добавить новое напоминание.\n"
        "• В списке напоминаний ты можешь *удалить* или *изменить время*.\n\n"
        "Или просто напиши мне текст, и я предложу ввести время!"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

# ---------- Создание напоминания ----------
@dp.callback_query(F.data == "create_reminder")
async def start_create(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.waiting_for_text)
    await callback.message.edit_text(
        "📝 О чём тебе напомнить? Напиши текст напоминания:",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(ReminderForm.waiting_for_text)
async def process_text(message: types.Message, state: FSMContext):
    await state.update_data(reminder_text=message.text)
    await state.set_state(ReminderForm.waiting_for_time)
    await message.answer(
        "📅 Когда тебе напомнить?\n"
        "Введи дату и время в формате: *ДД.ММ.ГГГГ ЧЧ:ММ*\n"
        "Например: `25.12.2026 15:30`",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )

@dp.message(ReminderForm.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        remind_at = parser.parse(time_str, dayfirst=True)
        if remind_at < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее время:")
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: ДД.ММ.ГГГГ ЧЧ:ММ")
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

    await message.answer(
        f"✅ Запомнил! Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}:\n"
        f"«{reminder_text}»",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

# ---------- Просмотр списка с кнопками ----------
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

    text_lines = ["📋 *Твои напоминания:*\n"]
    for rem_id, rem_text, remind_at in reminders:
        dt = datetime.fromisoformat(remind_at)
        text_lines.append(
            f"🆔 `{rem_id}`  |  {dt.strftime('%d.%m.%Y %H:%M')}\n"
            f"📌 _{rem_text[:40]}{'…' if len(rem_text)>40 else ''}_"
        )
    await callback.message.edit_text(
        "\n".join(text_lines),
        parse_mode="Markdown"
    )
    # Отправляем отдельные сообщения с кнопками действий под каждым напоминанием
    for rem_id, rem_text, remind_at in reminders:
        dt = datetime.fromisoformat(remind_at)
        msg = f"🔹 *{rem_text[:30]}{'…' if len(rem_text)>30 else ''}* — {dt.strftime('%d.%m.%Y %H:%M')}"
        await callback.message.answer(
            msg,
            parse_mode="Markdown",
            reply_markup=reminder_actions_keyboard(rem_id)
        )
    await callback.message.answer(
        "Вернуться в меню:",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

# ---------- Удаление ----------
@dp.callback_query(F.data.startswith("delete_"))
async def delete_callback(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    deleted = await delete_reminder(rem_id, callback.from_user.id)
    if deleted:
        try:
            scheduler.remove_job(f"rem_{rem_id}")
        except:
            pass
        await callback.message.edit_text(f"✅ Напоминание {rem_id} удалено.")
    else:
        await callback.message.edit_text("❌ Не удалось удалить напоминание.")
    await callback.answer()

# ---------- Изменение времени ----------
@dp.callback_query(F.data.startswith("edit_"))
async def edit_callback(callback: types.CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_")[1])
    reminder = await get_reminder(rem_id, callback.from_user.id)
    if not reminder:
        await callback.message.edit_text("❌ Напоминание не найдено.")
        await callback.answer()
        return

    await state.update_data(edit_id=rem_id, old_text=reminder[1])
    await state.set_state(ReminderForm.editing_time)
    await callback.message.edit_text(
        f"Текущее напоминание: «{reminder[1]}» на {datetime.fromisoformat(reminder[2]).strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Введи *новое время* в формате ДД.ММ.ГГГГ ЧЧ:ММ",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )
    await callback.answer()

@dp.message(ReminderForm.editing_time)
async def process_edit_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        new_time = parser.parse(time_str, dayfirst=True)
        if new_time < datetime.now():
            await message.answer("⏳ Это время уже прошло. Введи будущее время:")
            return
    except:
        await message.answer("❌ Неверный формат. Попробуй снова: ДД.ММ.ГГГГ ЧЧ:ММ")
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

# ---------- Любой другой текст (для быстрого создания) ----------
@dp.message(StateFilter(None))
async def handle_any_text(message: types.Message, state: FSMContext):
    # Начинаем создание напоминания
    await state.update_data(reminder_text=message.text)
    await state.set_state(ReminderForm.waiting_for_time)
    await message.answer(
        "📅 Когда тебе напомнить?\n"
        "Введи дату и время в формате: *ДД.ММ.ГГГГ ЧЧ:ММ*",
        parse_mode="Markdown",
        reply_markup=back_to_menu_button()
    )

# ---------- Запуск ----------
async def main():
    await init_db()
    await load_scheduled_jobs()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
