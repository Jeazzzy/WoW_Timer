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
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from tracker import (
    CHARACTERS, MAIN_INDEX, TARGET, BUYOUT_COST, Tracker, clock_hm, game_day,
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

router = Router()
tracker: Tracker
owner_id = 0


class RemainingInput(StatesGroup):
    waiting_minutes = State()


def is_owner(user_id: int | None) -> bool:
    return user_id == owner_id


def markup_home():
    b = InlineKeyboardBuilder()
    for i, character in enumerate(CHARACTERS):
        b.button(text=character, callback_data=f"view:char:{i}")
    b.button(text="🎁 Награды", callback_data="view:rewards")
    b.button(text="📊 История", callback_data="view:history")
    b.button(text="🔄 Обновить", callback_data="view:home")
    b.adjust(*([1] * 7), 2, 1)
    return b.as_markup()


def markup_char(index: int, now: float):
    p = tracker.progress(index, now)
    day = game_day(now)
    b = InlineKeyboardBuilder()
    if index == MAIN_INDEX:
        if p.manual_done:
            b.button(text="↩️ Отменить норму", callback_data=f"action:manual_off:{index}:{day}")
        else:
            b.button(text="✅ Норма выполнена", callback_data=f"action:manual_on:{index}:{day}")
    elif not p.norm_done:
        if p.running:
            b.button(text="⏸ Остановить таймер", callback_data=f"action:pause:{index}:{day}")
        else:
            b.button(text="▶️ Начать / продолжить", callback_data=f"action:start:{index}:{day}")
    if index != MAIN_INDEX and not p.reward_received:
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
        b.button(text=f"{symbol} {CHARACTERS[p.character]}",
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
        if char["missed"]:
            b.button(text=char["name"], callback_data=f"view:weekchar:{char['index']}")
    b.button(text="⬅️ К истории", callback_data="view:history")
    b.adjust(1)
    return b.as_markup()


def markup_weekchar(index: int, now: float):
    b = InlineKeyboardBuilder()
    stats = tracker.week_stats(now)
    char = stats["characters"][index]
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
    lines.extend([f"День {week_day_number(now)}", f"Закрыто: {total_done}/7"])
    balance = tracker.token_balance()
    if balance is not None:
        lines.append(f"Учётный баланс сейчас: {balance}")
    lines.append("")
    for p in rows:
        name = CHARACTERS[p.character]
        if p.character == MAIN_INDEX:
            desc = "ручная отметка"
        else:
            desc = (f"{clock_hm(p.elapsed)}/02:00 — осталось "
                    f"{clock_hm(TARGET - p.elapsed)}")
        symbol = "✅" if p.fully_done else "🎁" if p.norm_done else "▶️" if p.running else "▫️"
        lines.append(f"{symbol} {name} — {desc}")
    return "\n".join(lines)


def character_text(index: int, now: float) -> str:
    p = tracker.progress(index, now)
    lines = [f"🎮 {CHARACTERS[index]}", f"Игровые сутки: {human_date(game_day(now))}", ""]
    if index == MAIN_INDEX:
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
        lines.append(f"{CHARACTERS[p.character]} — {label}")
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
    char = stats["characters"][index]
    today = game_day(now)
    lines = [f"💳 {char['name']} — неделя {human_date(stats['week'])}",
             f"Закрыто: {char['covered']}/7; выкуплено: {len(char['bought'])}", ""]
    for day in stats["days"]:
        if tracker.was_bought(day, index):
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


def screen(view: str, now: float, index: int | None = None):
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


async def redraw(callback: CallbackQuery, view: str, index: int | None = None,
                 notice: str | None = None) -> None:
    now = time.time()
    tracker.close_expired(now)
    tracker.settle_weeks(now)
    text, markup = screen(view, now, index)
    if notice:
        text = notice + "\n\n" + text
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as ex:
            if "message is not modified" not in str(ex):
                raise
    else:
        await callback.bot.send_message(owner_id, text, reply_markup=markup)


@router.message(CommandStart())
@router.message(Command("status"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    tracker.close_expired(now)
    text, markup = screen("home", now)
    await message.answer(text, reply_markup=markup)


@router.message(Command("rewards"))
async def cmd_rewards(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    text, markup = screen("rewards", now)
    await message.answer(text, reply_markup=markup)


@router.message(Command("week"))
async def cmd_week(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    tracker.settle_weeks(now)
    text, markup = screen("history", now)
    await message.answer(text, reply_markup=markup)


@router.message(Command("balance"))
async def cmd_balance(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    raw = (message.text or "").strip().split()
    if len(raw) == 1:
        balance = tracker.token_balance()
        await message.answer("Баланс ещё не указан. Введи /balance 250" if balance is None
                             else f"Учётный баланс: {balance} токенов. Для коррекции: /balance 250")
        return
    if len(raw) != 2 or not raw[1].isdigit():
        await message.answer("Формат: /balance 250 (фактический баланс токенов в игре)")
        return
    tracker.settle_weeks(time.time())
    tracker.set_token_balance(int(raw[1]))
    await message.answer(f"✅ Баланс синхронизирован с игрой: {int(raw[1])} токенов. "
                         "Последующие выкупы и недельные начисления бот учитывает автоматически.")


@router.message(Command("history"))
async def cmd_history(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    now = time.time()
    text, markup = screen("history", now)
    await message.answer(text, reply_markup=markup)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    await message.answer(
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
        "Инферналь: ручная норма и отдельная награда, без таймера и уведомлений. "
        "Бот не считывает фактический онлайн из WoW: всё учитывается по твоим нажатиям."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    active = await state.get_state()
    await state.clear()
    await message.answer("Ввод оставшихся минут отменён." if active else
                         "Сейчас нет ожидающего ввода.")


@router.message(RemainingInput.waiting_minutes)
async def receive_remaining(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    raw = (message.text or "").strip()
    if len(raw) > 3 or not raw.isascii() or not raw.isdecimal():
        await message.answer("Пришли целое число от 0 до 120 (например, 37). Или /cancel.")
        return
    minutes = int(raw)
    if not 0 <= minutes <= 120:
        await message.answer("Допустимо от 0 до 120 минут. Пример: 37. Или /cancel.")
        return
    data = await state.get_data()
    index = data.get("index")
    day = data.get("day")
    await state.clear()
    now = time.time()
    tracker.close_expired(now)
    if not isinstance(index, int) or not 0 <= index < len(CHARACTERS):
        await message.answer("Персонаж не выбран. Открой /start и попробуй ещё раз.")
        return
    if day != game_day(now):
        await message.answer("Уже наступил новый игровой день (07:00 МСК). "
                             "Старый таймер менять нельзя. Выбери персонажа заново.")
        return
    result = tracker.set_remaining(index, minutes, now)
    text, keyboard = screen("char", now, index)
    await message.answer(result + "\n\n" + text, reply_markup=keyboard)


@router.callback_query()
async def handle_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = callback.data or ""
    parts = data.split(":")
    now = time.time()
    tracker.close_expired(now)
    tracker.settle_weeks(now)
    # Navigating to another screen/action cancels an unfinished minutes prompt.
    await state.clear()
    if len(parts) >= 2 and parts[0] == "view":
        await callback.answer()
        view = parts[1]
        index = None
        if view in ("char", "weekchar") and len(parts) == 3:
            try:
                index = int(parts[2])
                if not 0 <= index < len(CHARACTERS):
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
            if not 0 <= index < len(CHARACTERS):
                raise ValueError
        except ValueError:
            await callback.answer("Некорректный персонаж", show_alert=True)
            return
        if day not in tracker.week_stats(now)["days"] or day >= game_day(now):
            await callback.answer("Выкупать можно только прошлые дни текущей недели", show_alert=True)
            await redraw(callback, "weekchar", index)
            return
        if kind == "confirm":
            if tracker.day_covered(day, index, now):
                await callback.answer("Этот день уже закрыт", show_alert=True)
                await redraw(callback, "weekchar", index)
                return
            await callback.answer()
            text = (f"Подтвердить учёт выкупа?\n\n{CHARACTERS[index]} — {human_date(day)}\n"
                    f"Стоимость: −{BUYOUT_COST} токенов.\n\n"
                    "Подтверждай только после фактической покупки в игре. Отмены расхода нет.")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(text, reply_markup=markup_buy_confirm(index, day))
            return
        if kind == "apply":
            result = tracker.buyout(index, day, now)
            await callback.answer()
            await redraw(callback, "weekchar", index, notice=result)
            return
        await callback.answer("Неизвестная операция", show_alert=True)
        return
    if len(parts) == 4 and parts[0] == "action":
        action, index_text, day = parts[1:]
        try:
            index = int(index_text)
            if not 0 <= index < len(CHARACTERS):
                raise ValueError
        except ValueError:
            await callback.answer("Некорректный персонаж", show_alert=True)
            return
        if day != game_day(now):
            await callback.answer("Это кнопка прошлого игрового дня", show_alert=True)
            await redraw(callback, "home")
            return
        if action == "remaining":
            if index == MAIN_INDEX:
                await callback.answer("У Инферналя нет таймера", show_alert=True)
                return
            if tracker.progress(index, now).reward_received:
                await callback.answer("Награда уже отмечена. Правка недоступна", show_alert=True)
                return
            await state.set_state(RemainingInput.waiting_minutes)
            await state.update_data(index=index, day=day)
            await callback.answer()
            await callback.bot.send_message(
                owner_id,
                f"⏱ {CHARACTERS[index]} — введи, сколько минут осталось до 2 часов "
                "(целое число от 0 до 120).\n\n"
                "Например, 37 → таймер на 37 минут с момента твоего ответа. "
                "0 → сразу завершить норму, 120 → начать отсчёт заново.\n"
                "Чтобы отменить, напиши /cancel."
            )
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
            await callback.answer("Неизвестная команда", show_alert=True)
            return
        await callback.answer()
        if action.startswith("reward"):
            await redraw(callback, "rewards", notice=result)
        else:
            await redraw(callback, "char", index, notice=result)
        return
    await callback.answer("Неизвестная кнопка", show_alert=True)


async def worker(bot: Bot) -> None:
    """Durable timers: calculate from persisted timestamps rather than sleep durations."""
    while True:
        try:
            now = time.time()
            tracker.close_expired(now)
            tracker.settle_weeks(now)
            week_to_report = tracker.unreported_week(now)
            if week_to_report is not None:
                await bot.send_message(owner_id, tracker.weekly_report(week_to_report, now))
                tracker.set_meta("last_week_reported", game_week(now))
            day_to_report = tracker.unreported_day(now)
            if day_to_report is not None:
                # If the bot was offline for several days, show the most recent
                # recorded day, rather than sending multiple historical alerts.
                await bot.send_message(owner_id, tracker.daily_report(day_to_report))
                tracker.set_meta("last_day_reported", game_day(now))
            for chat_id, message_id in tracker.due_message_deletions(now):
                try:
                    await bot.delete_message(chat_id, message_id)
                except TelegramBadRequest:
                    pass
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
    dp = Dispatcher()
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
