import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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
