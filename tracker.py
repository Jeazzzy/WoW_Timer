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
    "Маг Эдгвин",
    "Рыцарь смерти Усатыйморж",
    "Паладин Элнин",
    "Воин Jeazzy",
    "Монах Бэйбик",
    "Шаман Чамингтон",
    "Охотник на демонов Инферналь",
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


def previous_week(week: str) -> str:
    return (date.fromisoformat(week) - timedelta(days=7)).isoformat()


def human_date(day: str) -> str:
    return date.fromisoformat(day).strftime("%d.%m.%Y")


def week_day_number(now: float) -> int:
    current_day = date.fromisoformat(game_day(now))
    start = date.fromisoformat(game_week(now))
    return (current_day - start).days + 1


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

    manual_mode: bool = False

    @property
    def norm_done(self) -> bool:
        return self.manual_done if self.manual_mode else self.elapsed >= TARGET

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
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS pending_message_deletions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                manual INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL,
                added_day TEXT NOT NULL,
                archived_day TEXT
            )
        """)
        if self.get_meta("characters_migrated") is None:
            self.con.executemany(
                "INSERT INTO characters(id,name,manual,position,added_day) VALUES (?,?,?,?,?)",
                [(i, name, int(i == MAIN_INDEX), i, "0001-01-01")
                 for i, name in enumerate(CHARACTERS)],
            )
            self.set_meta("characters_migrated", "1")
        self._migrate_progress_table()
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS buyouts_by_day ON buyouts(day)"
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS pending_deletions_by_time "
            "ON pending_message_deletions(delete_at)"
        )
        self.con.commit()

    def _migrate_progress_table(self) -> None:
        """Add fields introduced after the first daily-tracker release."""
        columns = {
            str(row["name"])
            for row in self.con.execute("PRAGMA table_info(progress)").fetchall()
        }
        additions = {
            "elapsed": "REAL NOT NULL DEFAULT 0",
            "started_at": "REAL",
            "warned_five": "INTEGER NOT NULL DEFAULT 0",
            "notified_done": "INTEGER NOT NULL DEFAULT 0",
            "manual_done": "INTEGER NOT NULL DEFAULT 0",
            "reward_received": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.con.execute(
                    f"ALTER TABLE progress ADD COLUMN {name} {definition}"
                )

    def characters(self, day: str | None = None) -> list[sqlite3.Row]:
        if day is None:
            return self.con.execute(
                "SELECT * FROM characters WHERE archived_day IS NULL ORDER BY position,id"
            ).fetchall()
        return self.con.execute(
            "SELECT * FROM characters WHERE added_day<=? "
            "AND (archived_day IS NULL OR archived_day>?) ORDER BY position,id", (day, day)
        ).fetchall()

    def character(self, index: int) -> sqlite3.Row:
        row = self.con.execute("SELECT * FROM characters WHERE id=?", (index,)).fetchone()
        if row is None:
            raise ValueError("Неизвестный персонаж")
        return row

    def is_active(self, index: int) -> bool:
        return any(c["id"] == index for c in self.characters())

    def name(self, index: int) -> str:
        return str(self.character(index)["name"])

    def is_manual(self, index: int) -> bool:
        return bool(self.character(index)["manual"])

    def add_character(self, name: str, manual: bool, now: float) -> int:
        name = self.validate_name(name)
        if len(self.characters()) >= 20:
            raise ValueError("Можно добавить не больше 20 активных персонажей.")
        position = self.con.execute("SELECT COALESCE(MAX(position),0)+1 FROM characters").fetchone()[0]
        cursor = self.con.execute(
            "INSERT INTO characters(name,manual,position,added_day) VALUES (?,?,?,?)",
            (name, int(manual), position, game_day(now)),
        )
        self.con.commit()
        return int(cursor.lastrowid)

    @staticmethod
    def validate_name(name: str) -> str:
        name = " ".join(name.split())
        if not 1 <= len(name) <= 60:
            raise ValueError("Название должно содержать от 1 до 60 символов.")
        return name

    def rename_character(self, index: int, name: str) -> None:
        self.character(index)
        self.con.execute("UPDATE characters SET name=? WHERE id=?", (self.validate_name(name), index))
        self.con.commit()

    def move_character(self, index: int, direction: int) -> None:
        rows = self.characters()
        ids = [c["id"] for c in rows]
        if index not in ids or direction not in (-1, 1):
            raise ValueError("Неизвестный персонаж или направление")
        old = ids.index(index)
        new = old + direction
        if 0 <= new < len(ids):
            ids[old], ids[new] = ids[new], ids[old]
            self.con.executemany("UPDATE characters SET position=? WHERE id=?", enumerate(ids))
            self.con.commit()

    def archive_character(self, index: int, now: float) -> None:
        if not self.is_active(index):
            return
        if len(self.characters()) <= 1:
            raise ValueError("Нельзя архивировать последнего персонажа.")
        self._settle(game_day(now), index, now)
        # Keep today's participation and all historical progress; hide from live menus.
        self.con.execute("UPDATE characters SET archived_day=? WHERE id=?",
                         ((date.fromisoformat(game_day(now)) + timedelta(days=1)).isoformat(), index))
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
        self.character(index)
        day = day or game_day(now)
        row = self._row(day, index)
        return Progress(
            character=index,
            manual_mode=self.is_manual(index),
            elapsed=self._elapsed(row, day, now),
            running=bool(row and row["started_at"] is not None and day == game_day(now)
                         and self._elapsed(row, day, now) < TARGET),
            manual_done=bool(row and row["manual_done"]),
            reward_received=bool(row and row["reward_received"]),
            warned_five=bool(row and row["warned_five"]),
            notified_done=bool(row and row["notified_done"]),
        )

    def all_progress(self, now: float, day: str | None = None) -> list[Progress]:
        return [self.progress(c["id"], now, day) for c in self.characters(day)]

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
        if not self.is_active(index):
            return "Неизвестный персонаж."
        if self.is_manual(index):
            return "У этого персонажа норма подтверждается вручную."
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
                return f"{self.name(index)} уже на таймере."
            self._settle(day, old_index, now)
            switched = True
        self._ensure(day, index)
        self.con.execute(
            "UPDATE progress SET started_at=? WHERE day=? AND character=?",
            (now, day, index),
        )
        self.con.commit()
        suffix = " Предыдущий таймер остановлен." if switched else ""
        return f"▶️ Таймер запущен: {self.name(index)}.{suffix}"

    def set_remaining(self, index: int, remaining_minutes: int, now: float) -> str:
        """Replace today's elapsed time and start a fresh countdown from ``now``.

        Intended for correcting overcount after an unexpected WoW outage. This
        updates the same durable progress row as the normal Start/Pause buttons.
        """
        if not self.is_active(index):
            return "Неизвестный персонаж."
        if self.is_manual(index):
            return "У этого персонажа норма подтверждается вручную."
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
            return (f"✅ {self.name(index)}: 2 часа отмечены выполненными. "
                    "Не забудь подтвердить награду до 07:00 МСК.")
        result = (f"⏱ {self.name(index)}: осталось {remaining_minutes} мин. "
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
        return f"⏸ Таймер {self.name(index)} остановлен. Время сохранено."

    def manual(self, index: int, done: bool, now: float) -> str:
        if not self.is_manual(index):
            return "У этого персонажа используется таймер."
        day = game_day(now)
        self._ensure(day, index)
        self.con.execute(
            "UPDATE progress SET manual_done=?, reward_received=CASE WHEN ?=0 THEN 0 ELSE reward_received END "
            "WHERE day=? AND character=?", (int(done), int(done), day, index)
        )
        self.con.commit()
        return f"✅ Норма {self.name(index)} отмечена." if done else f"↩️ Отметка {self.name(index)} отменена."

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
        return (f"🎁 Награда отмечена: {self.name(index)}."
                if received else f"↩️ Получение награды отменено: {self.name(index)}.")

    def due_alerts(self, now: float) -> list[tuple[int, str, str]]:
        """Returns (character index, kind, text). Mark as sent *after* Telegram accepts it."""
        day = game_day(now)
        alerts: list[tuple[int, str, str]] = []
        for character in self.characters():
            i = character["id"]
            if character["manual"]:
                continue
            row = self._row(day, i)
            if not row:
                continue
            state = self.progress(i, now)
            if state.norm_done and not state.notified_done:
                alerts.append((i, "done", f"✅ {self.name(i)}: 2 часа выполнены! Не забудь забрать награду до 07:00 МСК."))
            elif state.running and state.elapsed >= TARGET - FIVE_MIN and not state.warned_five:
                alerts.append((i, "five", f"⏳ {self.name(i)}: осталось 5 минут до нормы!"))
        return alerts

    def mark_alert_sent(
        self,
        index: int,
        kind: str,
        now: float,
        chat_id: int | None = None,
        message_id: int | None = None,
        delete_at: float | None = None,
    ) -> None:
        fields = {"five": "warned_five", "done": "notified_done"}
        if kind not in fields:
            raise ValueError("Неизвестный тип уведомления")
        deletion_values = (chat_id, message_id, delete_at)
        if any(value is not None for value in deletion_values) and any(
            value is None for value in deletion_values
        ):
            raise ValueError("Для удаления сообщения нужны chat_id, message_id и delete_at")
        field = fields[kind]
        self.con.execute(
            f"UPDATE progress SET {field}=1 WHERE day=? AND character=?",
            (game_day(now), index)
        )
        if kind == "done":
            # We track at most the required 2 hours; continued online time is irrelevant.
            self._settle(game_day(now), index, now)
        if chat_id is not None and message_id is not None and delete_at is not None:
            self.con.execute(
                "INSERT OR REPLACE INTO pending_message_deletions "
                "(chat_id, message_id, delete_at) VALUES (?,?,?)",
                (chat_id, message_id, delete_at),
            )
        self.con.commit()

    def due_message_deletions(self, now: float) -> list[tuple[int, int]]:
        rows = self.con.execute(
            "SELECT chat_id, message_id FROM pending_message_deletions "
            "WHERE delete_at<=? ORDER BY delete_at",
            (now,),
        ).fetchall()
        return [(int(row["chat_id"]), int(row["message_id"])) for row in rows]

    def forget_message_deletion(self, chat_id: int, message_id: int) -> None:
        self.con.execute(
            "DELETE FROM pending_message_deletions WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        self.con.commit()

    def unreported_day(self, now: float) -> str | None:
        self.init_day_marker(now)
        current = game_day(now)
        seen = self.get_meta("last_day_reported")
        if seen and seen < current:
            return (date.fromisoformat(current) - timedelta(days=1)).isoformat()
        return None

    def daily_report(self, day: str) -> str:
        # The day has already ended; progress() automatically caps stale sessions.
        end = game_day_end(day)
        rows = self.all_progress(end, day)
        completed = [self.name(s.character) for s in rows if s.fully_done]
        lines = [f"📅 Итоги игровых суток {human_date(day)} (07:00 МСК)",
                 f"Полностью выполнено: {len(completed)}/{len(rows)}", ""]
        for state in rows:
            name = self.name(state.character)
            if state.fully_done:
                lines.append(f"✅ {name} — норма + награда")
            elif state.norm_done:
                lines.append(f"❌ {name} — норма есть, награда НЕ забрана")
            elif self.is_manual(state.character):
                lines.append(f"❌ {name} — ручная норма не отмечена")
            else:
                lines.append(f"❌ {name} — {clock_hm(state.elapsed)}/02:00")
        lines.extend(["", "Неполученные награды сгорели, в новый день они не переходят."])
        return "\n".join(lines)

    def week_tokens_received(self, week: str) -> int:
        value = self.con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM token_ledger "
            "WHERE id LIKE ? AND amount > 0",
            (f"bonus:{week}:%",),
        ).fetchone()[0]
        return int(value)

    def history(self, now: float, week: str | None = None) -> str:
        week = week or game_week(now)
        days = week_days(week)
        lines = [f"📊 Неделя: {human_date(week)} — {human_date(next_week(week))}"]
        collected = 0
        possible = 0
        for day in days:
            states = self.all_progress(min(now, game_day_end(day)), day)
            rewards = sum(state.fully_done for state in states)
            collected += rewards
            possible += len(states)
            lines.append(f"{human_date(day)} — награды {rewards}/{len(states)}")
        bought = int(self.con.execute(
            "SELECT COUNT(*) FROM buyouts WHERE day>=? AND day<?",
            (week, next_week(week)),
        ).fetchone()[0])
        lines.extend(["", f"Собрано наград: {collected}/{possible}", f"Выкуплено дней: {bought}"])
        if week < game_week(now):
            lines.append(f"Получено токенов: {self.week_tokens_received(week)}")
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
        if not self.is_active(index):
            return "Неизвестный персонаж."
        if day not in week_days(game_week(now)):
            return "Этот день не входит в текущую игровую неделю."
        if not any(c["id"] == index for c in self.characters(day)):
            return "В этот день персонаж ещё не участвовал."
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
             f"Выкуп {day}: {self.name(index)}")
        )
        self.con.commit()
        return (f"✅ Выкуп {human_date(day)} для {self.name(index)} "
                f"учтён (−{BUYOUT_COST} токенов).")

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
        participants = self.con.execute(
            "SELECT * FROM characters WHERE added_day<? "
            "AND (archived_day IS NULL OR archived_day>?) ORDER BY position,id",
            (next_week(week), week),
        ).fetchall()
        for character in participants:
            i, name = character["id"], character["name"]
            eligible = [day for day in days if day >= character["added_day"]
                        and (character["archived_day"] is None or day < character["archived_day"])]
            complete_days = [day for day in eligible if self.day_covered(day, i, now)]
            missed_days = [day for day in eligible if day < today and day not in complete_days]
            bought_days = [day for day in days if self.was_bought(day, i)]
            earned = WEEKLY_REWARD if len(complete_days) == 7 else 0
            forecast = WEEKLY_REWARD if not missed_days and len(eligible) == 7 else 0
            characters.append({
                "index": i, "name": name, "covered": len(complete_days), "eligible": eligible,
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
            "net_if_all_bought": sum(len(c["eligible"]) == 7 for c in characters) * WEEKLY_REWARD - expenses
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
                    cursor = self.con.execute(
                        "INSERT OR IGNORE INTO token_ledger(id, created_at, amount, description) "
                        "VALUES (?,?,?,?)",
                        (f"bonus:{active}:{char['index']}",
                         now, WEEKLY_REWARD,
                         f"Недельная награда {active}: {char['name']}")
                    )
                    if cursor.rowcount:
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
        lines = [f"🏁 Итоги недели {human_date(week)} — {human_date(next_week(week))} (07:00 МСК)"]
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
