"""Session names, state and dated history, read from state.db and data.db."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from gauge_fixtures import make_state_db

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_gauge_sessions_under_test", HERE / "sessions.py")
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)

NOW = 1_700_000_000.0
HOUR = 3600.0


def test_open_state_returns_none_when_the_file_is_missing_or_not_a_state_db(tmp_path):
    assert sessions.open_state(tmp_path / "nope.db") is None
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    assert sessions.open_state(other) is None


def test_open_state_is_read_only(tmp_path):
    path = make_state_db(tmp_path / "state.db", [{"id": "a", "title": "Alpha"}])
    conn = sessions.open_state(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE sessions SET title = 'x'")
    conn.close()


def test_state_db_path_is_under_the_home_it_is_given(monkeypatch, tmp_path):
    # The module never guesses a home: the caller resolves the active profile.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ignored"))
    assert sessions.state_db_path(tmp_path / "x") == tmp_path / "x" / "state.db"
    assert sessions.open_state(None) is None


def test_session_meta_reads_known_sessions_and_skips_unknown_ones(tmp_path):
    path = make_state_db(tmp_path / "state.db", [
        {"id": "a", "title": "  Weekly plan  ", "last_activity_at": 123.0, "thread_id": "7"},
        {"id": "b", "title": "", "archived": 1},
        {"id": "c", "title": "Old", "auto_archived": 1, "ended_at": 5.0, "end_reason": "stale"},
    ])
    conn = sessions.open_state(path)
    meta = sessions.session_meta(conn, ["a", "b", "c", "ghost", "a", ""])
    assert set(meta) == {"a", "b", "c"}
    assert meta["a"]["title"] == "Weekly plan"
    assert meta["a"]["thread_id"] == "7"
    assert meta["a"]["last_activity"] == 123.0
    assert meta["b"]["title"] is None
    assert meta["b"]["archived"] is True
    assert meta["c"]["archived"] is True and meta["c"]["end_reason"] == "stale"
    assert sessions.session_meta(conn, []) == {}
    assert sessions.session_meta(None, ["a"]) == {}
    conn.close()


def test_session_meta_handles_more_ids_than_one_query_allows(tmp_path):
    rows = [{"id": f"s{i}", "title": f"T{i}"} for i in range(1000)]
    conn = sessions.open_state(make_state_db(tmp_path / "state.db", rows))
    meta = sessions.session_meta(conn, [r["id"] for r in rows])
    assert len(meta) == 1000
    conn.close()


def test_session_meta_survives_a_state_db_missing_columns(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('a', 'Only a title')")
    conn.commit()
    conn.close()
    conn = sessions.open_state(path)
    meta = sessions.session_meta(conn, ["a"])
    assert meta["a"]["title"] == "Only a title"
    assert meta["a"]["last_activity"] is None
    assert meta["a"]["archived"] is False
    conn.close()


@pytest.mark.parametrize(
    "meta, last_seen, expected",
    [
        ({"archived": True, "last_activity": NOW}, NOW, "archived"),
        ({"ended_at": NOW - 10, "last_activity": NOW}, NOW, "frozen"),
        ({"last_activity": NOW - HOUR}, None, "active"),
        ({"last_activity": NOW - 25 * HOUR}, None, "active"),
        (None, NOW - HOUR, "active"),
        (None, NOW - 30 * HOUR, "active"),
        (None, NOW - 80 * HOUR, "frozen"),
        (None, None, "frozen"),
        # The gauge saw it more recently than Hermes logged: the newer stamp wins.
        ({"last_activity": NOW - 48 * HOUR}, NOW - HOUR, "active"),
    ],
)
def test_classify(meta, last_seen, expected):
    assert sessions.classify(meta, last_seen, NOW) == expected


def test_last_activity_takes_the_latest_known_stamp():
    assert sessions.last_activity({"last_activity": 10.0}, 20.0) == 20.0
    assert sessions.last_activity({"last_activity": 30.0}, 20.0) == 30.0
    assert sessions.last_activity(None, None) is None
    assert sessions.last_activity({}, 5.0) == 5.0


def test_display_name_prefers_the_title_and_marks_the_thread():
    sid = "20261107_130358_0c6f526f"
    assert sessions.display_name(sid, None) == sid
    assert sessions.display_name(sid, {"title": None}) == sid
    assert sessions.display_name(sid, None, id_len=8) == "20261107"
    assert sessions.display_name(sid, {"title": "Budget review"}) == "Budget review"
    assert sessions.display_name(sid, {"title": "Budget review", "thread_id": "42"}) == (
        "Budget review · thread 42"
    )
    assert sessions.display_name(sid, {"title": "Budget review", "thread_id": "42"}, lang="fr") == (
        "Budget review · fil 42"
    )
    long = sessions.display_name(sid, {"title": "x" * 200})
    assert len(long) == 80 and long.endswith("…")


# --- turn_series ---------------------------------------------------------------


@pytest.fixture
def gauge_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE recorded_turns (
               session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
               chars INTEGER NOT NULL DEFAULT 0, created REAL,
               absolute INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (session_id, turn_id))"""
    )
    yield conn
    conn.close()


def _add(conn, turn, chars, created, absolute=0, session="s"):
    conn.execute("INSERT INTO recorded_turns VALUES (?, ?, ?, ?, ?)", (session, turn, chars, created, absolute))


def test_turn_series_accumulates_replies_the_way_the_gauge_does(gauge_conn):
    _add(gauge_conn, "t1", 4_000, 100.0)
    _add(gauge_conn, "t2", 4_000, 200.0)
    _add(gauge_conn, "t3", 2_000, 300.0)
    assert sessions.turn_series(gauge_conn, "s") == [(100.0, 2_000), (200.0, 4_000), (300.0, 5_000)]


def test_turn_series_lets_an_absolute_measurement_replace_the_size(gauge_conn):
    _add(gauge_conn, "t1", 4_000, 100.0)
    _add(gauge_conn, "t2", 40_000, 200.0, absolute=1)  # whole transcript, one token per 4 chars
    _add(gauge_conn, "t3", 400, 300.0)
    assert sessions.turn_series(gauge_conn, "s") == [(100.0, 2_000), (200.0, 10_000), (300.0, 20_200)]


def test_turn_series_starts_over_after_a_compaction_or_reset(gauge_conn):
    _add(gauge_conn, "t1", 40_000, 100.0)
    _add(gauge_conn, "t2", 40_000, 200.0)
    _add(gauge_conn, "t3", 4_000, 400.0)
    _add(gauge_conn, "t4", 4_000, 500.0)
    series = sessions.turn_series(gauge_conn, "s", reset_times=[300.0])
    assert series == [(100.0, 20_000), (200.0, 40_000), (400.0, 2_000), (500.0, 4_000)]


def test_turn_series_skips_undated_rows_and_other_sessions(gauge_conn):
    _add(gauge_conn, "old", 9_000, None)
    _add(gauge_conn, "new", 4_000, 50.0)
    _add(gauge_conn, "x", 8_000, 60.0, session="other")
    assert sessions.turn_series(gauge_conn, "s") == [(50.0, 2_000)]


def test_turn_series_honours_chars_per_token(gauge_conn):
    _add(gauge_conn, "t1", 4_000, 10.0)
    assert sessions.turn_series(gauge_conn, "s", chars_per_token=2) == [(10.0, 4_000)]
    assert sessions.turn_series(gauge_conn, "s", chars_per_token=0) == [(10.0, 2_000)]


def test_turn_series_is_empty_on_a_database_from_before_v05():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recorded_turns (session_id TEXT, turn_id TEXT, chars INTEGER)")
    conn.execute("INSERT INTO recorded_turns VALUES ('s', 't1', 4000)")
    assert sessions.turn_series(conn, "s") == []
    conn.close()
