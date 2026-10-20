import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Chat, User, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

import bot
from tracker import Tracker, game_day, TARGET


def timestamp(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=ZoneInfo('Europe/Moscow')).timestamp()


class CharacterStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'tracker.sqlite3'
        self.tr = Tracker(self.path)

    def tearDown(self):
        self.tr.close()
        self.tmp.cleanup()

    def test_reorder_preserves_progress_buyouts_and_persists(self):
        self.tr.start(0, timestamp(15))
        self.tr.buyout(1, '2026-10-14', timestamp(15))
        self.tr.move_character(1, -1)
        self.tr.close()
        self.tr = Tracker(self.path)
        self.assertEqual([c['id'] for c in self.tr.characters()][:2], [1, 0])
        self.assertEqual(self.tr.progress(0, timestamp(15) + 60).elapsed, 60)
        self.assertTrue(self.tr.was_bought('2026-10-14', 1))
        self.assertEqual(len(self.tr.characters()), 7)

    def test_new_characters_do_not_inherit_past_missed_days_or_weekly_bonus(self):
        self.tr.initialize_week(timestamp(14))
        index = self.tr.add_character('Новый', False, timestamp(16))
        char = next(c for c in self.tr.week_stats(timestamp(16))['characters'] if c['index'] == index)
        self.assertEqual(char['missed'], [])
        self.assertEqual(char['forecast'], 0)
        self.assertNotIn('Новый', self.tr.daily_report('2026-10-15'))
        self.assertIn('ещё не участвовал', self.tr.buyout(index, '2026-10-15', timestamp(16)))
        for day in range(16, 21):
            self.tr.set_remaining(index, 0, timestamp(day))
            self.tr.reward(index, True, timestamp(day))
        self.tr.settle_weeks(timestamp(21))
        self.assertEqual(self.tr.week_tokens_received('2026-10-14'), 0)

    def test_manual_new_character_archive_and_nonreused_id(self):
        index = self.tr.add_character('Новый ручной', True, timestamp(15))
        self.tr.manual(index, True, timestamp(15))
        self.tr.reward(index, True, timestamp(15))
        self.assertTrue(self.tr.progress(index, timestamp(15)).fully_done)
        self.assertFalse(any(alert[0] == index for alert in self.tr.due_alerts(timestamp(15))))
        self.tr.archive_character(index, timestamp(15))
        self.assertFalse(self.tr.is_active(index))
        self.assertIn('Новый ручной — норма + награда', self.tr.daily_report('2026-10-15'))
        self.assertNotIn('Новый ручной', self.tr.daily_report('2026-10-16'))
        other = self.tr.add_character('Другой', False, timestamp(16))
        self.assertGreater(other, index)
        self.assertFalse(self.tr.progress(other, timestamp(16)).norm_done)

    def test_reordered_ui_uses_ids_not_positions(self):
        with patch.object(bot, 'tracker', self.tr, create=True):
            self.tr.move_character(6, -1)
            self.tr.rename_character(6, 'Ручной')
            self.assertEqual(bot.markup_home().inline_keyboard[5][0].callback_data, 'view:char:6')
            self.assertIn('Ручной', bot.weekchar_text(6, timestamp(15)))
            self.assertIn('Ручной', self.tr.daily_report('2026-10-15'))


class OneMessageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tr = Tracker(Path(self.tmp.name) / 'tracker.sqlite3')
        self.patchers = [patch.object(bot, 'tracker', self.tr, create=True),
                         patch.object(bot, 'owner_id', 42),
                         patch.object(bot, 'panel_lock', asyncio.Lock()),
                         patch.object(bot.time, 'time', return_value=timestamp(15))]
        for p in self.patchers:
            p.start()
        self.api = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock(),
                                   delete_message=AsyncMock())
        self.next_id = 100
        self.live = {}
        async def send(chat_id, text, **kwargs):
            self.next_id += 1
            self.live[self.next_id] = text
            return SimpleNamespace(message_id=self.next_id)
        async def delete(chat_id, message_id):
            self.live.pop(message_id, None)
        async def edit(**kwargs):
            self.live[kwargs['message_id']] = kwargs['text']
        self.api.send_message.side_effect = send
        self.api.delete_message.side_effect = delete
        self.api.edit_message_text.side_effect = edit
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=42, user_id=42))

    async def asyncTearDown(self):
        await self.storage.close()
        for p in reversed(self.patchers):
            p.stop()
        self.tr.close()
        self.tmp.cleanup()

    def message(self, text, message_id=90):
        return Message(message_id=message_id, date=datetime.now(), chat=Chat(id=42, type='private'),
                       from_user=User(id=42, is_bot=False, first_name='Owner'), text=text).as_(self.api)

    def callback(self, data, message_id=80):
        return CallbackQuery(id='callback', from_user=User(id=42, is_bot=False, first_name='Owner'),
                             chat_instance='test', data=data, message=self.message('Menu', message_id))

    async def test_minutes_prompt_invalid_input_and_result_edit_same_menu(self):
        callback = self.callback('action:remaining:0:2026-10-15')
        with patch.object(CallbackQuery, 'answer', new=AsyncMock()), patch.object(CallbackQuery, 'bot', self.api):
            await bot.handle_callback(callback, self.state)
        self.assertEqual(self.tr.get_meta('menu_message_id'), '80')
        await bot.receive_remaining(self.message('ошибка'), self.state)
        self.assertIsNotNone(await self.state.get_state())
        await bot.receive_remaining(self.message('37', 91), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.tr.progress(0, timestamp(15)).elapsed, TARGET - 37 * 60)
        self.api.send_message.assert_not_awaited()
        self.assertEqual(set(self.live), {80})
        self.assertEqual(self.api.delete_message.await_count, 2)

    async def test_repeated_commands_and_restart_reuse_persisted_menu(self):
        await bot.cmd_start(self.message('/start'), self.state)
        saved = self.tr.get_meta('menu_message_id')
        path = self.tr.con.execute('PRAGMA database_list').fetchone()[2]
        self.tr.close()
        self.tr = Tracker(path)
        bot.tracker = self.tr
        await bot.cmd_history(self.message('/history', 91), self.state)
        self.assertEqual(self.tr.get_meta('menu_message_id'), saved)
        self.assertEqual(self.api.send_message.await_count, 1)
        self.assertEqual(len(self.live), 1)

    async def test_rollover_keeps_only_latest_report_then_menu(self):
        self.tr.init_day_marker(timestamp(14))
        self.tr.set_meta('report_message_id', '70')
        self.tr.set_meta('menu_message_id', '80')
        self.live.update({70: 'Old report', 80: 'Old menu'})
        await bot.publish_daily_report(self.api, timestamp(15))
        self.assertEqual(list(self.live), [101, 102])
        self.assertIn('14.10.2026', self.live[101])
        self.assertIn('WoW Tracker', self.live[102])
        await bot.publish_daily_report(self.api, timestamp(15))
        self.assertEqual(self.api.send_message.await_count, 2)
        await bot.publish_daily_report(self.api, timestamp(16))
        self.assertEqual(list(self.live), [103, 104])
        self.assertIn('15.10.2026', self.live[103])

    async def test_failed_menu_send_retries_without_duplicate_daily_report(self):
        self.tr.init_day_marker(timestamp(14))
        send = self.api.send_message.side_effect
        async def fail_menu(chat_id, text, **kwargs):
            if 'WoW Tracker' in text:
                raise ConnectionError('offline')
            return await send(chat_id, text, **kwargs)
        self.api.send_message.side_effect = fail_menu
        with self.assertRaises(ConnectionError):
            await bot.publish_daily_report(self.api, timestamp(15))
        self.api.send_message.side_effect = send
        await bot.publish_daily_report(self.api, timestamp(15))
        self.assertEqual(list(self.live), [101, 102])
        self.assertEqual(self.tr.get_meta('last_day_reported'), '2026-10-15')

    async def test_missing_menu_replaced_but_other_api_errors_propagate(self):
        self.tr.set_meta('menu_message_id', '80')
        self.api.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(text='x', chat_id=42, message_id=80),
            message='Bad Request: message to edit not found')
        await bot.show_panel(self.api)
        self.assertEqual(self.tr.get_meta('menu_message_id'), '101')
        self.api.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(text='x', chat_id=42, message_id=101), message='Bad Request: other error')
        with self.assertRaises(TelegramBadRequest):
            await bot.show_panel(self.api)
        self.assertEqual(self.api.send_message.await_count, 1)

    async def test_stale_minutes_do_not_change_new_day(self):
        await self.state.set_state(bot.RemainingInput.waiting_minutes)
        await self.state.update_data(index=0, day='2026-10-14')
        await bot.receive_remaining(self.message('20'), self.state)
        self.assertEqual(self.tr.progress(0, timestamp(15)).elapsed, 0)
        self.assertIn('новый игровой день', next(iter(self.live.values())))

    async def test_add_rename_and_reorder_through_settings(self):
        async def click(data):
            message_id = int(self.tr.get_meta('menu_message_id') or 80)
            with patch.object(CallbackQuery, 'answer', new=AsyncMock()), patch.object(CallbackQuery, 'bot', self.api):
                await bot.handle_callback(self.callback(data, message_id), self.state)
        await click('manage:new:manual')
        await bot.receive_character_name(self.message('Новый ручной'), self.state)
        index = self.tr.characters()[-1]['id']
        self.assertTrue(self.tr.is_manual(index))
        await click(f'manage:rename:{index}')
        await bot.receive_character_name(self.message('Новое имя', 91), self.state)
        await click(f'manage:up:{index}')
        self.assertEqual(self.tr.characters()[-2]['id'], index)
        self.assertIn('Новое имя', self.live[80])
        await click(f'manage:archive:{index}')
        self.assertTrue(self.tr.is_active(index))
        await click(f'manage:archive_yes:{index}')
        self.assertFalse(self.tr.is_active(index))
        self.api.send_message.assert_not_awaited()

    async def test_cancel_and_invalid_name_stay_in_one_menu(self):
        await self.state.set_state(bot.CharacterInput.name)
        await self.state.update_data(manual=False)
        await bot.receive_character_name(self.message(' '), self.state)
        self.assertIsNotNone(await self.state.get_state())
        await bot.cmd_cancel(self.message('/cancel', 91), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(len(self.live), 1)
        self.assertIn('WoW Tracker', next(iter(self.live.values())))

    async def test_auto_refresh_throttles_and_preserves_character_screen(self):
        now = timestamp(15)
        self.tr.start(0, now)
        await bot.show_panel(self.api, 'char', 0)
        saved = self.tr.get_meta('menu_message_id')
        await bot.refresh_panel(self.api, now + 59)
        self.api.edit_message_text.assert_not_awaited()
        await bot.refresh_panel(self.api, now + 60)
        self.assertIn('Онлайн: 00:01/02:00', self.live[int(saved)])
        self.assertEqual(self.tr.get_meta('menu_view'), 'char')
        await bot.refresh_panel(self.api, now + 75)
        self.assertEqual(self.api.edit_message_text.await_count, 1)
        self.assertEqual(self.api.send_message.await_count, 1)

    async def test_auto_refresh_leaves_prompts_and_settings_untouched(self):
        now = timestamp(15)
        self.tr.start(0, now)
        await bot.show_panel(self.api, text='Введи минуты', markup=bot.cancel_markup())
        await bot.refresh_panel(self.api, now + 60)
        self.api.edit_message_text.assert_not_awaited()
        self.assertEqual(next(iter(self.live.values())), 'Введи минуты')
        await bot.show_panel(self.api, 'settings')
        self.api.edit_message_text.reset_mock()
        await bot.refresh_panel(self.api, now + 120)
        self.api.edit_message_text.assert_not_awaited()

    async def test_auto_refresh_updates_completion_then_stops(self):
        now = timestamp(15)
        self.tr.set_remaining(0, 1, now)
        await bot.show_panel(self.api, 'char', 0)
        self.tr.mark_alert_sent(0, 'done', now + 60)
        await bot.refresh_panel(self.api, now + 60)
        self.assertIn('Норма есть, награда не забрана', next(iter(self.live.values())))
        await bot.refresh_panel(self.api, now + 120)
        self.assertEqual(self.api.edit_message_text.await_count, 1)

    async def test_auto_refresh_disabled_without_timer_and_resumes_after_input(self):
        now = timestamp(15)
        await bot.show_panel(self.api)
        await bot.refresh_panel(self.api, now + 60)
        self.api.edit_message_text.assert_not_awaited()
        await self.state.set_state(bot.RemainingInput.waiting_minutes)
        await self.state.update_data(index=0, day=game_day(now))
        await bot.receive_remaining(self.message('37'), self.state)
        self.api.edit_message_text.reset_mock()
        await bot.refresh_panel(self.api, now + 60)
        self.api.edit_message_text.assert_awaited_once()
        self.assertEqual(self.tr.get_meta('menu_view'), 'char')
