import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
from tracker import Tracker

TZ = ZoneInfo("Europe/Moscow")


class BotUiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.tracker = Tracker(Path(self.tmp.name) / "ui.sqlite3")
        bot.tracker.set_token_balance(870)
        self.now = datetime(2026, 10, 15, 12, tzinfo=TZ).timestamp()

    def tearDown(self):
        bot.tracker.close()
        self.tmp.cleanup()

    def test_home_has_week_day_and_balance_without_old_explanations(self):
        bot.tracker.start(0, self.now - 53 * 60)
        text = bot.home_text(self.now)
        self.assertIn("Игровые сутки: 15.10.2026", text)
        self.assertIn("Неделя: 14.10.2026 — 21.10.2026", text)
        self.assertIn("День 2", text)
        self.assertIn("Учётный баланс сейчас: 870", text)
        self.assertNotIn("Сброс:", text)
        self.assertNotIn("День выполнен только", text)
        self.assertNotIn("Недельные токены", text)
        self.assertIn("Маг Эдгвин — 00:53/02:00 — осталось 01:07", text)
        buttons = [
            button.text
            for row in bot.markup_home().inline_keyboard
            for button in row
        ]
        self.assertNotIn("🪙 Неделя и токены", buttons)
        self.assertIn("📊 История", buttons)

    def test_history_navigation_contains_previous_week(self):
        current, markup = bot.screen("history", self.now)
        previous, _ = bot.screen("historyprev", self.now)
        self.assertIn("Собрано наград: 0/49", current)
        self.assertIn("Выкуплено дней: 0", current)
        self.assertIn("Неделя: 07.10.2026 — 14.10.2026", previous)
        self.assertIn("Получено токенов: 0", previous)
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("⬅️ Прошлая неделя", buttons)
        previous_buttons = [
            button.text for row in bot.screen("historyprev", self.now)[1].inline_keyboard
            for button in row
        ]
        self.assertIn("✏️ Исправить неделю", previous_buttons)

    def test_history_edit_navigation(self):
        edit_text, edit_markup = bot.screen("historyedit", self.now)
        self.assertIn("Выбери день", edit_text)
        self.assertEqual(len(edit_markup.inline_keyboard), 9)
        day_text, day_markup = bot.screen("historyday", self.now, 0)
        self.assertIn("07.10.2026", day_text)
        self.assertEqual(len(day_markup.inline_keyboard), 8)
        confirm_text, confirm_markup = bot.screen("historyallconfirm", self.now)
        self.assertIn("все награды", confirm_text)
        self.assertEqual(confirm_markup.inline_keyboard[0][0].callback_data,
                         "hist:all:2026-10-07")

    def test_forecast_month_and_year_navigation(self):
        month, month_markup = bot.screen("forecast", self.now)
        self.assertIn("Сейчас: 870", month)
        self.assertIn("Октябрь 2026", month)
        self.assertIn("21.10.2026  →  1 570", month)
        self.assertIn("28.10.2026  →  2 270", month)
        self.assertEqual(sum("  →  " in line for line in month.splitlines()), 4)
        year, year_markup = bot.screen("forecastyear", self.now, 3)
        self.assertEqual(sum("  →  " in line for line in year.splitlines()), 13)
        self.assertIn("13.10.2027  →", year)
        month_buttons = [button.text for row in month_markup.inline_keyboard for button in row]
        year_buttons = [button.text for row in year_markup.inline_keyboard for button in row]
        self.assertIn("🗓 Весь год", month_buttons)
        self.assertIn("◀️ Назад", year_buttons)
        self.assertNotIn("Дальше ▶️", year_buttons)

    def test_infernal_offers_timer_and_manual_completion(self):
        text, markup = bot.screen("char", self.now, 6)
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("▶️ Начать / продолжить", buttons)
        self.assertIn("✅ Отметить норму вручную", buttons)
        self.assertIn("⏱ Установить остаток (мин)", buttons)
        self.assertIn("Онлайн: 00:00/02:00", text)
        bot.tracker.manual(6, True, self.now)
        self.assertIn("норма отмечена вручную", bot.home_text(self.now))
        _, marked_markup = bot.screen("char", self.now, 6)
        marked_buttons = [button.text for row in marked_markup.inline_keyboard for button in row]
        self.assertIn("↩️ Отменить норму", marked_buttons)
        self.assertIn("🎁 Награда получена", marked_buttons)
        self.assertNotIn("▶️ Начать / продолжить", marked_buttons)
        bot.tracker.manual(6, False, self.now)
        bot.tracker.set_remaining(6, 0, self.now)
        _, timed_markup = bot.screen("char", self.now, 6)
        timed_buttons = [button.text for row in timed_markup.inline_keyboard for button in row]
        self.assertIn("🎁 Награда получена", timed_buttons)
        self.assertNotIn("✅ Отметить норму вручную", timed_buttons)

    def test_character_settings_archive_and_restore_screens(self):
        identifier = bot.tracker.add_character("Новый герой", "manual", self.now)
        home, home_markup = bot.screen("home", self.now)
        self.assertIn("Закрыто: 0/8", home)
        self.assertIn("⚙️ Персонажи", [button.text for row in home_markup.inline_keyboard for button in row])
        settings, settings_markup = bot.screen("settings", self.now, identifier)
        self.assertIn("ручная отметка", settings)
        self.assertIn("🔁 Изменить тип учёта",
                      [button.text for row in settings_markup.inline_keyboard for button in row])
        bot.tracker.archive_character(identifier, self.now)
        archive, archive_markup = bot.screen("archive", self.now)
        self.assertIn("Архив персонажей", archive)
        self.assertIn("Новый герой", [button.text for row in archive_markup.inline_keyboard for button in row])
        _, restored_markup = bot.screen("archived", self.now, identifier)
        self.assertIn("↩️ Вернуть персонажа",
                      [button.text for row in restored_markup.inline_keyboard for button in row])


class CharacterHandlersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_tracker = getattr(bot, "tracker", None)
        self.previous_owner = bot.owner_id
        bot.tracker = Tracker(Path(self.tmp.name) / "handlers.sqlite3")
        bot.owner_id = 42
        self.now = datetime(2026, 10, 15, 12, tzinfo=TZ).timestamp()

    def tearDown(self):
        bot.tracker.close()
        bot.tracker = self.previous_tracker
        bot.owner_id = self.previous_owner
        self.tmp.cleanup()

    async def test_mode_archive_and_restore_callbacks(self):
        state = SimpleNamespace(clear=AsyncMock())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
        with patch.object(bot, "redraw", new_callable=AsyncMock):
            with patch.object(bot.time, "time", return_value=self.now):
                callback.data = "manage:setmode:0:manual"
                await bot.handle_callback(callback, state)
                self.assertEqual(bot.tracker.mode(0), "manual")
                callback.data = "manage:archive_yes:0"
                await bot.handle_callback(callback, state)
                self.assertFalse(bot.tracker.is_active(0))
                callback.data = "manage:restore:0"
                await bot.handle_callback(callback, state)
                self.assertTrue(bot.tracker.is_active(0))

    async def test_new_character_name_fsm(self):
        state = SimpleNamespace(get_data=AsyncMock(return_value={"mode": "both"}),
                                clear=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), text="Новый герой",
                                  answer=AsyncMock())
        with patch.object(bot.time, "time", return_value=self.now):
            await bot.receive_character_name(message, state)
        self.assertEqual(bot.tracker.name(7), "Новый герой")
        self.assertEqual(bot.tracker.mode(7), "both")
        state.clear.assert_awaited_once()
