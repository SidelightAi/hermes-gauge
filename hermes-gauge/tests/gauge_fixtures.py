"""Shared builders for tests. A stand-in for Hermes' ``state.db``."""

import sqlite3

_SESSIONS = """CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    thread_id TEXT,
    display_name TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    title TEXT,
    last_activity_at REAL,
    archived INTEGER DEFAULT 0,
    auto_archived INTEGER DEFAULT 0
)"""


def make_state_db(path, sessions):
    """Create a state.db at ``path``. ``sessions`` is a list of dicts keyed by column."""
    conn = sqlite3.connect(str(path))
    conn.execute(_SESSIONS)
    for row in sessions:
        values = {"source": "telegram", "started_at": 0.0, **row}
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO sessions ({columns}) VALUES ({marks})", list(values.values()))
    conn.commit()
    conn.close()
    return path
