"""The public demo must serve without a Hermes install and without writing a database."""

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "webapp-demo" / "server.py"
_SPEC = importlib.util.spec_from_file_location("hermes_gauge_demo_server_tests", _PATH)
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


def test_fixture_sessions_are_the_four_public_percents():
    state = server.fixture_state()
    assert state["fixture"] is True
    assert state["demo"] is False
    assert [s["fill_percent"] for s in state["sessions"]] == [92.0, 63.0, 38.0, 12.0]
    assert [s["status"] for s in state["sessions"]] == ["FULL", "WATCH", "OK", "OK"]
    assert [s["window_source"] for s in state["sessions"]] == ["configured"] * 4
    for session in state["sessions"]:
        assert 0 <= session["fill_percent"] <= 100
        assert sum(session["breakdown"].values()) == session["estimated_tokens"]


def test_demo_sessions_stay_the_canonical_six():
    state = server.demo_state()
    assert state["demo"] is True
    assert state["fixture"] is False
    assert [s["session_id"] for s in state["sessions"]] == [
        "release-review",
        "incident-room",
        "onboarding",
        "api-contract",
        "quick-question",
        "standup-notes",
    ]
    assert [s["fill_percent"] for s in state["sessions"]] == [
        96.0, 88.0, 74.0, 63.0, 28.0, 12.0,
    ]
    page = (_PATH.parent / "index.html").read_text(encoding="utf-8")
    for session in state["sessions"]:
        assert session["session_id"] in page


def test_fixture_dispatch_does_not_create_a_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GAUGE_FIXTURE", "1")
    code, body, _ctype = server.dispatch("/api/gauge")
    assert code == 200
    payload = json.loads(body)
    assert payload["fixture"] is True
    assert [s["fill_percent"] for s in payload["sessions"]] == [92.0, 63.0, 38.0, 12.0]
    assert not (tmp_path / "plugin-data").exists()


def test_missing_database_falls_back_to_fixtures(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GAUGE_FIXTURE", raising=False)
    monkeypatch.delenv("GAUGE_LIVE", raising=False)
    code, body, _ctype = server.dispatch("/api/gauge")
    assert code == 200
    assert json.loads(body)["fixture"] is True


def test_demo_query_wins_over_fixture_mode(monkeypatch):
    monkeypatch.setenv("GAUGE_FIXTURE", "1")
    code, body, _ctype = server.dispatch("/api/gauge?demo=1")
    assert code == 200
    payload = json.loads(body)
    assert payload["demo"] is True
    assert len(payload["sessions"]) == 6
