import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "statcast_bot.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # July 19: auto-post velocity-drop alerts. Dedupe is per
        # (game_pk, pitcher_id, pitch_type, metric) so each specific drop
        # alerts once, but a DIFFERENT pitch type or metric dropping later
        # in the same start still gets its own alert.
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerted_drops (
                game_pk INTEGER,
                pitcher_id INTEGER,
                pitch_type TEXT,
                metric TEXT,
                alerted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (game_pk, pitcher_id, pitch_type, metric)
            )
        """)


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))


def get_config(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def drop_already_alerted(game_pk: int, pitcher_id: int, pitch_type: str, metric: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM alerted_drops WHERE game_pk=? AND pitcher_id=? AND pitch_type=? AND metric=?",
            (game_pk, pitcher_id, pitch_type, metric),
        ).fetchone()
        return row is not None


def mark_drop_alerted(game_pk: int, pitcher_id: int, pitch_type: str, metric: str):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO alerted_drops (game_pk, pitcher_id, pitch_type, metric) VALUES (?,?,?,?)",
            (game_pk, pitcher_id, pitch_type, metric),
        )
