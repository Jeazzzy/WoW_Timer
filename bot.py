"""Personal WoW online/reward Telegram tracker powered by aiogram 3."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from tracker import (
    TARGET, BUYOUT_COST, Tracker, clock_hm, game_day,
    game_week, human_date, next_week, previous_week, week_day_number,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wow_tracker")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_TEXT = os.getenv("OWNER_ID", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data/tracker.sqlite3"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
ALERT_DELETE_DELAY = 10 * 60
MENU_REFRESH_INTERVAL = 60

router = Router()
tracker: Tracker
owner_id = 0
panel_lock = asyncio.Lock()


class CharacterInput(StatesGroup):
    name = State()


class RemainingInput(StatesGroup):
    waiting_minutes = State()


def is_owner(user_id: int | None) -> bool:
    return user_id == owner_id


def markup_home():
    b = InlineKeyboardBuilder()
    for character in tracker.characters():
        b.button(text=character["name"], callback_data=f"view:char:{character['id']}")
    b.button(text="🎁 Награды", callback_data="view:rewards")
    b.button(text="📊 История", callback_data="view:history")
    b.button(text="🔄 Обновить", callback_data="view:home")
    b.button(text="⚙️ Персонажи", callback_data="view:settings")
    b.adjust(*([1] * len(tracker.characters())), 2, 1, 1)
    return b.as_markup()


def markup_char(index: int, now: float):
    p = tracker.progress(index, now)
    day = game_day(now)
    b = InlineKeyboardBuilder()
    if tracker.is_manual(index):
        if p.manual_done:
            b.button(text="↩️ Отменить норму", callback_data=f"action:manual_off:{index}:{day}")
        else:
            b.button(text="✅ Норма выполнена", callback_data=f"action:manual_on:{index}:{day}")
    elif not p.norm_done:
        if p.running:
            b.button(text="⏸ Остановить таймер", callback_data=f"action:pause:{index}:{day}")
        else:
            b.button(text="▶️ Начать / продолжить", callback_data=f"action:start:{index}:{day}")
    if not tracker.is_manual(index) and not p.reward_received:
        b.button(text="⏱ Установить остаток (мин)",
                 callback_data=f"action:remaining:{index}:{day}")
    if p.norm_done:
        if p.reward_received:
            b.button(text="↩️ Отменить получение награды", callback_data=f"action:reward_off:{index}:{day}")
        else:
            b.button(text="🎁 Награда получена", callback_data=f"action:reward_on:{index}:{day}")
    b.button(text="🎁 Все награды", callback_data="view:rewards")
    b.button(text="⬅️ К списку", callback_data="view:home")
    b.adjust(1)
    return b.as_markup()


def markup_rewards(now: float):
    b = InlineKeyboardBuilder()
    day = game_day(now)
    for p in tracker.all_progress(now):
        if not p.norm_done:
            continue
        action = "reward_off" if p.reward_received else "reward_on"
        symbol = "✅" if p.reward_received else "🎁"
        b.button(text=f"{symbol} {tracker.name(p.character)}",
                 callback_data=f"action:{action}:{p.character}:{day}")
    b.button(text="🔄 Обновить", callback_data="view:rewards")
    b.button(text="⬅️ К списку", callback_data="view:home")
    b.adjust(1)
    return b.as_markup()


def markup_history(now: float, previous: bool = False):
    b = InlineKeyboardBuilder()
    if previous:
        b.button(text="📊 Текущая неделя", callback_data="view:history")
        b.button(text="🔄 Обновить", callback_data="view:historyprev")
    else:
        if tracker.week_stats(now)["missed_count"]:
            b.button(text="💳 Выкупы", callback_data="view:buyouts")
        b.button(text="⬅️ Прошлая неделя", callback_data="view:historyprev")
        b.button(text="🔄 Обновить", callback_data="view:history")
    b.button(text="⬅️ На главную", callback_data="view:home")
    b.adjust(1)
    return b.as_markup()


def markup_buyouts(now: float):
    b = InlineKeyboardBuilder()
    stats = tracker.week_stats(now)
    for char in stats["characters"]:
        if char["missed"] and tracker.is_active(char["index"]):
            b.button(text=char["name"], callback_data=f"view:weekchar:{char['index']}")
    b.button(text="⬅️ К истории", callback_data="view:history")
    b.adjust(1)
    return b.as_markup()


def markup_weekchar(index: int, now: float):
    b = InlineKeyboardBuilder()
    stats = tracker.week_stats(now)
    char = next(c for c in stats["characters"] if c["index"] == index)
    for day in char["missed"]:
        b.button(text=f"💳 Отметить выкуп {human_date(day)} (−{BUYOUT_COST})",
                 callback_data=f"buy:confirm:{index}:{day}")
    b.button(text="⬅️ К выкупам", callback_data="view:buyouts")
    b.adjust(1)
    return b.as_markup()


def markup_buy_confirm(index: int, day: str):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, я купил выкуп в WoW (−50)",
             callback_data=f"buy:apply:{index}:{day}")
    b.button(text="Отмена", callback_data=f"view:weekchar:{index}")
    b.adjust(1)
    return b.as_markup()


def status_label(p) -> str:
    if p.fully_done:
        return "✅ День закрыт"
    if p.norm_done:
        return "🎁 Норма есть, награда не забрана"
    if p.running:
        return "▶️ Таймер идёт"
    if p.elapsed > 0:
        return "⏸ На паузе"
    return "▫️ Не начато"


def home_text(now: float) -> str:
    day = game_day(now)
    week = game_week(now)
    rows = tracker.all_progress(now)
    total_done = sum(p.fully_done for p in rows)
    lines = ["🎮 WoW Tracker", f"Игровые сутки: {human_date(day)}",
             f"Неделя: {human_date(week)} — {human_date(next_week(week))}"]
    lines.extend([f"День {week_day_number(now)}", f"Закрыто: {total_done}/{len(rows)}"])
    balance = tracker.token_balance()
    if balance is not None:
        lines.append(f"Учётный баланс сейчас: {balance}")
    lines.append("")
    for p in rows:
        name = tracker.name(p.character)
        if tracker.is_manual(p.character):
            desc = "ручная отметка"
        else:
            desc = (f"{clock_hm(p.elapsed)}/02:00 — осталось "
                    f"{clock_hm(TARGET - p.elapsed)}")
        symbol = "✅" if p.fully_done else "🎁" if p.norm_done else "▶️" if p.running else "▫️"
        lines.append(f"{symbol} {name} — {desc}")
    return "\n".join(lines)


def character_text(index: int, now: float) -> str:
    p = tracker.progress(index, now)
    lines = [f"🎮 {tracker.name(index)}", f"Игровые сутки: {human_date(game_day(now))}", ""]
    if tracker.is_manual(index):
        lines.append(f"Норма: {'✅ Отмечена вручную' if p.manual_done else '▫️ Не отмечена'}")
        lines.append("Таймер и уведомления отключены.")
    else:
        lines.append(f"Онлайн: {clock_hm(p.elapsed)}/02:00")
        if not p.norm_done:
            lines.append(f"Осталось: {clock_hm(TARGET - p.elapsed)}")
    lines.extend(["", f"Статус: {status_label(p)}",
                  f"Награда: {'✅ Получена' if p.reward_received else '❌ Не получена'}"])
    return "\n".join(lines)


def rewards_text(now: float) -> str:
    rows = tracker.all_progress(now)
    lines = ["🎁 Награды за игровые сутки " + human_date(game_day(now)), ""]
    for p in rows:
        label = "✅ Получена" if p.reward_received else "🎁 Можно отметить" if p.norm_done else "🔒 Сначала норма"
        lines.append(f"{tracker.name(p.character)} — {label}")
    lines.extend(["", "Неполученные до 07:00 МСК награды сгорают.",
                  "Кнопки появятся, когда выполнена норма."])
    return "\n".join(lines)


def buyouts_text(now: float) -> str:
    stats = tracker.week_stats(now)
    return (f"💳 Выкупы — неделя {human_date(stats['week'])} — "
            f"{human_date(next_week(stats['week']))}\n\n"
            "Выбери персонажа с пропущенным днём.")


def weekchar_text(index: int, now: float) -> str:
    stats = tracker.week_stats(now)
    char = next(c for c in stats["characters"] if c["index"] == index)
    today = game_day(now)
    lines = [f"💳 {char['name']} — неделя {human_date(stats['week'])}",
             f"Закрыто: {char['covered']}/7; выкуплено: {len(char['bought'])}", ""]
    for day in stats["days"]:
        if day not in char["eligible"]:
            state = "▫️ Не участвовал"
        elif tracker.was_bought(day, index):
            state = "💳 Выкуплен (−50)"
        elif tracker.day_covered(day, index, now):
            state = "✅ Время + награда"
        elif day < today:
            state = "❌ Пропущен — можно выкупить"
        elif day == today:
            state = "⏳ Сегодня (до 07:00 МСК)"
        else:
            state = "▫️ Впереди"
        lines.append(f"{human_date(day)} — {state}")
    lines.extend(["", f"Прогноз награды: +{char['forecast']} токенов",
                  "Покупку выкупа делай в WoW, затем отмечай её кнопкой ниже.",
                  "Выкупить можно пропущенный день до конца недельного КД."])
    return "\n".join(lines)


def settings_screen(index: int | None = None):
    b = InlineKeyboardBuilder()
    if index is None:
        lines = ["⚙️ Персонажи", "Выбери персонажа для изменения порядка или названия."]
        for c in tracker.characters():
            b.button(text=c["name"], callback_data=f"view:settings:{c['id']}")
        b.button(text="➕ Добавить", callback_data="manage:add")
    else:
        c = tracker.character(index)
        lines = [f"⚙️ {c['name']}", "Режим: " + ("ручная отметка" if c["manual"] else "таймер 2 часа")]
        ids = [c["id"] for c in tracker.characters()]
        if ids.index(index) > 0:
            b.button(text="↑ Выше", callback_data=f"manage:up:{index}")
        if ids.index(index) < len(ids) - 1:
            b.button(text="↓ Ниже", callback_data=f"manage:down:{index}")
        b.button(text="✏️ Переименовать", callback_data=f"manage:rename:{index}")
        b.button(text="📦 Архивировать", callback_data=f"manage:archive:{index}")
        b.button(text="⬅️ Персонажи", callback_data="view:settings")
    b.button(text="⬅️ На главную", callback_data="view:home")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


def cancel_markup():
    b = InlineKeyboardBuilder()
    b.button(text="Отмена", callback_data="view:home")
    return b.as_markup()


def screen(view: str, now: float, index: int | None = None):
    if view == "settings":
        return settings_screen(index)
    if view == "char" and index is not None:
        return character_text(index, now), markup_char(index, now)
    if view == "rewards":
        return rewards_text(now), markup_rewards(now)
    if view == "week":
        return tracker.history(now), markup_history(now)
    if view == "weekchar" and index is not None:
        return weekchar_text(index, now), markup_weekchar(index, now)
    if view == "history":
        return tracker.history(now), markup_history(now)
    if view == "historyprev":
        week = previous_week(game_week(now))
        return tracker.history(now, week), markup_history(now, previous=True)
    if view == "buyouts":
        return buyouts_text(now), markup_buyouts(now)
    return home_text(now), markup_home()


async def delete_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest as ex:
        # Telegram cannot remove messages older than its deletion window.
        if not any(reason in str(ex).lower() for reason in
                   ("message to delete not found", "message can't be deleted")):
            raise


async def _panel(bot: Bot, text: str, markup) -> None:
    saved = tracker.get_meta("menu_message_id")
    if saved:
        try:
            await bot.edit_message_text(chat_id=owner_id, message_id=int(saved),
                                        text=text, reply_markup=markup)
            return
        except TelegramBadRequest as ex:
            if "message is not modified" in str(ex):
                return
            if not any(reason in str(ex).lower() for reason in
                       ("message to edit not found", "message can't be edited")):
                raise
            await delete_message(bot, owner_id, int(saved))
    message = await bot.send_message(owner_id, text, reply_markup=markup)
    tracker.set_meta("menu_message_id", str(message.message_id))


async def show_panel(bot: Bot, view="home", index=None, notice=None, text=None, markup=None):
    async with panel_lock:
        now = time.time()
        tracker.close_expired(now)
        tracker.settle_weeks(now)
        refresh_view = view if text is None and view != "settings" else ""
        if text is None:
            text, markup = screen(view, now, index)
        if notice:
            text = notice + "\n\n" + text
        await _panel(bot, text, markup)
        remember_panel(refresh_view, index, now)


def remember_panel(view: str, index: int | None, now: float) -> None:
    with tracker.con:
        tracker.con.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("menu_view", view), ("menu_index", "" if index is None else str(index)),
             ("menu_refreshed_at", str(now)),
             ("menu_timer_running", str(int(any(p.running for p in tracker.all_progress(now)))))],
        )


async def refresh_panel(bot: Bot, now: float) -> None:
    async with panel_lock:
        view = tracker.get_meta("menu_view")
        if not view or not tracker.get_meta("menu_message_id"):
            return
        last = float(tracker.get_meta("menu_refreshed_at") or 0)
        if now - last < MENU_REFRESH_INTERVAL:
            return
        running = any(p.running for p in tracker.all_progress(now))
        # Refresh once more at completion, even after mark_alert_sent stops the timer.
        if not running and tracker.get_meta("menu_timer_running") != "1":
            return
        raw_index = tracker.get_meta("menu_index")
        index = int(raw_index) if raw_index else None
        if index is not None and not tracker.is_active(index):
            view, index = "home", None
        text, markup = screen(view, now, index)
        await _panel(bot, text, markup)
        remember_panel(view, index, now)


async def respond_screen(message: Message, view: str, index=None, notice=None):
    await delete_message(message.bot, message.chat.id, message.message_id)
    await show_panel(message.bot, view, index, notice)


async def respond(message: Message, text: str, reply_markup=None):
    await delete_message(message.bot, message.chat.id, message.message_id)
    await show_panel(message.bot, text=text, markup=reply_markup or cancel_markup())


async def redraw(callback: CallbackQuery, view: str, index: int | None = None,
                 notice: str | None = None) -> None:
    await show_panel(callback.bot, view, index, notice)


@router.message(CommandStart())
@router.message(Command("status"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    tracker.close_expired(now)
    await respond_screen(message, "home")


@router.message(Command("rewards"))
async def cmd_rewards(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    await respond_screen(message, "rewards")


@router.message(Command("week"))
async def cmd_week(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    tracker.settle_weeks(now)
    await respond_screen(message, "history")


@router.message(Command("balance"))
async def cmd_balance(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    raw = (message.text or "").strip().split()
    if len(raw) == 1:
        balance = tracker.token_balance()
        await respond(message, "Баланс ещё не указан. Введи /balance 250" if balance is None
                             else f"Учётный баланс: {balance} токенов. Для коррекции: /balance 250")
        return
    if len(raw) != 2 or not raw[1].isdigit():
        await respond(message, "Формат: /balance 250 (фактический баланс токенов в игре)")
        return
    tracker.settle_weeks(time.time())
    tracker.set_token_balance(int(raw[1]))
    await respond(message, f"✅ Баланс синхронизирован с игрой: {int(raw[1])} токенов. "
                         "Последующие выкупы и недельные начисления бот учитывает автоматически.")


@router.message(Command("history"))
async def cmd_history(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    await respond_screen(message, "history")


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    await respond(message,
        "Команды: /start, /status, /rewards, /history, /balance [число], /cancel, /help.\n\n"
        "Выбери персонажа и нажми ▶️, когда зайдёшь в игру. "
        "На паузе время не растёт. При запуске другого чара старый таймер автоматически останавливается.\n\n"
        "Если WoW-сервер упал или время посчиталось неправильно, "
        "выбери персонажа → «⏱ Установить остаток (мин)» и пришли число от 0 до 120. "
        "Бот заменит текущий прогресс и запустит обратный отсчёт с момента ввода. "
        "0 означает, что норма уже выполнена. /cancel отменяет ввод.\n\n"
        "Предупреждение придёт за 5 минут и по достижении 2 часов. "
        "Награду после нормы отметь отдельной кнопкой до 07:00 МСК.\n\n"
        "Недельный прогресс и прошлая неделя находятся в разделе «История». "
        "Пропущенный день можно отметить как выкупленный там после покупки в игре. "
        "Баланс можно задать командой /balance 250.\n\n"
        "В «⚙️ Персонажи» можно менять порядок, добавлять, переименовывать и архивировать персонажей. "
        "Ручная норма — без таймера и уведомлений. "
        "Бот не считывает фактический онлайн из WoW: всё учитывается по твоим нажатиям."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    active = await state.get_state()
    await state.clear()
    await delete_message(message.bot, message.chat.id, message.message_id)
    await show_panel(message.bot, notice="Ввод отменён." if active else "Сейчас нет ожидающего ввода.")


@router.message(RemainingInput.waiting_minutes)
async def receive_remaining(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    raw = (message.text or "").strip()
    if len(raw) > 3 or not raw.isascii() or not raw.isdecimal():
        await respond(message, "Пришли целое число от 0 до 120 (например, 37). Или /cancel.")
        return
    minutes = int(raw)
    if not 0 <= minutes <= 120:
        await respond(message, "Допустимо от 0 до 120 минут. Пример: 37. Или /cancel.")
        return
    data = await state.get_data()
    index = data.get("index")
    day = data.get("day")
    await state.clear()
    now = time.time()
    tracker.close_expired(now)
    if not isinstance(index, int) or not tracker.is_active(index):
        await respond(message, "Персонаж не выбран. Открой /start и попробуй ещё раз.")
        return
    if day != game_day(now):
        await respond(message, "Уже наступил новый игровой день (07:00 МСК). "
                             "Старый таймер менять нельзя. Выбери персонажа заново.")
        return
    result = tracker.set_remaining(index, minutes, now)
    await respond_screen(message, "char", index, notice=result)


@router.message(CharacterInput.name)
async def receive_character_name(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    try:
        if "index" in data:
            index = data["index"]
            if not tracker.is_active(index):
                await state.clear()
                await respond(message, "Персонаж уже архивирован.")
                return
            tracker.rename_character(index, message.text or "")
        else:
            index = tracker.add_character(message.text or "", data["manual"], time.time())
    except ValueError as ex:
        await respond(message, str(ex))
        return
    await state.clear()
    text, markup = settings_screen(index)
    await respond(message, text, markup)


@router.callback_query()
async def handle_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async def inline_answer(text=None, show_alert=False):
        await callback.answer()
        if text:
            await show_panel(callback.bot, notice=text)

    # Remember the pre-upgrade menu, including when its first action is a prompt.
    async with panel_lock:
        if isinstance(callback.message, Message):
            saved = tracker.get_meta("menu_message_id")
            if not saved:
                tracker.set_meta("menu_message_id", str(callback.message.message_id))
            elif int(saved) != callback.message.message_id:
                await delete_message(callback.bot, owner_id, callback.message.message_id)

    data = callback.data or ""
    parts = data.split(":")
    now = time.time()
    tracker.close_expired(now)
    tracker.settle_weeks(now)
    # Navigating to another screen/action cancels an unfinished minutes prompt.
    await state.clear()
    if parts[0] == "manage":
        await callback.answer()
        try:
            operation = parts[1]
            if operation == "add":
                b = InlineKeyboardBuilder()
                b.button(text="⏱ Таймер 2 часа", callback_data="manage:new:timer")
                b.button(text="✅ Ручная отметка", callback_data="manage:new:manual")
                b.button(text="Отмена", callback_data="view:settings")
                b.adjust(1)
                await show_panel(callback.bot, text="Выбери режим нового персонажа:", markup=b.as_markup())
                return
            if operation == "new" and parts[2] in ("timer", "manual"):
                await state.set_state(CharacterInput.name)
                await state.update_data(manual=parts[2] == "manual")
                await show_panel(callback.bot, text="Введи название персонажа (1–60 символов).", markup=cancel_markup())
                return
            index = int(parts[2])
            if not tracker.is_active(index):
                raise ValueError("Персонаж уже архивирован.")
            if operation in ("up", "down"):
                tracker.move_character(index, -1 if operation == "up" else 1)
                await redraw(callback, "settings", index)
            elif operation == "rename":
                await state.set_state(CharacterInput.name)
                await state.update_data(index=index)
                await show_panel(callback.bot, text="Введи новое название (1–60 символов).", markup=cancel_markup())
            elif operation == "archive":
                b = InlineKeyboardBuilder()
                b.button(text="📦 Да, архивировать", callback_data=f"manage:archive_yes:{index}")
                b.button(text="Отмена", callback_data=f"view:settings:{index}")
                b.adjust(1)
                await show_panel(callback.bot, text=f"Архивировать {tracker.name(index)}? История сохранится, таймер остановится.", markup=b.as_markup())
            elif operation == "archive_yes":
                tracker.archive_character(index, now)
                await redraw(callback, "settings")
            else:
                raise ValueError("Неизвестная операция")
        except (ValueError, IndexError) as ex:
            await show_panel(callback.bot, "settings", notice=str(ex))
        return
    if len(parts) >= 2 and parts[0] == "view":
        await inline_answer()
        view = parts[1]
        index = None
        if view in ("char", "weekchar", "settings") and len(parts) == 3:
            try:
                index = int(parts[2])
                if not tracker.is_active(index):
                    raise ValueError
            except ValueError:
                await redraw(callback, "home")
                return
        await redraw(callback, view, index)
        return
    if len(parts) == 4 and parts[0] == "buy":
        _, kind, index_text, day = parts
        try:
            index = int(index_text)
            if not tracker.is_active(index):
                raise ValueError
        except ValueError:
            await inline_answer("Некорректный персонаж", show_alert=True)
            return
        if day not in tracker.week_stats(now)["days"] or day >= game_day(now):
            await callback.answer()
            await redraw(callback, "weekchar", index,
                         notice="Выкупать можно только прошлые дни текущей недели")
            return
        if kind == "confirm":
            if tracker.day_covered(day, index, now):
                await callback.answer()
                await redraw(callback, "weekchar", index, notice="Этот день уже закрыт")
                return
            await inline_answer()
            text = (f"Подтвердить учёт выкупа?\n\n{tracker.name(index)} — {human_date(day)}\n"
                    f"Стоимость: −{BUYOUT_COST} токенов.\n\n"
                    "Подтверждай только после фактической покупки в игре. Отмены расхода нет.")
            await show_panel(callback.bot, text=text, markup=markup_buy_confirm(index, day))
            return
        if kind == "apply":
            result = tracker.buyout(index, day, now)
            await inline_answer()
            await redraw(callback, "weekchar", index, notice=result)
            return
        await inline_answer("Неизвестная операция", show_alert=True)
        return
    if len(parts) == 4 and parts[0] == "action":
        action, index_text, day = parts[1:]
        try:
            index = int(index_text)
            if not tracker.is_active(index):
                raise ValueError
        except ValueError:
            await inline_answer("Некорректный персонаж", show_alert=True)
            return
        if day != game_day(now):
            await callback.answer()
            await redraw(callback, "home", notice="Это кнопка прошлого игрового дня")
            return
        if action == "remaining":
            if tracker.is_manual(index):
                await inline_answer("У этого персонажа нет таймера", show_alert=True)
                return
            if tracker.progress(index, now).reward_received:
                await inline_answer("Награда уже отмечена. Правка недоступна", show_alert=True)
                return
            await state.set_state(RemainingInput.waiting_minutes)
            await state.update_data(index=index, day=day)
            await inline_answer()
            await show_panel(
                callback.bot, text=f"⏱ {tracker.name(index)} — введи оставшиеся минуты (0–120).\n\n"
                "Например, 37 — запустить таймер на 37 минут. 0 — норма выполнена.\n"
                "Отмена — кнопкой ниже или /cancel.", markup=cancel_markup())
            return
        if action == "start":
            result = tracker.start(index, now)
        elif action == "pause":
            result = tracker.pause(index, now)
        elif action in {"manual_on", "manual_off"}:
            result = tracker.manual(index, action == "manual_on", now)
        elif action in {"reward_on", "reward_off"}:
            result = tracker.reward(index, action == "reward_on", now)
        else:
            await inline_answer("Неизвестная команда", show_alert=True)
            return
        await inline_answer()
        if action.startswith("reward"):
            await redraw(callback, "rewards", notice=result)
        else:
            await redraw(callback, "char", index, notice=result)
        return
    await inline_answer("Неизвестная кнопка", show_alert=True)


@router.message()
async def receive_other(message: Message):
    if is_owner(message.from_user.id if message.from_user else None):
        await delete_message(message.bot, message.chat.id, message.message_id)
        await show_panel(message.bot)


async def publish_daily_report(bot: Bot, now: float) -> None:
    async with panel_lock:
        day = tracker.unreported_day(now)
        if day is not None:
            # Persist intermediate state so a failed send/restart resumes without duplicate reports.
            if tracker.get_meta("report_day") != day:
                old = tracker.get_meta("report_message_id")
                if old:
                    await delete_message(bot, owner_id, int(old))
                message = await bot.send_message(owner_id, tracker.daily_report(day))
                with tracker.con:
                    tracker.con.executemany(
                        "INSERT INTO metadata(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        [("report_message_id", str(message.message_id)), ("report_day", day)],
                    )
            menu = tracker.get_meta("menu_message_id")
            if menu:
                await delete_message(bot, owner_id, int(menu))
                tracker.set_meta("menu_message_id", "")
            text, markup = screen("home", now)
            await _panel(bot, text, markup)
            remember_panel("home", None, now)
            tracker.set_meta("last_day_reported", game_day(now))
        # Weekly results remain in History; settlement still runs normally.
        tracker.set_meta("last_week_reported", game_week(now))


async def worker(bot: Bot) -> None:
    """Durable timers: calculate from persisted timestamps rather than sleep durations."""
    while True:
        try:
            now = time.time()
            tracker.close_expired(now)
            tracker.settle_weeks(now)
            await publish_daily_report(bot, now)
            for chat_id, message_id in tracker.due_message_deletions(now):
                await delete_message(bot, chat_id, message_id)
                tracker.forget_message_deletion(chat_id, message_id)
            for index, kind, alert in tracker.due_alerts(now):
                message = await bot.send_message(owner_id, alert)
                tracker.mark_alert_sent(
                    index,
                    kind,
                    now,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    delete_at=time.time() + ALERT_DELETE_DELAY,
                )
            await refresh_panel(bot, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка фоновой проверки, повтор через 15 секунд")
        await asyncio.sleep(15)


async def main() -> None:
    global tracker, owner_id
    if not TOKEN or not OWNER_ID_TEXT:
        raise RuntimeError("Заполни BOT_TOKEN и OWNER_ID в .env")
    try:
        owner_id = int(OWNER_ID_TEXT)
    except ValueError as ex:
        raise RuntimeError("OWNER_ID в .env должен быть целым числом") from ex
    if owner_id <= 0:
        raise RuntimeError("OWNER_ID в .env должен быть положительным числом")
    tracker = Tracker(DB_PATH)
    now = time.time()
    tracker.init_day_marker(now)
    tracker.initialize_week(now)
    tracker.close_expired(now)
    tracker.settle_weeks(now)
    bot = Bot(TOKEN)
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.include_router(router)
    monitor = asyncio.create_task(worker(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        tracker.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
