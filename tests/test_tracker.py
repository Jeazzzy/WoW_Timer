import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tracker import (MAIN_INDEX, TARGET, Tracker, game_day, game_week, week_days,
                     next_week, WEEKLY_REWARD, BUYOUT_COST, human_date,
                     previous_week, week_day_number)

TZ = ZoneInfo('Europe/Moscow')


def ts(day, hour, minute=0, second=0):
    return datetime.fromisoformat(f'{day}T{hour:02}:{minute:02}:{second:02}').replace(tzinfo=TZ).timestamp()


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')

    def tearDown(self):
        self.tr.close()
        self.tmp.cleanup()

    def test_day_boundary_7_moscow(self):
        self.assertEqual(game_day(ts('2026-10-16', 6, 59, 59)), '2026-10-15')
        self.assertEqual(game_day(ts('2026-10-16', 7)), '2026-10-16')
        self.assertEqual(human_date('2026-10-16'), '16.10.2026')
        self.assertEqual(week_day_number(ts('2026-10-15', 8)), 2)
        self.assertEqual(previous_week('2026-10-14'), '2026-10-07')

    def test_pauses_and_switching_are_cumulative(self):
        t = ts('2026-10-15', 8)
        self.tr.start(0, t)
        self.tr.start(1, t + 2400)  # pauses first after 40m
        self.assertEqual(self.tr.progress(0, t+2400).elapsed, 2400)
        self.assertFalse(self.tr.progress(0, t+2400).running)
        self.tr.start(0, t + 3600)  # resume first
        self.assertEqual(self.tr.progress(0, t+3600+4800).elapsed, TARGET)
        self.assertTrue(self.tr.progress(0, t+3600+4800).norm_done)
        self.assertFalse(self.tr.progress(0, t+3600+4800).fully_done)

    def test_reward_required_and_not_carried_after_7(self):
        t = ts('2026-10-15', 23)
        self.tr.start(0, t)
        self.assertTrue(self.tr.progress(0, t+TARGET).norm_done)
        self.assertFalse(self.tr.progress(0, t+TARGET).fully_done)
        self.assertIn('награда НЕ забрана', self.tr.daily_report('2026-10-15'))
        reset = ts('2026-10-16', 7)
        self.tr.close_expired(reset)
        self.assertEqual(self.tr.progress(0, reset).elapsed, 0)
        self.assertFalse(self.tr.progress(0, reset).reward_received)
        self.assertFalse(self.tr.progress(0, reset).fully_done)

    def test_claiming_reward_completes_only_that_day(self):
        t = ts('2026-10-15', 8)
        self.tr.start(2, t)
        self.tr.pause(2, t+TARGET)
        result = self.tr.reward(2, True, t+TARGET)
        self.assertIn('Награда отмечена', result)
        self.assertTrue(self.tr.progress(2, t+TARGET).fully_done)
        tomorrow = ts('2026-10-16', 8)
        self.assertFalse(self.tr.progress(2, tomorrow).fully_done)
        self.assertIn('✅ Паладин Элнин', self.tr.daily_report('2026-10-15'))

    def test_infernal_manual_without_timer(self):
        t = ts('2026-10-15', 8)
        self.assertIn('вручную', self.tr.start(MAIN_INDEX, t))
        self.tr.manual(MAIN_INDEX, True, t)
        self.assertTrue(self.tr.progress(MAIN_INDEX, t).norm_done)
        self.assertFalse(self.tr.progress(MAIN_INDEX, t).fully_done)
        self.assertEqual(self.tr.due_alerts(t), [])
        self.tr.reward(MAIN_INDEX, True, t)
        self.assertTrue(self.tr.progress(MAIN_INDEX, t).fully_done)
        self.tr.manual(MAIN_INDEX, False, t)
        self.assertFalse(self.tr.progress(MAIN_INDEX, t).reward_received)

    def test_five_minute_and_done_alert_once(self):
        t = ts('2026-10-15', 9)
        self.tr.start(4, t)
        self.assertEqual(self.tr.due_alerts(t + TARGET - 301), [])
        due = self.tr.due_alerts(t + TARGET - 300)
        self.assertEqual([(idx, kind) for idx, kind, _ in due], [(4, 'five')])
        self.tr.mark_alert_sent(4, 'five', t+TARGET-300)
        self.assertEqual(self.tr.due_alerts(t+TARGET-299), [])
        due = self.tr.due_alerts(t + TARGET)
        self.assertEqual([(idx, kind) for idx, kind, _ in due], [(4, 'done')])
        self.tr.mark_alert_sent(4, 'done', t+TARGET)
        self.assertEqual(self.tr.due_alerts(t+TARGET+60), [])
        self.assertFalse(self.tr.progress(4, t+TARGET+60).running)

    def test_sent_alert_flags_survive_reopen(self):
        start = ts('2026-10-15', 9)
        self.tr.start(4, start)
        warning_at = start + TARGET - 300
        self.tr.mark_alert_sent(4, 'five', warning_at)
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertEqual(self.tr.due_alerts(warning_at + 1), [])
        done_at = start + TARGET
        self.tr.mark_alert_sent(4, 'done', done_at)
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertEqual(self.tr.due_alerts(done_at + 1), [])

    def test_pending_alert_deletion_survives_reopen(self):
        start = ts('2026-10-15', 9)
        self.tr.start(0, start)
        warning_at = start + TARGET - 300
        delete_at = warning_at + 600
        self.tr.mark_alert_sent(
            0,
            'five',
            warning_at,
            chat_id=123,
            message_id=456,
            delete_at=delete_at,
        )
        self.assertEqual(self.tr.due_message_deletions(delete_at - 1), [])
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertEqual(self.tr.due_message_deletions(delete_at), [(123, 456)])
        self.tr.forget_message_deletion(123, 456)
        self.assertEqual(self.tr.due_message_deletions(delete_at + 1), [])

    def test_capped_at_reset_even_if_bot_was_offline(self):
        t = ts('2026-10-16', 6)
        self.tr.start(1, t)
        now = ts('2026-10-16', 9)
        self.tr.close_expired(now)
        old = self.tr.progress(1, now, '2026-10-15')
        self.assertEqual(old.elapsed, 3600)
        self.assertFalse(old.norm_done)
        self.assertFalse(old.running)
        self.assertEqual(self.tr.progress(1, now).elapsed, 0)

    def test_report_is_sent_once_per_new_day(self):
        start = ts('2026-10-15', 8)
        self.tr.init_day_marker(start)
        self.assertIsNone(self.tr.unreported_day(start))
        new = ts('2026-10-16', 7)
        self.assertEqual(self.tr.unreported_day(new), '2026-10-15')
        self.tr.set_meta('last_day_reported', game_day(new))
        self.assertIsNone(self.tr.unreported_day(new))

    def test_report_after_long_downtime_uses_latest_completed_day(self):
        start = ts('2026-10-15', 8)
        self.tr.init_day_marker(start)
        after_downtime = ts('2026-10-19', 12)
        self.assertEqual(self.tr.unreported_day(after_downtime), '2026-10-18')

    def test_history_shows_current_week_by_days(self):
        self.complete_character_day(self.tr, 0, '2026-10-14')
        now = ts('2026-10-16', 8)
        self.tr.initialize_week(now)
        self.tr.buyout(1, '2026-10-15', now)
        report = self.tr.history(now)
        self.assertIn('Неделя: 14.10.2026 — 21.10.2026', report)
        self.assertIn('14.10.2026 — награды 1/7', report)
        self.assertIn('20.10.2026 — награды 0/7', report)
        self.assertIn('Собрано наград: 1/49', report)
        self.assertIn('Выкуплено дней: 1', report)
        self.assertNotIn('Выкуп помогает', report)
        self.assertNotIn('Ежедневная награда засчитывается', report)


    @staticmethod
    def complete_character_day(tr, index, day):
        moment = ts(day, 8)
        if index == MAIN_INDEX:
            tr.manual(index, True, moment)
        else:
            tr.start(index, moment)
            tr.pause(index, moment + TARGET)
        tr.reward(index, True, moment + TARGET)

    def test_week_resets_wednesday_at_7(self):
        self.assertEqual(game_week(ts('2026-10-14', 6, 59)), '2026-10-07')
        self.assertEqual(game_week(ts('2026-10-14', 7)), '2026-10-14')
        self.assertEqual(game_week(ts('2026-10-21', 6, 59)), '2026-10-14')
        self.assertEqual(game_week(ts('2026-10-21', 7)), '2026-10-21')
        self.assertEqual(len(week_days('2026-10-14')), 7)
        self.assertEqual(next_week('2026-10-14'), '2026-10-21')

    def test_current_running_time_cannot_complete_future_day(self):
        now = ts('2026-10-14', 8)
        self.tr.start(0, now)
        stat = self.tr.week_stats(now + 60)['characters'][0]
        self.assertEqual(stat['covered'], 0)
        self.assertEqual(stat['forecast'], 100)

    def test_seven_days_earn_100_per_character_max_700(self):
        self.tr.initialize_week(ts('2026-10-14', 8))
        self.tr.set_token_balance(300)
        for day in week_days('2026-10-14'):
            for index in range(7):
                self.complete_character_day(self.tr, index, day)
        end = ts('2026-10-21', 6, 59)
        stat = self.tr.week_stats(end)
        self.assertEqual(stat['forecast_bonus'], 700)
        self.assertEqual(stat['forecast_net'], 700)
        self.assertEqual(stat['actual_bonus'], 700)
        posted = self.tr.settle_weeks(ts('2026-10-21', 7))
        self.assertEqual(posted, [('2026-10-14', 700)])
        self.assertEqual(self.tr.token_balance(), 1000)
        self.assertEqual(self.tr.settle_weeks(ts('2026-10-21', 8)), [])
        self.assertEqual(self.tr.token_balance(), 1000)
        previous = self.tr.history(ts('2026-10-21', 8), '2026-10-14')
        self.assertIn('Получено токенов: 700', previous)

    def test_weekly_awards_are_not_duplicated_after_reopen(self):
        self.tr.initialize_week(ts('2026-10-14', 8))
        self.tr.set_token_balance(0)
        for day in week_days('2026-10-14'):
            self.complete_character_day(self.tr, 0, day)
        reset = ts('2026-10-21', 7)
        self.assertEqual(self.tr.settle_weeks(reset), [('2026-10-14', 100)])
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertEqual(self.tr.settle_weeks(reset + 60), [])
        self.assertEqual(self.tr.token_balance(), 100)
        self.assertEqual(
            self.tr.con.execute(
                "SELECT COUNT(*) FROM token_ledger WHERE id='bonus:2026-10-14:0'"
            ).fetchone()[0],
            1,
        )

    def test_buyout_is_for_one_character_one_failed_day_costs_50(self):
        first = ts('2026-10-14', 8)
        self.tr.initialize_week(first)
        self.tr.set_token_balance(200)
        for day in week_days('2026-10-14'):
            if day != '2026-10-15':
                self.complete_character_day(self.tr, 0, day)
        check = ts('2026-10-20', 22)
        # Character 0 missed Thu; character 1 is wholly uncompleted.
        self.assertEqual(self.tr.week_stats(check)['forecast_bonus'], 0)
        self.assertIn('07:00', self.tr.buyout(0, '2026-10-20', check))
        self.assertEqual(self.tr.buyout(0, '2026-10-15', check),
                         '✅ Выкуп 15.10.2026 для Маг Эдгвин учтён (−50 токенов).')
        self.assertEqual(self.tr.token_balance(), 150)
        self.assertTrue(self.tr.day_covered('2026-10-15', 0, check))
        self.assertFalse(self.tr.day_covered('2026-10-15', 1, check))
        self.assertEqual(self.tr.week_stats(check)['forecast_bonus'], 100)
        self.assertEqual(self.tr.week_stats(check)['expenses'], 50)
        self.assertEqual(self.tr.week_stats(check)['forecast_net'], 50)
        self.assertIn('уже учтён', self.tr.buyout(0, '2026-10-15', check))
        self.assertEqual(self.tr.token_balance(), 150)
        self.assertEqual(self.tr.settle_weeks(ts('2026-10-21', 7)), [('2026-10-14', 100)])
        self.assertEqual(self.tr.token_balance(), 250)
        self.assertIn('текущую игровую неделю',
                      self.tr.buyout(0, '2026-10-15', ts('2026-10-21', 8)))

    def test_no_bonus_if_reward_missing_even_after_full_two_hours(self):
        self.tr.initialize_week(ts('2026-10-14', 8))
        for day in week_days('2026-10-14'):
            self.complete_character_day(self.tr, 0, day)
        # Remove one marked reward for a closed day (simulate not having claimed it).
        self.tr.con.execute("UPDATE progress SET reward_received=0 WHERE day=? AND character=0",
                            ('2026-10-16',))
        self.tr.con.commit()
        before = ts('2026-10-20', 22)
        stats = self.tr.week_stats(before)
        self.assertEqual(stats['characters'][0]['covered'], 6)
        self.assertEqual(stats['characters'][0]['forecast'], 0)
        self.assertEqual(self.tr.settle_weeks(ts('2026-10-21', 7)), [('2026-10-14', 0)])

    def test_balance_recalibration_and_buyout_multiple_characters(self):
        now = ts('2026-10-16', 8)
        self.tr.initialize_week(now)
        self.tr.set_token_balance(500)
        self.tr.buyout(0, '2026-10-15', now)
        self.tr.buyout(1, '2026-10-15', now)
        self.assertEqual(self.tr.token_balance(), 400)
        self.assertEqual(self.tr.week_stats(now)['expenses'], 100)
        self.tr.set_token_balance(360)  # actual balance after outside spending
        self.assertEqual(self.tr.token_balance(), 360)
        self.assertTrue(self.tr.buyout(2, '2026-10-15', now).startswith('✅'))
        self.assertEqual(self.tr.token_balance(), 310)

    def test_buyout_not_allowed_for_successful_day_or_previous_week(self):
        self.tr.initialize_week(ts('2026-10-14', 8))
        self.complete_character_day(self.tr, 0, '2026-10-14')
        at = ts('2026-10-16', 8)
        self.assertIn('уже закрыт', self.tr.buyout(0, '2026-10-14', at))
        self.assertIn('текущую игровую неделю',
                      self.tr.buyout(0, '2026-10-07', at))
        self.assertEqual(self.tr.week_stats(at)['expenses'], 0)

    def test_stored_buyout_and_balance_survive_reopen(self):
        now = ts('2026-10-16', 8)
        self.tr.initialize_week(now)
        self.tr.set_token_balance(500)
        self.tr.buyout(2, '2026-10-15', now)
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertTrue(self.tr.was_bought('2026-10-15', 2))
        self.assertEqual(self.tr.token_balance(), 450)
        self.assertEqual(self.tr.week_stats(now)['expenses'], 50)

    def test_set_remaining_replaces_elapsed_and_starts_immediately(self):
        start = ts('2026-10-15', 8)
        self.tr.start(0, start)
        self.assertIn('осталось 37 мин', self.tr.set_remaining(0, 37, start + 60 * 40))
        self.assertEqual(self.tr.progress(0, start + 60 * 40).elapsed, 83 * 60)
        self.assertTrue(self.tr.progress(0, start + 60 * 40).running)
        self.assertFalse(self.tr.progress(0, start + 60 * 40 + 37 * 60 - 1).norm_done)
        self.assertTrue(self.tr.progress(0, start + 60 * 40 + 37 * 60).norm_done)
        self.assertFalse(self.tr.progress(0, start + 60 * 40 + 37 * 60).fully_done)
        self.tr.reward(0, True, start + 60 * 40 + 37 * 60)
        self.assertTrue(self.tr.progress(0, start + 60 * 40 + 37 * 60).fully_done)

    def test_set_remaining_pauses_other_char(self):
        start = ts('2026-10-15', 8)
        self.tr.start(1, start)
        self.tr.set_remaining(0, 25, start + 20 * 60)
        self.assertEqual(self.tr.progress(1, start + 21 * 60).elapsed, 20 * 60)
        self.assertFalse(self.tr.progress(1, start + 21 * 60).running)
        self.assertTrue(self.tr.progress(0, start + 21 * 60).running)
        self.assertEqual(self.tr.progress(0, start + 21 * 60).elapsed, 96 * 60)

    def test_set_remaining_less_than_five_does_not_send_incorrect_warning(self):
        t = ts('2026-10-15', 8)
        self.tr.set_remaining(2, 3, t)
        self.assertEqual(self.tr.due_alerts(t), [])
        self.assertEqual(self.tr.due_alerts(t + 120), [])
        self.assertEqual(self.tr.due_alerts(t + 180)[0][1], 'done')
        self.tr.mark_alert_sent(2, 'done', t + 180)
        self.assertEqual(self.tr.due_alerts(t + 190), [])

    def test_set_remaining_exact_five_rearms_five_and_done(self):
        t = ts('2026-10-15', 8)
        self.tr.start(0, t)
        self.tr.mark_alert_sent(0, 'five', t + 115 * 60)
        self.tr.set_remaining(0, 5, t + 116 * 60)
        self.assertEqual(self.tr.due_alerts(t + 116 * 60)[0][1], 'five')
        self.tr.mark_alert_sent(0, 'five', t + 116 * 60)
        self.assertEqual(self.tr.due_alerts(t + 121 * 60)[0][1], 'done')

    def test_set_remaining_zero_completes_norm_but_reward_still_needed(self):
        t = ts('2026-10-15', 8)
        self.assertIn('2 часа отмечены', self.tr.set_remaining(3, 0, t))
        state = self.tr.progress(3, t)
        self.assertTrue(state.norm_done)
        self.assertFalse(state.running)
        self.assertFalse(state.fully_done)
        self.assertEqual(self.tr.due_alerts(t)[0][1], 'done')
        self.tr.reward(3, True, t)
        self.assertTrue(self.tr.progress(3, t).fully_done)
        self.assertIn('недоступно', self.tr.set_remaining(3, 20, t + 10))
        self.assertTrue(self.tr.progress(3, t + 10).fully_done)

    def test_zero_remainder_does_not_interrupt_other_active_character(self):
        t = ts('2026-10-15', 8)
        self.tr.start(1, t)
        self.tr.set_remaining(3, 0, t + 10 * 60)
        self.assertTrue(self.tr.progress(1, t + 11 * 60).running)
        self.assertTrue(self.tr.progress(3, t + 11 * 60).norm_done)

    def test_set_remaining_validation_and_infernal(self):
        t = ts('2026-10-15', 8)
        self.assertIn('0 до 120', self.tr.set_remaining(0, 121, t))
        self.assertIn('0 до 120', self.tr.set_remaining(0, -1, t))
        self.assertIn('0 до 120', self.tr.set_remaining(0, 4.2, t))
        self.assertIn('Неизвестный', self.tr.set_remaining(10, 40, t))
        self.assertIn('вручную', self.tr.set_remaining(MAIN_INDEX, 40, t))
        self.assertEqual(self.tr.progress(0, t).elapsed, 0)

    def test_corrected_timer_persists_reopen_and_doesnt_carry_past_reset(self):
        t = ts('2026-10-16', 6, 30)
        self.tr.set_remaining(4, 60, t)
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        self.assertEqual(self.tr.progress(4, t + 20 * 60).elapsed, 80 * 60)
        boundary = ts('2026-10-16', 7)
        self.tr.close_expired(boundary)
        self.assertEqual(self.tr.progress(4, boundary, '2026-10-15').elapsed, 90 * 60)
        self.assertEqual(self.tr.progress(4, boundary).elapsed, 0)
        self.assertFalse(self.tr.progress(4, boundary).running)

    def test_normal_running_timer_persists_after_reopen(self):
        start = ts('2026-10-15', 8)
        self.tr.start(1, start)
        self.tr.close()
        self.tr = Tracker(Path(self.tmp.name) / 't.sqlite3')
        restored = self.tr.progress(1, start + 47 * 60)
        self.assertTrue(restored.running)
        self.assertEqual(restored.elapsed, 47 * 60)

    def test_completion_just_before_reset_can_be_claimed(self):
        completion = ts('2026-10-16', 6, 59, 59)
        self.tr.start(0, completion - TARGET)
        self.assertTrue(self.tr.progress(0, completion).norm_done)
        self.tr.reward(0, True, completion)
        self.assertTrue(self.tr.progress(0, completion).fully_done)
        self.assertFalse(self.tr.progress(0, ts('2026-10-16', 7)).fully_done)

    def test_completion_exactly_at_reset_is_unclaimed_old_day(self):
        reset = ts('2026-10-16', 7)
        self.tr.start(0, reset - TARGET)
        self.tr.close_expired(reset)
        old = self.tr.progress(0, reset, '2026-10-15')
        self.assertTrue(old.norm_done)
        self.assertFalse(old.fully_done)
        self.assertIn('награда НЕ забрана', self.tr.daily_report('2026-10-15'))
        self.assertIn('Сначала нужно выполнить', self.tr.reward(0, True, reset))

    def test_repeated_buttons_are_idempotent(self):
        start = ts('2026-10-15', 8)
        self.tr.start(0, start)
        self.assertIn('уже на таймере', self.tr.start(0, start + 60))
        self.tr.pause(0, start + 10 * 60)
        self.assertIn('не на таймере', self.tr.pause(0, start + 11 * 60))
        self.assertEqual(self.tr.progress(0, start + 11 * 60).elapsed, 10 * 60)
        self.tr.manual(MAIN_INDEX, True, start)
        self.tr.manual(MAIN_INDEX, True, start + 1)
        self.tr.reward(MAIN_INDEX, True, start + 2)
        self.tr.reward(MAIN_INDEX, True, start + 3)
        self.assertTrue(self.tr.progress(MAIN_INDEX, start + 3).fully_done)

    def test_old_progress_schema_is_migrated_without_data_loss(self):
        self.tr.close()
        path = Path(self.tmp.name) / 'old.sqlite3'
        con = sqlite3.connect(path)
        con.execute(
            'CREATE TABLE progress ('
            'day TEXT NOT NULL, character INTEGER NOT NULL, elapsed REAL NOT NULL DEFAULT 0, '
            'started_at REAL, PRIMARY KEY(day, character))'
        )
        con.execute(
            'INSERT INTO progress(day, character, elapsed, started_at) VALUES (?,?,?,NULL)',
            ('2026-10-15', 2, 1800),
        )
        con.commit()
        con.close()
        self.tr = Tracker(path)
        state = self.tr.progress(2, ts('2026-10-15', 9), '2026-10-15')
        self.assertEqual(state.elapsed, 1800)
        self.assertFalse(state.manual_done)
        self.assertFalse(state.reward_received)

    def test_missed_week_does_not_retroactively_award_on_first_boot(self):
        now = ts('2026-10-21', 9)
        self.tr.initialize_week(now)
        self.assertIsNone(self.tr.unreported_week(now))
        self.assertEqual(self.tr.settle_weeks(now), [])
        self.assertEqual(self.tr.con.execute('SELECT COUNT(*) FROM token_ledger').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
