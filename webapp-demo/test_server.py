"""Dashboard routes: fixtures without Hermes, the six-session demo, live caps."""

import ast
import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent


def _server():
    spec = importlib.util.spec_from_file_location("gauge_demo_server", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _server()


def test_server_imports_stdlib_only():
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {
        "argparse", "importlib", "json", "os", "sqlite3", "threading",
        "time", "webbrowser", "http", "urllib",
    }


def test_fixture_sessions_are_12_38_63_92_and_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GAUGE_FIXTURE", "1")
    code, body, _ctype = server.dispatch("/api/gauge")
    assert code == 200
    payload = json.loads(body)
    assert payload["fixture"] is True
    assert payload["demo"] is False
    assert [s["fill_percent"] for s in payload["sessions"]] == [92.0, 63.0, 38.0, 12.0]
    assert [s["status"] for s in payload["sessions"]] == ["FULL", "WATCH", "OK", "OK"]
    assert [s["window_source"] for s in payload["sessions"]] == ["configured"] * 4
    for session in payload["sessions"]:
        assert 0 <= session["fill_percent"] <= 100
        assert sum(session["breakdown"].values()) == session["estimated_tokens"]
    assert not (tmp_path / "plugin-data").exists()


def test_missing_database_serves_fixtures_without_creating_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GAUGE_FIXTURE", raising=False)
    monkeypatch.delenv("GAUGE_LIVE", raising=False)
    code, body, _ctype = server.dispatch("/api/gauge")
    assert code == 200
    assert json.loads(body)["fixture"] is True
    assert not (tmp_path / "plugin-data" / "hermes-gauge" / "data.db").exists()


def test_demo_route_keeps_six_sessions_and_does_not_touch_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GAUGE_FIXTURE", "1")
    code, body, _ctype = server.dispatch("/api/gauge?demo=1")
    assert code == 200
    payload = json.loads(body)
    assert payload["demo"] is True
    assert payload["fixture"] is False
    assert [s["session_id"] for s in payload["sessions"]] == [
        "release-review", "incident-room", "onboarding", "api-contract",
        "quick-question", "standup-notes",
    ]
    assert [s["fill_percent"] for s in payload["sessions"]] == [
        96.0, 88.0, 74.0, 63.0, 28.0, 12.0,
    ]
    assert [s["status"] for s in payload["sessions"]] == [
        "FULL", "FULL", "WATCH", "WATCH", "OK", "OK",
    ]
    html = (HERE / "index.html").read_text(encoding="utf-8")
    for session in payload["sessions"]:
        assert session["session_id"] in html
    assert not (tmp_path / "plugin-data").exists()


def test_health_is_ok():
    code, body, _ctype = server.dispatch("/health")
    assert code == 200
    assert json.loads(body)["ok"] is True


def test_live_fill_is_capped_at_100(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    db_dir = home / "plugin-data" / "hermes-gauge"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "data.db")
    conn.execute(
        """CREATE TABLE usage (
               session_id TEXT PRIMARY KEY,
               chars_in INTEGER NOT NULL,
               turns INTEGER NOT NULL,
               model TEXT,
               first_seen REAL,
               last_seen REAL
           )"""
    )
    conn.execute(
        "INSERT INTO usage VALUES ('huge', ?, 1, 'm', 0, 1)",
        (800_000,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("GAUGE_FIXTURE", raising=False)
    monkeypatch.setenv("GAUGE_LIVE", "1")
    payload = json.loads(server.dispatch("/api/gauge")[1])
    assert payload["fixture"] is False
    assert payload["sessions"][0]["fill_percent"] == 100.0
    assert payload["sessions"][0]["window_source"] == "fallback"
    assert 0 <= payload["sessions"][0]["fill_percent"] <= 100


def test_live_unresolved_window_is_an_em_dash(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    db_dir = home / "plugin-data" / "hermes-gauge"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "data.db")
    conn.execute(
        """CREATE TABLE usage (
               session_id TEXT PRIMARY KEY,
               chars_in INTEGER NOT NULL,
               turns INTEGER NOT NULL,
               model TEXT,
               first_seen REAL,
               last_seen REAL,
               anchor_valid INTEGER,
               window_unresolved INTEGER,
               detected_window INTEGER
           )"""
    )
    conn.execute(
        "INSERT INTO usage VALUES ('stale', 800000, 4, 'opus', 0, 1, 0, 1, NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GAUGE_LIVE", "1")
    monkeypatch.delenv("GAUGE_FIXTURE", raising=False)
    session = json.loads(server.dispatch("/api/gauge")[1])["sessions"][0]
    assert session["fill_percent"] is None
    assert session["fill_display"] == "\u2014"
    assert session["window_source"] == "unresolved"
    assert session["status"] == "UNKNOWN"


def test_live_session_window_does_not_leak(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    db_dir = home / "plugin-data" / "hermes-gauge"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "data.db")
    conn.execute(
        """CREATE TABLE usage (
               session_id TEXT PRIMARY KEY,
               chars_in INTEGER NOT NULL,
               turns INTEGER NOT NULL,
               model TEXT,
               first_seen REAL,
               last_seen REAL
           )"""
    )
    conn.execute("INSERT INTO usage VALUES ('alpha', 3600, 1, 'opus', 0, 2)")
    conn.execute("INSERT INTO usage VALUES ('beta', 3600, 1, 'opus', 0, 1)")
    conn.execute(
        """CREATE TABLE session_windows (
               session_id TEXT PRIMARY KEY,
               model TEXT,
               window_tokens INTEGER NOT NULL
           )"""
    )
    conn.execute("INSERT INTO session_windows VALUES ('alpha', 'opus', 3000)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GAUGE_LIVE", "1")
    sessions = {
        s["session_id"]: s
        for s in json.loads(server.dispatch("/api/gauge")[1])["sessions"]
    }
    assert sessions["alpha"]["window_source"] == "configured"
    assert sessions["alpha"]["window_tokens"] == 3_000
    assert sessions["beta"]["window_source"] == "fallback"
    assert sessions["beta"]["window_tokens"] == 200_000


def test_live_subject_uses_the_smallest_window(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    db_dir = home / "plugin-data" / "hermes-gauge"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "data.db")
    conn.execute(
        """CREATE TABLE usage (
               session_id TEXT PRIMARY KEY,
               chars_in INTEGER NOT NULL,
               turns INTEGER NOT NULL,
               model TEXT,
               first_seen REAL,
               last_seen REAL,
               anchor_valid INTEGER
           )"""
    )
    conn.execute("INSERT INTO usage VALUES ('topic', 20000, 2, 'big', 0, 1, 1)")
    conn.execute(
        """CREATE TABLE agents (
               session_id TEXT,
               agent_id TEXT,
               model TEXT,
               chars_in INTEGER,
               detected_window INTEGER
           )"""
    )
    conn.execute("INSERT INTO agents VALUES ('topic', 'a', 'big', 2000, 1000000)")
    conn.execute("INSERT INTO agents VALUES ('topic', 'b', 'small', 18000, 10000)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GAUGE_LIVE", "1")
    session = json.loads(server.dispatch("/api/gauge")[1])["sessions"][0]
    assert session["window_label"] == "window: smallest model of the subject"
    assert session["window_tokens"] == 10_000
    assert session["fill_percent"] == 100.0
    assert session["status"] == "FULL"
    assert {agent["model"] for agent in session["agents"]} == {"big", "small"}


def test_http_root_and_health(monkeypatch):
    monkeypatch.setenv("GAUGE_FIXTURE", "1")
    monkeypatch.setenv("GAUGE_QUIET", "1")
    httpd = server.HTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/health") as response:
            assert response.status == 200
            assert json.loads(response.read())["ok"] is True
        with urlopen(f"http://127.0.0.1:{port}/") as response:
            assert response.status == 200
            assert b"Hermes" in response.read()
        with urlopen(f"http://127.0.0.1:{port}/api/gauge") as response:
            assert response.status == 200
            fills = [s["fill_percent"] for s in json.loads(response.read())["sessions"]]
            assert fills == [92.0, 63.0, 38.0, 12.0]
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
