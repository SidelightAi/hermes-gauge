"""Session names, state and dated history. Standard library only, read-only.

Hermes keeps the human title of a session in its own ``state.db``
(``sessions.title``). The gauge keeps measurements in ``data.db``. This module
joins the two for reports: readable names instead of ids, a last-activity time,
and a plain active / frozen / archived label. It never writes to either file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# A session that has not moved for this long, or that Hermes has ended, is
# frozen: still on disk, still holding context, but nobody is working in it.
FROZEN_AFTER_S = 72 * 3600

STATE_ACTIVE = "active"
STATE_FROZEN = "frozen"
STATE_ARCHIVED = "archived"
STATES = (STATE_ACTIVE, STATE_FROZEN, STATE_ARCHIVED)

# ``compaction_log.reason`` values written when the user starts a fresh session.
# They clear the gauge, but nothing was summarized, so they are not compactions.
RESET_REASONS = frozenset({"new", "new_session", "session_reset", "reset"})
# ``compaction_log`` also holds the in-flight snapshot taken on entering WATCH.
WATCH_REASON = "watch"

_WANTED = (
    "title", "display_name", "source", "thread_id", "last_activity_at",
    "started_at", "ended_at", "end_reason", "archived", "auto_archived",
)


def state_db_path(home):
    """``state.db`` under a Hermes home. The caller resolves the home of the active profile."""
    return Path(home) / "state.db"


def open_state(path):
    """Read-only connection to Hermes' ``state.db`` at ``path``, or None when it is not there."""
    target = Path(path) if path else None
    if target is None or not target.is_file():
        return None
    try:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sessions LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def has_column(conn, table, column):
    try:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def has_table(conn, table):
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _text(value):
    if value is None:
        return None
    # One line: a title with a line break would split a report row in two.
    text = " ".join(str(value).split())
    return text or None


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def session_meta(state_conn, session_ids):
    """``{session_id: meta}`` for the ids Hermes knows. Unknown ids are absent."""
    ids = [sid for sid in dict.fromkeys(session_ids or []) if sid]
    if state_conn is None or not ids:
        return {}
    try:
        present = {row[1] for row in state_conn.execute("PRAGMA table_info(sessions)")}
        columns = [name for name in _WANTED if name in present]
        if "id" not in present or not columns:
            return {}
        out = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            query = f"SELECT id, {', '.join(columns)} FROM sessions WHERE id IN ({marks})"
            for row in state_conn.execute(query, chunk):
                raw = dict(zip(columns, row[1:]))
                out[row[0]] = {
                    "title": _text(raw.get("title")),
                    "chat_name": _text(raw.get("display_name")),
                    "source": _text(raw.get("source")),
                    "thread_id": _text(raw.get("thread_id")),
                    "last_activity": _number(raw.get("last_activity_at")),
                    "started_at": _number(raw.get("started_at")),
                    "ended_at": _number(raw.get("ended_at")),
                    "end_reason": _text(raw.get("end_reason")),
                    "archived": bool(raw.get("archived") or raw.get("auto_archived")),
                }
        return out
    except sqlite3.Error:
        return {}


def last_activity(meta, last_seen=None):
    """Latest of Hermes' own last activity and the gauge's last reading."""
    stamps = [
        value for value in (
            (meta or {}).get("last_activity"), _number(last_seen),
        ) if value is not None
    ]
    return max(stamps) if stamps else None


def classify(meta, last_seen, now, frozen_after=FROZEN_AFTER_S):
    """``active``, ``frozen`` or ``archived``."""
    if meta and meta.get("archived"):
        return STATE_ARCHIVED
    if meta and meta.get("ended_at"):
        return STATE_FROZEN
    seen = last_activity(meta, last_seen)
    if seen is None or (now - seen) >= frozen_after:
        return STATE_FROZEN
    return STATE_ACTIVE


_THREAD_WORD = {"fr": "fil", "en": "thread"}


def short_id(session_id, size=18):
    return str(session_id or "")[:size]


def display_name(session_id, meta, lang="en", max_len=80, id_len=None):
    """Title (and thread, when the session lives in one) instead of the id.

    Without a title the id itself is the name, cut to ``id_len`` when given.
    """
    title = (meta or {}).get("title")
    if not title:
        return short_id(session_id, id_len) if id_len else str(session_id or "")
    if len(title) > max_len:
        title = title[: max_len - 1].rstrip() + "…"
    thread = (meta or {}).get("thread_id")
    if thread:
        return f"{title} · {_THREAD_WORD.get(lang, _THREAD_WORD['en'])} {thread}"
    return title


def turn_series(gauge_conn, session_id, reset_times=(), chars_per_token=4):
    """Estimated tokens after each dated turn: ``[(created, tokens), ...]``.

    Replays ``recorded_turns`` the way the live gauge counts it. A reply adds to
    the running size; a whole-transcript measurement replaces it; the first turn
    after a compaction or a reset starts the size over. Turns recorded before v0.5
    carry no date and are left out — a curve never invents a time.
    """
    if not has_column(gauge_conn, "recorded_turns", "created"):
        return []
    flag = "absolute" if has_column(gauge_conn, "recorded_turns", "absolute") else "0"
    rows = gauge_conn.execute(
        f"SELECT created, chars, {flag} FROM recorded_turns "
        "WHERE session_id = ? AND created IS NOT NULL ORDER BY created, rowid",
        (session_id,),
    ).fetchall()
    resets = sorted(float(stamp) for stamp in reset_times if stamp is not None)
    per = chars_per_token if isinstance(chars_per_token, int) and chars_per_token >= 1 else 4
    points = []
    level = 0
    anchored = True
    cursor = 0
    for created, chars, absolute in rows:
        while cursor < len(resets) and resets[cursor] <= created:
            level = 0
            anchored = False
            cursor += 1
        size = max(int(chars or 0), 0)
        if absolute or not anchored:
            level = size
        else:
            level += size
        anchored = True
        tokens = level // per if absolute else (level // per) * 2
        points.append((float(created), tokens))
    return points
