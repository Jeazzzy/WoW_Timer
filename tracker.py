"""Pure tracking and storage logic. All timestamps are Unix seconds (UTC)."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
CHARACTERS = (
    "Эдгвин", "Усатыйморж", "Элнин", "Jeazzy", "Бэйбик", "Чамингтон", "Инферналь"
)
MAIN_INDEX = 6
TARGET = 2 * 60 * 60
FIVE_MIN = 5 * 60
WEEKLY_REWARD = 100
BUYOUT_COST = 50


def game_day(now: float | None = None) -> str:
    """Game day labeled by the Moscow date of its 07:00 starting boundary."""
    if now is None:
        now = time.time()
    return (datetime.fromtimestamp(now, MOSCOW) - timedelta(hours=7)).date().isoformat()


def game_day_end(day: str) -> float:
    next_date = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(next_date, dt_time(7, 0), MOSCOW).timestamp()


def game_week(now: float | None = None) -> str:
    """Current Wednesday 07:00 MSK reset, identified by its starting date."""
    d = date.fromisoformat(game_day(now))
    return (d - timedelta(days=(d.weekday() - 2) % 7)).isoformat()


def week_days(week: str) -> list[str]:
    start = date.fromisoformat(week)
    if start.weekday() != 2:
        raise ValueError("Неделя должна начинаться в среду")
    return [(start + timedelta(days=offset)).isoformat() for offset in range(7)]


def next_week(week: str) -> str:
    return (date.fromisoformat(week) + timedelta(days=7)).isoformat()


def clock_hm(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{(seconds % 3600) // 60:02}"


@dataclass(frozen=True)
class Progress:
    character: int
    elapsed: float
    running: bool
    manual_done: bool
    reward_received: bool
    warned_five: bool = False
    notified_done: bool = False

    @property
    def norm_done(self) -> bool:
        return self.manual_done if self.character == MAIN_INDEX else self.elapsed >= TARGET

    @property
    def fully_done(self) -> bool:
        return self.norm_done and self.reward_received


class Tracker:
    def __init__(self, db_path: str | Path = "data/tracker.sqlite3") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                day TEXT NOT NULL,
                character INTEGER NOT NULL,
                elapsed REAL NOT NULL DEFAULT 0,
                started_at REAL,
                warned_five INTEGER NOT NULL DEFAULT 0,
                notified_done INTEGER NOT NULL DEFAULT 0,
                manual_done INTEGER NOT NULL DEFAULT 0,
                reward_received INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, character)
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS buyouts (
                day TEXT NOT NULL,
                character INTEGER NOT NULL,
                bought_at REAL NOT NULL,
                price INTEGER NOT NULL,
                PRIMARY KEY (day, character)
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS token_ledger (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT NOT NULL
            )
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def get_meta(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO metadata (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )
        self.con.commit()

    def init_day_marker(self, now: float) -> None:
        if self.get_meta("last_day_reported") is None:
            self.set_meta("last_day_reported", game_day(now))

    def _ensure(self, day: str, index: int) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO progress (day, character) VALUES (?,?)", (day, index)
        )

    def _row(self, day: str, index: int):
        return self.con.execute(
            "SELECT * FROM progress WHERE day=? AND character=?", (day, index)
        ).fetchone()

    @staticmethod
    def _elapsed(row: sqlite3.Row | None, day: str, now: float) -> float:
        if row is None:
            return 0.0
        elapsed = float(row["elapsed"])
        if row["started_at"] is not None:
            elapsed += max(0.0, min(now, game_day_end(day)) - float(row["started_at"]))
        return min(float(TARGET), elapsed)

    def progress(self, index: int, now: float, day: str | None = None) -> Progress:
        if not (0 <= index < len(CHARACTERS)):
            raise ValueError("Неизвестный персонаж")
        day = day or game_day(now)
        row = self._row(day, index)
        return Progress(
            character=index,
            elapsed=self._elapsed(row, day, now),
            running=bool(row and row["started_at"] is not None and day == game_day(now)
                         and self._elapsed(row, day, now) < TARGET),
            manual_done=bool(row and row["manual_done"]),
            reward_received=bool(row and row["reward_received"]),
            warned_five=bool(row and row["warned_five"]),
            notified_done=bool(row and row["notified_done"]),
        )

    def all_progress(self, now: float, day: str | None = None) -> list[Progress]:
        return [self.progress(i, now, day) for i in range(len(CHARACTERS))]

    def close_expired(self, now: float) -> None:
        """Finalize stale sessions strictly at their day's 07:00 boundary."""
        today = game_day(now)
        old_rows = self.con.execute(
            "SELECT * FROM progress WHERE day < ? AND started_at IS NOT NULL", (today,)
        ).fetchall()
        for row in old_rows:
            total = self._elapsed(row, row["day"], now)
            self.con.execute(
                "UPDATE progress SET elapsed=?, started_at=NULL WHERE day=? AND character=?",
                (total, row["day"], row["character"]),
            )
        if old_rows:
            self.con.commit()

    def _settle(self, day: str, index: int, now: float) -> None:
        row = self._row(day, index)
        if row is None or row["started_at"] is None:
            return
        elapsed = self._elapsed(row, day, now)
        self.con.execute(
            "UPDATE progress SET elapsed=?, started_at=NULL WHERE day=? AND character=?",
            (elapsed, day, index),
        )

    def start(self, index: int, now: float) -> str:
        if index == MAIN_INDEX:
            return "У Инферналя норма подтверждается вручную."
        if not (0 <= index < len(CHARACTERS)):
            return "Неизвестный персонаж."
        day = game_day(now)
        self.close_expired(now)
        if self.progress(index, now).norm_done:
            return "Два часа уже отыграны. Осталось проверить награду."
        current = self.con.execute(
            "SELECT character FROM progress WHERE day=? AND started_at IS NOT NULL", (day,)
        ).fetchall()
        switched = False
        for row in current:
            old_index = int(row["character"])
            if old_index == index:
                return f"{CHARACTERS[index]} уже на таймере."
            self._settle(day, old_index, now)
            switched = True
        self._ensure(day, index)
        self.con.execute(
            "UPDATE progress SET started_at=? WHERE day=? AND character=?",
            (now, day, index),
        )
        self.con.commit()
        suffix = " Предыдущий таймер остановлен." if switched else ""
        return f"▶️ Таймер запущен: {CHARACTERS[index]}.{suffix}"

    def set_remaining(self, index: int, remaining_minutes: int, now: float) -> str:
        """Replace today's elapsed time and start a fresh countdown from ``now``.

        Intended for correcting overcount after an unexpected WoW outage. This
        updates the same durable progress row as the normal Start/Pause buttons.
        """
        if not 0 <= index < len(CHARACTERS):
            return "Неизвестный персонаж."
        if index == MAIN_INDEX:
            return "У Инферналя норма подтверждается вручную."
        if type(remaining_minutes) is not int or not 0 <= remaining_minutes <= 120:
            return "Укажи целое число оставшихся минут от 0 до 120."

        self.close_expired(now)
        day = game_day(now)
        if self.progress(index, now).reward_received:
            return "Награда уже отмечена полученной. Исправление таймера недоступно."

        # A positive remainder starts an active session, so only one character
        # may be running. A zero-minute correction just completes the norm and
        # should not interrupt an unrelated character's live countdown.
        if remaining_minutes:
            active_rows = self.con.execute(
                "SELECT character FROM progress WHERE day=? AND started_at IS NOT NULL",
                (day,),
            ).fetchall()
            for row in active_rows:
                other_index = int(row["character"])
                if other_index != index:
                    self._settle(day, other_index, now)

        self._ensure(day, index)
        elapsed = TARGET - remaining_minutes * 60
        # If <5 minutes remain, don't send a misleading '5 minutes left' ping.
        # If exactly 5 remain, the normal five-minute alert is due immediately.
        self.con.execute(
            """UPDATE progress
               SET elapsed=?, started_at=?, warned_five=?, notified_done=0
               WHERE day=? AND character=?""",
            (elapsed, now if remaining_minutes else None,
             int(remaining_minutes < 5), day, index),
        )
        self.con.commit()
        if remaining_minutes == 0:
            return (f"✅ {CHARACTERS[index]}: 2 часа отмечены выполненными. "
                    "Не забудь подтвердить награду до 07:00 МСК.")
        result = (f"⏱ {CHARACTERS[index]}: осталось {remaining_minutes} мин. "
                  "Таймер запущен заново с текущего момента.")
        if now + remaining_minutes * 60 >= game_day_end(day):
            result += (" ⚠️ До сброса в 07:00 МСК не хватит времени "
                       "для завершения нормы и получения награды.")
        return result

    def pause(self, index: int, now: float) -> str:
        day = game_day(now)
        current = self._row(day, index)
        if not current or current["started_at"] is None:
            return "Этот персонаж сейчас не на таймере."
        self._settle(day, index, now)
        self.con.commit()
        return f"⏸ Таймер {CHARACTERS[index]} остановлен. Время сохранено."

    def manual(self, index: int, done: bool, now: float) -> str:
        if index != MAIN_INDEX:
            return "Ручная норма доступна только Инферналю."
        day = game_day(now)
        self._ensure(day, index)
        self.con.execute(
            "UPDATE progress SET manual_done=?, reward_received=CASE WHEN ?=0 THEN 0 ELSE reward_received END "
            "WHERE day=? AND character=?", (int(done), int(done), day, index)
        )
        self.con.commit()
        return "✅ Норма Инферналя отмечена." if done else "↩️ Отметка Инферналя отменена."

    def reward(self, index: int, received: bool, now: float) -> str:
        state = self.progress(index, now)
        if received and not state.norm_done:
            return "Сначала нужно выполнить норму персонажа."
        day = game_day(now)
        self._ensure(day, index)
        self.con.execute(
            "UPDATE progress SET reward_received=? WHERE day=? AND character=?",
            (int(received), day, index)
        )
        self.con.commit()
        return (f"🎁 Награда отмечена: {CHARACTERS[index]}."
                if received else f"↩️ Получение награды отменено: {CHARACTERS[index]}.")

    def due_alerts(self, now: float) -> list[tuple[int, str, str]]:
        """Returns (character index, kind, text). Mark as sent *after* Telegram accepts it."""
        day = game_day(now)
        alerts: list[tuple[int, str, str]] = []
        for i in range(MAIN_INDEX):
            row = self._row(day, i)
            if not row:
                continue
            state = self.progress(i, now)
            if state.norm_done and not state.notified_done:
                alerts.append((i, "done", f"✅ {CHARACTERS[i]}: 2 часа выполнены! Не забудь забрать награду до 07:00 МСК."))
            elif state.running and state.elapsed >= TARGET - FIVE_MIN and not state.warned_five:
                alerts.append((i, "five", f"⏳ {CHARACTERS[i]}: осталось 5 минут до нормы!"))
        return alerts

    def mark_alert_sent(self, index: int, kind: str, now: float) -> None:
        field = "warned_five" if kind == "five" else "notified_done"
        self.con.execute(
            f"UPDATE progress SET {field}=1 WHERE day=? AND character=?",
            (game_day(now), index)
        )
        if kind == "done":
            # We track at most the required 2 hours; continued online time is irrelevant.
            self._settle(game_day(now), index, now)
        self.con.commit()

    def unreported_day(self, now: float) -> str | None:
        self.init_day_marker(now)
        current = game_day(now)
        seen = self.get_meta("last_day_reported")
        if seen and seen < current:
            return seen
        return None

    def daily_report(self, day: str) -> str:
        # The day has already ended; progress() automatically caps stale sessions.
        end = game_day_end(day)
        rows = self.all_progress(end, day)
        completed = [CHARACTERS[s.character] for s in rows if s.fully_done]
        lines = [f"📅 Итоги игровых суток {day} (07:00 МСК)",
                 f"Полностью выполнено: {len(completed)}/7", ""]
        for state in rows:
            name = CHARACTERS[state.character]
            if state.fully_done:
                lines.append(f"✅ {name} — норма + награда")
            elif state.norm_done:
                lines.append(f"❌ {name} — норма есть, награда НЕ забрана")
            elif state.character == MAIN_INDEX:
                lines.append(f"❌ {name} — ручная норма не отмечена")
            else:
                lines.append(f"❌ {name} — {clock_hm(state.elapsed)}/02:00")
        lines.extend(["", "Неполученные награды сгорели, в новый день они не переходят."])
        return "\n".join(lines)

    def history(self, now: float, days: int = 7) -> str:
        today = date.fromisoformat(game_day(now))
        lines = ["📊 История (последние игровые сутки)"]
        for shift in range(1, days + 1):
            day = (today - timedelta(days=shift)).isoformat()
            states = self.all_progress(game_day_end(day), day)
            naturally = sum(s.fully_done for s in states)
            compensated = sum(self.was_bought(day, i) for i in range(len(CHARACTERS)))
            lines.append(f"{day}: награды {naturally}/7, выкупы {compensated}")
        lines.append("Выкуп помогает недельной серии только того персонажа, для которого записан.")
        lines.append("Ежедневная награда засчитывается лишь до 07:00 МСК.")
        return "\n".join(lines)


    # ----------------------------- Weekly tokens ------------------------------
    def initialize_week(self, now: float) -> None:
        """Initialize once, including for upgrades from the daily-only version.

        Never award retroactive tokens for weeks before weekly tracking was enabled.
        Existing daily progress in the current week remains usable.
        """
        week = game_week(now)
        if self.get_meta("active_week") is None:
            self.set_meta("active_week", week)
        if self.get_meta("last_week_reported") is None:
            self.set_meta("last_week_reported", week)

    def was_bought(self, day: str, index: int) -> bool:
        return self.con.execute(
            "SELECT 1 FROM buyouts WHERE day=? AND character=?", (day, index)
        ).fetchone() is not None

    def day_covered(self, day: str, index: int, now: float) -> bool:
        # Do not count online time that has not actually passed yet.
        return self.was_bought(day, index) or self.progress(
            index, min(now, game_day_end(day)), day
        ).fully_done

    def buyout(self, index: int, day: str, now: float) -> str:
        """Record a buyout ALREADY purchased inside the game (does not spend in WoW).

        Only closed, failed days of the current week can be bought.
        A unique key makes a repeated Telegram callback idempotent.
        """
        if not 0 <= index < len(CHARACTERS):
            return "Неизвестный персонаж."
        if day not in week_days(game_week(now)):
            return "Этот день не входит в текущую игровую неделю."
        if day >= game_day(now):
            return "Этот игровой день ещё не закончился: дождись сброса в 07:00 МСК."
        if self.was_bought(day, index):
            return "Выкуп этого дня уже учтён."
        if self.day_covered(day, index, now):
            return "Этот день уже закрыт, выкуп не нужен."
        self.con.execute(
            "INSERT INTO buyouts(day, character, bought_at, price) VALUES (?,?,?,?)",
            (day, index, now, BUYOUT_COST)
        )
        self.con.execute(
            "INSERT INTO token_ledger(id, created_at, amount, description) VALUES (?,?,?,?)",
            (f"buyout:{day}:{index}", now, -BUYOUT_COST,
             f"Выкуп {day}: {CHARACTERS[index]}")
        )
        self.con.commit()
        return f"✅ Выкуп {day} для {CHARACTERS[index]} учтён (−{BUYOUT_COST} токенов)."

    def token_balance(self) -> int | None:
        """Estimate after user supplies real in-game balance at least once."""
        base = self.get_meta("token_base")
        if base is None:
            return None
        delta = self.con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM token_ledger"
        ).fetchone()[0]
        return int(base) + int(delta)

    def set_token_balance(self, balance: int) -> None:
        """Calibrate to actual token balance without changing immutable ledger."""
        if balance < 0:
            raise ValueError("Баланс не может быть отрицательным")
        delta = int(self.con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM token_ledger"
        ).fetchone()[0])
        self.set_meta("token_base", str(balance - delta))

    def week_stats(self, now: float, week: str | None = None) -> dict:
        """Forecast assumes current and future game days will be finished normally.

        A past incomplete day breaks the forecast for that particular character
        until a recorded buyout restores it.
        """
        week = week or game_week(now)
        today = game_day(now)
        days = week_days(week)
        characters = []
        for i, name in enumerate(CHARACTERS):
            complete_days = [day for day in days if self.day_covered(day, i, now)]
            missed_days = [day for day in days if day < today and day not in complete_days]
            bought_days = [day for day in days if self.was_bought(day, i)]
            earned = WEEKLY_REWARD if len(complete_days) == 7 else 0
            forecast = WEEKLY_REWARD if not missed_days else 0
            characters.append({
                "index": i, "name": name, "covered": len(complete_days),
                "missed": missed_days, "bought": bought_days,
                "earned": earned, "forecast": forecast,
            })
        expenses = int(self.con.execute(
            "SELECT COALESCE(SUM(price),0) FROM buyouts WHERE day>=? AND day<?",
            (week, next_week(week))
        ).fetchone()[0])
        predicted = sum(c["forecast"] for c in characters)
        return {
            "week": week, "days": days, "characters": characters,
            "expenses": expenses, "forecast_bonus": predicted,
            "forecast_net": predicted - expenses,
            "actual_bonus": sum(c["earned"] for c in characters),
            "missed_count": sum(len(c["missed"]) for c in characters),
            "net_if_all_bought": 7 * WEEKLY_REWARD - expenses
                - BUYOUT_COST * sum(len(c["missed"]) for c in characters),
        }

    def settle_weeks(self, now: float) -> list[tuple[str, int]]:
        """Post +100 per eligible character once at every Wednesday rollover."""
        self.initialize_week(now)
        active = self.get_meta("active_week")
        current = game_week(now)
        summaries: list[tuple[str, int]] = []
        while active is not None and active < current:
            stats = self.week_stats(now, active)
            awards = 0
            for char in stats["characters"]:
                if char["earned"]:
                    self.con.execute(
                        "INSERT OR IGNORE INTO token_ledger(id, created_at, amount, description) "
                        "VALUES (?,?,?,?)",
                        (f"bonus:{active}:{char['index']}",
                         now, WEEKLY_REWARD,
                         f"Недельная награда {active}: {char['name']}")
                    )
                    awards += WEEKLY_REWARD
            self.con.execute(
                "INSERT INTO metadata(key,value) VALUES ('active_week',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (next_week(active),)
            )
            self.con.commit()
            summaries.append((active, awards))
            active = next_week(active)
        return summaries

    def unreported_week(self, now: float) -> str | None:
        self.initialize_week(now)
        current = game_week(now)
        seen = self.get_meta("last_week_reported")
        if seen is not None and seen < current:
            return (date.fromisoformat(current) - timedelta(days=7)).isoformat()
        return None

    def weekly_report(self, week: str, now: float) -> str:
        stats = self.week_stats(now, week)
        lines = [f"🏁 Итоги недели {week} — {next_week(week)} (07:00 МСК)"]
        for char in stats["characters"]:
            sign = "✅" if char["earned"] else "❌"
            lines.append(f"{sign} {char['name']}: {char['covered']}/7, +{char['earned']} токенов")
        lines.extend([
            "", f"Начислено: +{stats['actual_bonus']} токенов",
            f"Выкупы: −{stats['expenses']} токенов",
            f"Итог недели: {stats['actual_bonus'] - stats['expenses']:+} токенов",
        ])
        balance = self.token_balance()
        if balance is not None:
            lines.append(f"Учётный баланс: {balance} токенов")
        return "\n".join(lines)
