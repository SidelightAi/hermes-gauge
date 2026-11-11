"""Behavior tests for hermes-gauge tools.

`tools.py` imports `plugin_db` from `plugins.plugin_storage`. That import is
replaced with an in-memory SQLite connection so the suite never touches
~/.hermes. pytest is the runner only; the plugin itself stays stdlib.
"""

import importlib.util
import json
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from gauge_fixtures import make_state_db

_CONN = {"conn": None}


def _plugin_db(name):
    assert name == "hermes-gauge"
    conn = _CONN["conn"]
    assert conn is not None, "plugin_db called outside a test"
    return conn


def _install_fake_plugins():
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_db = _plugin_db
    plugins = types.ModuleType("plugins")
    plugins.plugin_storage = storage
    sys.modules["plugins"] = plugins
    sys.modules["plugins.plugin_storage"] = storage


def _load_tools():
    path = Path(__file__).resolve().parents[1] / "tools.py"
    spec = importlib.util.spec_from_file_location("hermes_gauge_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_schemas():
    path = Path(__file__).resolve().parents[1] / "schemas.py"
    spec = importlib.util.spec_from_file_location("hermes_gauge_schemas", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_install_fake_plugins()
tools = _load_tools()
# Tests exercise the dict-returning logic; the public entry points wrap them
# into strings for the Hermes runtime. Keep the string wrappers reachable.
tools._string_check = tools.gauge_check
tools._string_report = tools.gauge_report
tools.gauge_check = tools._gauge_check
tools.gauge_report = tools._gauge_report

# Default window is 200_000 tokens.
# estimated_tokens = (chars // 4) * 2, then ratio = estimated / window.
# Bands: OK < 0.60, WATCH >= 0.60, FULL >= 0.85, fill capped at 100%.
BANDS = [
    # chars, estimated, fill_percent, remaining, status
    (0, 0, 0.0, 200_000, "OK"),
    (200_000, 100_000, 50.0, 100_000, "OK"),
    (239_600, 119_800, 59.9, 80_200, "OK"),
    (240_000, 120_000, 60.0, 80_000, "WATCH"),
    (280_000, 140_000, 70.0, 60_000, "WATCH"),
    (339_600, 169_800, 84.9, 30_200, "WATCH"),
    (340_000, 170_000, 85.0, 30_000, "FULL"),
    (380_000, 190_000, 95.0, 10_000, "FULL"),
    (800_000, 400_000, 100.0, 0, "FULL"),
]


# Reports join Hermes' state.db for titles. Tests never read the real one.
NO_STATE_DB = "/nonexistent/hermes-gauge-tests/state.db"


@pytest.fixture(autouse=True)
def memory_db():
    conn = sqlite3.connect(":memory:")
    _CONN["conn"] = conn
    tools._BUFFERS.clear()
    tools._PLUGIN_CTX = None
    tools._ALERT_SINK = None
    tools._DATA_DIR = None
    tools._CONFIG_PATH = None
    tools._ROUTE_SENDER = None
    tools._STATE_DB = NO_STATE_DB
    tools._HERMES_HOME = None
    tools._HERMES_COMMAND = None
    yield conn
    conn.close()
    _CONN["conn"] = None
    tools._BUFFERS.clear()
    tools._PLUGIN_CTX = None
    tools._ALERT_SINK = None
    tools._DATA_DIR = None
    tools._CONFIG_PATH = None
    tools._ROUTE_SENDER = None
    tools._STATE_DB = None
    tools._HERMES_HOME = None
    tools._HERMES_COMMAND = None


def test_tools_use_the_replaced_plugin_db_import():
    assert tools.plugin_db is _plugin_db


def test_record_llm_call_accumulates_and_keeps_an_existing_model(memory_db):
    tools.record_llm_call("sess", 100, model="alpha")
    tools.record_llm_call("sess", 40, model=None)
    tools.record_llm_call("sess", None, model=None)

    row = memory_db.execute(
        "SELECT chars_in, turns, model FROM usage WHERE session_id = ?",
        ("sess",),
    ).fetchone()
    assert row == (140, 3, "alpha")


def test_record_llm_call_adopts_a_model_once_one_arrives(memory_db):
    tools.record_llm_call("sess", 10, model=None)
    tools.record_llm_call("sess", 15, model="beta")

    row = memory_db.execute(
        "SELECT chars_in, turns, model FROM usage WHERE session_id = ?",
        ("sess",),
    ).fetchone()
    assert row == (25, 2, "beta")


def test_record_llm_call_keeps_first_seen_and_refreshes_last_seen(memory_db, monkeypatch):
    clock = iter((1_000.0, 2_500.0))
    monkeypatch.setattr(tools.time, "time", lambda: next(clock))

    tools.record_llm_call("sess", 8, model="alpha")
    tools.record_llm_call("sess", 8, model="alpha")

    first, last = memory_db.execute(
        "SELECT first_seen, last_seen FROM usage WHERE session_id = ?",
        ("sess",),
    ).fetchone()
    assert first == 1_000.0
    assert last == 2_500.0


def test_record_llm_call_ignores_a_missing_session():
    assert tools.record_llm_call("", 500, model="alpha") is None
    assert tools.record_llm_call(None, 500, model="alpha") is None

    report = tools.gauge_report()
    assert report["sessions"] == []
    assert report["session_count"] == 0
    assert report["total_estimated_tokens"] == 0


def test_gauge_check_requires_a_session():
    error = {"error": "No active session — cannot measure."}
    assert tools.gauge_check() == error
    assert tools.gauge_check(session_id="") == error
    assert tools.gauge_check(session_id=None) == error


def test_gauge_check_unknown_session_is_empty_and_ok():
    result = tools.gauge_check(session_id="missing", model="opus")
    assert result["status"] == "OK"
    assert result["fill_percent"] == 0.0
    assert result["estimated_tokens"] == 0
    assert result["remaining_tokens"] == 200_000
    assert result["turns"] == 0
    assert result["model"] == "opus"
    assert result["recommendation"] == "Plenty of headroom. Carry on."


@pytest.mark.parametrize("chars,estimated,fill,remaining,status", BANDS)
def test_gauge_check_status_bands(chars, estimated, fill, remaining, status):
    # opus is a pro model, so the bands are scored on the full 200_000 window.
    tools.record_llm_call("sess", chars, model="opus")
    result = tools.gauge_check(session_id="sess")

    assert result["status"] == status
    assert result["estimated_tokens"] == estimated
    assert result["fill_percent"] == fill
    assert result["remaining_tokens"] == remaining
    assert result["window_tokens"] == 200_000
    assert result["turns"] == 1

    if status == "FULL":
        assert result["recommendation"] == (
            "Context almost exhausted. Summarize what matters, then start "
            "a fresh session (/new) and paste the summary."
        )
    elif status == "WATCH":
        assert result["recommendation"] == (
            f"About {remaining // 1000}k tokens of headroom left. "
            "Finish the current topic first; avoid starting anything big."
        )
    else:
        assert result["recommendation"] == "Plenty of headroom. Carry on."


def test_gauge_report_orders_fullest_first_and_labels_each_band():
    tools.record_llm_call("ok-topic", 200_000, model="opus")           # 50% OK
    tools.record_llm_call("full-topic", 380_000, model="claude-opus")  # 95% FULL
    tools.record_llm_call("watch-topic", 280_000, model="opus")        # 70% WATCH

    report = tools.gauge_report()
    assert [s["session_id"] for s in report["sessions"]] == [
        "full-topic",
        "watch-topic",
        "ok-topic",
    ]
    assert [s["status"] for s in report["sessions"]] == ["FULL", "WATCH", "OK"]
    assert [s["fill_percent"] for s in report["sessions"]] == [95.0, 70.0, 50.0]
    assert report["session_count"] == 3
    assert report["total_estimated_tokens"] == 190_000 + 140_000 + 100_000
    assert isinstance(report["generated_at"], float)


def test_gauge_report_limit_keeps_the_fullest_sessions():
    tools.record_llm_call("small", 10_000, model="hermes-3")
    tools.record_llm_call("mid", 100_000, model="hermes-3")
    tools.record_llm_call("large", 300_000, model="hermes-3")

    report = tools.gauge_report(limit=2)
    assert [s["session_id"] for s in report["sessions"]] == ["large", "mid"]
    assert report["session_count"] == 2
    assert report["total_estimated_tokens"] == sum(
        s["estimated_tokens"] for s in report["sessions"]
    )


def test_gauge_set_window_moves_the_same_usage_across_bands():
    # 3_600 chars -> 1_800 estimated tokens.
    tools.record_llm_call("sess", 3_600, model="opus")

    baseline = tools.gauge_check(session_id="sess")
    assert baseline["status"] == "OK"
    assert baseline["window_tokens"] == 200_000
    assert baseline["estimated_tokens"] == 1_800
    assert baseline["fill_percent"] == 0.9

    # 1_800 / 3_000 = 60% -> WATCH
    updated = tools.gauge_set_window("opus", window_tokens=3_000)
    assert updated == {"model": "opus", "window_tokens": 3000, "reset": False}
    watched = tools.gauge_check(session_id="sess")
    assert watched["status"] == "WATCH"
    assert watched["window_tokens"] == 3_000
    assert watched["fill_percent"] == 60.0
    assert watched["remaining_tokens"] == 1_200
    assert watched["recommendation"].startswith("About 1k tokens of headroom left.")

    # 1_800 / 2_000 = 90% -> FULL
    tools.gauge_set_window("opus", window_tokens=2_000)
    full = tools.gauge_check(session_id="sess")
    assert full["status"] == "FULL"
    assert full["window_tokens"] == 2_000
    assert full["fill_percent"] == 90.0
    assert full["remaining_tokens"] == 200
    assert full["recommendation"].startswith("Context almost exhausted.")

    reset = tools.gauge_set_window("opus")
    assert reset == {"model": "opus", "window_tokens": 200_000, "reset": True}
    restored = tools.gauge_check(session_id="sess")
    assert restored["status"] == "OK"
    assert restored["window_tokens"] == 200_000
    assert restored["fill_percent"] == 0.9


def test_gauge_set_window_reset_without_an_override_uses_the_default():
    result = tools.gauge_set_window("never-set")
    assert result == {"model": "never-set", "window_tokens": 200_000, "reset": True}


def _config(**overrides):
    config = {
        "thresholds": {"watch": 0.60, "full": 0.85},
        "output_reserve": 0,
        "fallback_window": 200_000,
        "models": {},
    }
    config.update(overrides)
    return config


def test_shipped_config_keeps_the_historical_thresholds():
    config = tools.load_config()
    assert config["thresholds"]["watch"] == 0.60
    assert config["thresholds"]["full"] == 0.85
    assert config["output_reserve"] == 0
    assert config["fallback_window"] == 200_000
    alerts = config["gauge"]["alerts"]
    assert alerts["enabled"] is True
    assert alerts["on"] == ["WATCH", "FULL"]
    assert alerts["cooldown_minutes"] == 30


def test_alert_config_parses_inline_lists_and_booleans():
    parsed = tools.gauge_math.parse_config(
        "gauge:\n"
        "  alerts:\n"
        "    enabled: false\n"
        "    on: [FULL]\n"
        "    cooldown_minutes: 5\n"
    )
    alerts = parsed["gauge"]["alerts"]
    assert alerts["enabled"] is False
    assert alerts["on"] == ["FULL"]
    assert alerts["cooldown_minutes"] == 5

    merged = tools.gauge_math.merge_config(
        tools.gauge_math.default_config(),
        {"gauge": {"alerts": {"enabled": False}}},
    )
    assert merged["gauge"]["alerts"]["enabled"] is False
    assert merged["gauge"]["alerts"]["on"] == ["WATCH", "FULL"]
    assert merged["thresholds"]["watch"] == 0.60
    assert tools.gauge_math.alert_settings(merged)["enabled"] is False
    assert tools.gauge_math.percent_label(92.0) == "92"
    assert tools.gauge_math.percent_label(84.9) == "84.9"
    assert tools.gauge_math.percent_label(None) is None


def test_fill_never_exceeds_100_or_drops_below_zero():
    tools.record_llm_call("huge", 10**12, model="hermes-3")
    huge = tools.gauge_check(session_id="huge")
    assert huge["fill_percent"] == 100.0
    assert huge["remaining_tokens"] == 0
    assert 0 <= huge["fill_percent"] <= 100

    tools.record_llm_call("negative", -50_000, model="hermes-3")
    low = tools.gauge_check(session_id="negative")
    assert low["estimated_tokens"] == 0
    assert low["fill_percent"] == 0.0


def test_window_source_is_fallback_until_something_real_is_known():
    result = tools.gauge_check(session_id="missing", model="opus")
    assert result["window_source"] == "fallback"
    assert result["window_tokens"] == 200_000
    assert result["approximate"] is True
    assert result["fill_display"].startswith("≈")


def test_detected_context_length_beats_the_fallback():
    tools.record_llm_call(
        "sess", 0, model="opus-real", context_length=1_000_000, confirm_window=True
    )
    result = tools.gauge_check(session_id="sess")
    assert result["window_source"] == "detected"
    assert result["window_tokens"] == 1_000_000
    assert result["nominal_window"] == 1_000_000


def test_config_matches_exact_model_ids_not_aliases(monkeypatch):
    monkeypatch.setattr(tools, "load_config", lambda: _config(models={
        "claude-opus-4-6": 1_000_000,
    }))
    tools.record_llm_call("alias", 100, model="claude-opus")
    alias = tools.gauge_check(session_id="alias")
    assert alias["window_source"] == "fallback"
    assert alias["window_tokens"] == 200_000

    tools.record_llm_call("exact", 100, model="claude-opus-4-6")
    exact = tools.gauge_check(session_id="exact")
    assert exact["window_source"] == "configured"
    assert exact["window_tokens"] == 1_000_000


def test_session_override_does_not_leak_onto_another_session():
    tools.record_llm_call("alpha", 100, model="opus")
    tools.record_llm_call("beta", 100, model="opus")

    updated = tools.gauge_set_window("opus", window_tokens=50_000, session_id="alpha")
    assert updated["window_source"] == "configured"
    assert updated["session_id"] == "alpha"
    assert updated["reset"] is False

    alpha = tools.gauge_check(session_id="alpha")
    beta = tools.gauge_check(session_id="beta")
    assert alpha["window_tokens"] == 50_000
    assert alpha["window_source"] == "configured"
    assert beta["window_tokens"] == 200_000
    assert beta["window_source"] == "fallback"

    # A model-level override still serves sessions that have no session override.
    tools.gauge_set_window("opus", window_tokens=80_000)
    assert tools.gauge_check(session_id="alpha")["window_tokens"] == 50_000
    assert tools.gauge_check(session_id="beta")["window_tokens"] == 80_000
    assert tools.gauge_check(session_id="beta")["window_source"] == "configured"

    tools.gauge_set_window("opus", session_id="alpha")
    assert tools.gauge_check(session_id="alpha")["window_tokens"] == 80_000


def test_unconfirmed_window_is_an_em_dash_not_an_invented_percent():
    tools.record_llm_call("sess", 50_000, model="mystery", confirm_window=False)
    result = tools.gauge_check(session_id="sess")
    assert result["window_source"] == "unresolved"
    assert result["fill_display"] == "\u2014"
    assert result["fill_percent"] is None
    assert result["status"] == "UNKNOWN"
    assert result["window_tokens"] is None


def test_compaction_invalidates_the_usage_anchor():
    tools.record_llm_call(
        "sess", 800_000, model="opus", context_length=200_000, confirm_window=True
    )
    assert tools.gauge_check(session_id="sess")["fill_percent"] == 100.0

    tools.handle_compaction(session_id="sess", reason="overflow")
    stale = tools.gauge_check(session_id="sess")
    assert stale["fill_display"] == "\u2014"
    assert stale["fill_percent"] is None
    assert stale["status"] == "UNKNOWN"

    snapshot = tools.compaction_events("sess")[0]["snapshot"]
    assert snapshot["fill_percent"] == 100.0
    assert snapshot["reason"] == "overflow"

    tools.record_llm_call(
        "sess", 3_600, model="opus", context_length=200_000, confirm_window=True
    )
    fresh = tools.gauge_check(session_id="sess")
    assert fresh["window_source"] == "detected"
    assert fresh["estimated_tokens"] == 1_800
    assert fresh["fill_percent"] == 0.9
    assert 0 <= fresh["fill_percent"] <= 100


def test_new_session_does_not_keep_the_previous_fill():
    tools.record_llm_call("chat", 800_000, model="opus")
    assert tools.gauge_check(session_id="chat")["fill_percent"] == 100.0

    tools.handle_session_reset(
        session_id="chat-2",
        old_session_id="chat",
        new_session_id="chat-2",
        reason="new",
    )
    fresh = tools.gauge_check(session_id="chat-2")
    assert fresh["fill_percent"] == 0.0
    assert fresh["estimated_tokens"] == 0
    old = tools.gauge_check(session_id="chat")
    assert old["fill_display"] == "\u2014"
    assert old["fill_percent"] is None


def test_stream_chunks_commit_once_and_a_later_final_does_not_double_count():
    tools.handle_stream_delta(session_id="sess", turn_id="t", delta="aa", model="m")
    tools.handle_stream_delta(session_id="sess", turn_id="t", delta="bb", model="m")
    assert tools.gauge_report()["sessions"] == []

    tools.handle_stream_end(
        session_id="sess", turn_id="t", final_text="aabb", finished=True, model="m"
    )
    reading = tools.gauge_check(session_id="sess")
    assert reading["estimated_tokens"] == 2
    assert reading["fill_percent"] is None

    tools.handle_post_llm_call(
        session_id="sess", turn_id="t", assistant_response="aabb", model="m"
    )
    again = tools.gauge_check(session_id="sess")
    assert again["estimated_tokens"] == 2


def test_gauge_check_flushes_an_unfinished_stream_and_logs_the_miss():
    tools.handle_stream_delta(session_id="sess", turn_id="t", delta="abcd", model="m")
    reading = tools.gauge_check(session_id="sess")
    assert reading["estimated_tokens"] == 2
    assert any(event["kind"] == "missed" for event in tools.hook_events("sess"))

    tools.handle_stream_end(
        session_id="sess", turn_id="t", final_text="abcdefghij", finished=True, model="m"
    )
    final = tools.gauge_check(session_id="sess")
    assert final["estimated_tokens"] == 4


def test_provider_error_is_logged_and_does_not_invent_a_full_gauge():
    tools.handle_api_error(session_id="sess", error="429", model="m")
    kinds = {event["kind"] for event in tools.hook_events("sess")}
    assert "missed" in kinds
    assert "provider_error" in kinds
    reading = tools.gauge_check(session_id="sess")
    assert reading["fill_display"] == "\u2014"
    assert reading["fill_percent"] is None

    tools.handle_api_error(
        session_id="known", error="overloaded", model="opus", context_length=128_000
    )
    known = tools.gauge_check(session_id="known")
    assert known["window_source"] == "detected"
    assert known["window_tokens"] == 128_000
    assert known["fill_percent"] == 0.0


def test_post_llm_call_accepts_a_streaming_payload():
    tools.handle_post_llm_call(session_id="sess", turn_id="t", delta="xyz", model="m")
    assert tools.gauge_report()["sessions"] == []
    tools.handle_post_llm_call(
        session_id="sess", turn_id="t", final_text="xyzXYZ", finished=True, model="m",
        context_length=200_000,
    )
    reading = tools.gauge_check(session_id="sess")
    assert reading["window_source"] == "detected"
    assert reading["estimated_tokens"] == (6 // 4) * 2
    assert reading["fill_percent"] <= 100


def test_provider_usage_is_exact_and_not_the_char_heuristic():
    tools.handle_post_api_request(
        session_id="sess",
        model="opus",
        usage={"prompt_tokens": 50_000, "context_length": 200_000},
    )
    reading = tools.gauge_check(session_id="sess")
    assert reading["window_source"] == "detected"
    assert reading["estimated_tokens"] == 50_000
    assert reading["approximate"] is False
    assert reading["fill_percent"] == 25.0
    assert reading["fill_display"] == "25.0%"

    tools.handle_post_llm_call(
        session_id="sess", turn_id="t", assistant_response="hi", model="opus"
    )
    again = tools.gauge_check(session_id="sess")
    assert again["estimated_tokens"] == 50_000
    assert again["fill_percent"] == 25.0


def test_gauge_check_recomputes_when_the_hook_never_ran():
    reading = tools.gauge_check(
        session_id="fresh",
        model="opus",
        conversation_history=[
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
        ],
        context_length=1_000,
    )
    assert any(event["kind"] == "missed" for event in tools.hook_events("fresh"))
    assert reading["window_source"] == "detected"
    assert reading["window_tokens"] == 1_000
    assert reading["approximate"] is True
    assert sum(reading["breakdown"].values()) == reading["estimated_tokens"]
    assert reading["breakdown"]["system_prompt"] > 0
    assert 0 <= reading["fill_percent"] <= 100


def test_watch_notifies_once_and_records_what_is_in_flight():
    tools.record_llm_call("sess", 240_000, model="opus")
    first = tools.gauge_check(session_id="sess")
    second = tools.gauge_check(session_id="sess")
    assert first["status"] == "WATCH"
    assert first["notification"]
    assert second["notification"] is None
    assert first["in_flight"]["breakdown"]["conversation"] == first["estimated_tokens"]
    assert len(tools.compaction_events("sess")) == 1
    assert sum(first["breakdown"].values()) == first["estimated_tokens"]


def test_output_reserve_is_deducted_from_the_nominal_window(monkeypatch):
    monkeypatch.setattr(tools, "load_config", lambda: _config(output_reserve=1_000))
    tools.record_llm_call(
        "sess", 0, model="opus", context_length=10_000, confirm_window=True
    )
    reading = tools.gauge_check(session_id="sess")
    assert reading["nominal_window"] == 10_000
    assert reading["output_reserve"] == 1_000
    assert reading["window_tokens"] == 9_000
    assert reading["window_source"] == "detected"


def test_thresholds_come_from_config(monkeypatch):
    monkeypatch.setattr(tools, "load_config", lambda: _config(
        thresholds={"watch": 0.10, "full": 0.20},
    ))
    tools.record_llm_call("watch", 60_000, model="opus")
    watched = tools.gauge_check(session_id="watch")
    assert watched["fill_percent"] == 15.0
    assert watched["status"] == "WATCH"

    tools.record_llm_call("full", 100_000, model="opus")
    assert tools.gauge_check(session_id="full")["status"] == "FULL"


def test_multi_agent_status_uses_the_smallest_window():
    tools.record_llm_call(
        "subj", 2_000, model="opus", agent_id="a",
        context_length=1_000_000, confirm_window=True,
    )
    tools.record_llm_call(
        "subj", 18_000, model="gpt-4o-mini", agent_id="b",
        context_length=10_000, confirm_window=True,
    )
    reading = tools.gauge_check(session_id="subj")
    assert reading["window_label"] == "window: smallest model of the subject"
    # The limiting model is gpt-4o-mini, so the mini family (0.40) applies.
    assert reading["degrade_factor"] == 0.40
    assert reading["window_tokens"] == 4_000
    assert reading["nominal_window"] == 10_000
    assert reading["estimated_tokens"] == 10_000
    assert reading["fill_percent"] == 100.0
    assert reading["status"] == "FULL"
    assert {agent["model"] for agent in reading["agents"]} == {"opus", "gpt-4o-mini"}

    tools.record_llm_call(
        "solo", 2_000, model="opus", agent_id="a",
        context_length=1_000_000, confirm_window=True,
    )
    solo = tools.gauge_check(session_id="solo")
    assert solo["window_label"] is None
    assert solo["window_source"] == "detected"
    assert solo["window_tokens"] == 1_000_000


def test_subagent_stop_folds_the_child_into_the_parent_subject():
    tools.record_llm_call(
        "parent", 2_000, model="opus", context_length=1_000_000, confirm_window=True
    )
    tools.record_llm_call(
        "child", 18_000, model="gpt-4o-mini", context_length=10_000, confirm_window=True
    )
    attached = tools.handle_subagent_stop(
        parent_session_id="parent", child_session_id="child"
    )
    assert attached["attached"] is True
    reading = tools.gauge_check(session_id="parent")
    assert reading["window_label"] == "window: smallest model of the subject"
    # The child model is gpt-4o-mini, so the mini family (0.40) applies.
    assert reading["degrade_factor"] == 0.40
    assert reading["window_tokens"] == 4_000
    assert reading["nominal_window"] == 10_000
    assert reading["status"] == "FULL"
    assert reading["fill_percent"] == 100.0


def test_manifest_lists_the_hooks_and_leaves_the_demo_out():
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
    nested = (root / "hermes-gauge" / "plugin.yaml").read_text(encoding="utf-8")
    for name in ("gauge_check", "gauge_report", "gauge_set_window", "gauge_render", "gauge_action"):
        assert name in manifest
        assert name in nested
    for hook in (
        "post_llm_call", "on_stream_delta", "on_stream_end", "api_request_error",
        "post_api_request", "post_compaction", "on_session_reset", "subagent_stop",
    ):
        assert hook in manifest
    assert "webapp-demo" not in manifest
    assert "version: 0.6.1" in manifest
    assert "version: 0.6.1" in nested


def test_root_entry_registers_the_same_hooks():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("hermes_gauge_root", root / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = []
            self.skills = []
            self.commands = []

        def register_tool(self, name, toolset, schema, handler):
            self.tools.append(name)

        def register_hook(self, name, fn):
            self.hooks.append(name)

        def register_skill(self, name, path):
            self.skills.append(name)

        def register_command(self, name, handler, description="", args_hint=""):
            self.commands.append(name)

    ctx = Ctx()
    module.register(ctx)
    assert ctx.tools == [
        "gauge_check", "gauge_report", "gauge_set_window", "gauge_render",
        "gauge_action",
    ]
    assert ctx.commands == ["gauge"]
    assert "post_llm_call" in ctx.hooks
    assert "post_compaction" in ctx.hooks
    assert "on_stream_end" in ctx.hooks
    assert ctx.skills == ["gauge-viewer"]


def _capture_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(tools, "_ALERT_SINK", lambda payload: sent.append(payload))
    return sent


def test_watch_and_full_push_once_per_status_and_honor_cooldown(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    clock = {"t": 1_000.0}
    monkeypatch.setattr(tools.time, "time", lambda: clock["t"])

    tools.record_llm_call("release-review", 280_000, model="opus")
    first = tools.gauge_check(session_id="release-review")
    assert first["status"] == "WATCH"
    assert first["notification"]
    first_push = _status_pushes(sent)
    assert len(first_push) == 1
    assert first_push[0]["session_id"] == "release-review"
    # One bubble: the WATCH sentence, then the dull recommendation under it.
    assert first_push[0]["message"].startswith(_watch_text("release-review"))
    assert "seuil débile" in first_push[0]["message"]

    tools.gauge_check(session_id="release-review")
    assert len(_status_pushes(sent)) == 1

    clock["t"] += 60
    tools.record_llm_call("release-review", 10_000, model="opus", replace=True)
    assert tools.gauge_check(session_id="release-review")["status"] == "OK"
    tools.record_llm_call("release-review", 280_000, model="opus", replace=True)
    again = tools.gauge_check(session_id="release-review")
    assert again["status"] == "WATCH"
    assert again["notification"]
    assert len(_status_pushes(sent)) == 1

    tools.record_llm_call("release-review", 10_000, model="opus", replace=True)
    tools.gauge_check(session_id="release-review")
    clock["t"] += 31 * 60
    tools.record_llm_call("release-review", 280_000, model="opus", replace=True)
    tools.gauge_check(session_id="release-review")
    status_pushes = _status_pushes(sent)
    assert len(status_pushes) == 2
    assert "70% full" in status_pushes[1]["message"]

    tools.record_llm_call("release-review", 340_000, model="opus", replace=True)
    full = tools.gauge_check(session_id="release-review")
    assert full["status"] == "FULL"
    assert _status_pushes(sent)[-1]["message"].startswith(_full_text("release-review"))
    tools.gauge_check(session_id="release-review")
    assert len(_status_pushes(sent)) == 3


def test_alerts_stay_quiet_when_disabled_or_the_status_is_not_listed(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(tools, "load_config", lambda: _config(gauge={
        "alerts": {"enabled": False, "on": ["WATCH", "FULL"], "cooldown_minutes": 30},
    }))
    tools.record_llm_call("sess", 280_000, model="opus")
    reading = tools.gauge_check(session_id="sess")
    assert reading["status"] == "WATCH"
    assert reading["notification"]
    assert _status_pushes(sent) == []

    monkeypatch.setattr(tools, "load_config", lambda: _config(gauge={
        "alerts": {"enabled": True, "on": ["FULL"], "cooldown_minutes": 30},
    }))
    tools.record_llm_call("other", 280_000, model="opus")
    assert tools.gauge_check(session_id="other")["status"] == "WATCH"
    assert _status_pushes(sent) == []
    tools.record_llm_call("other", 340_000, model="opus", replace=True)
    assert tools.gauge_check(session_id="other")["status"] == "FULL"
    assert len(_status_pushes(sent)) == 1
    assert "85% full" in _status_pushes(sent)[0]["message"]


def test_hook_pushes_when_an_active_session_crosses_watch(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    tools.handle_post_llm_call(
        session_id="release-review",
        assistant_response="x" * 280_000,
        model="opus",
        context_length=200_000,
    )
    assert len(_status_pushes(sent)) == 1
    assert _status_pushes(sent)[0]["message"].startswith(_watch_text("release-review"))

    tools.handle_post_llm_call(
        session_id="release-review",
        assistant_response="y",
        model="opus",
        context_length=200_000,
    )
    assert len(_status_pushes(sent)) == 1
    follow = tools.gauge_check(session_id="release-review")
    assert follow["notification"] is None
    assert follow["status"] == "WATCH"


def test_alert_posts_on_the_bound_session_channel():
    posts = []

    class Ctx:
        def post_message(self, text):
            posts.append(text)
            return {"ok": True}

    tools.bind_context(Ctx())
    tools.record_llm_call("release-review", 340_000, model="opus")
    tools.gauge_check(session_id="release-review")
    assert len(posts) == 1
    assert posts[0].startswith(_full_text("release-review"))
    assert sum("85% full" in text for text in posts) == 1


def test_gauge_report_names_the_weakest_link_before_the_detail():
    tools.record_llm_call("big-chars", 200_000, model="opus")
    tools.gauge_set_window("opus", window_tokens=2_000, session_id="small-chars")
    tools.record_llm_call("small-chars", 3_600, model="opus")

    report = tools.gauge_report()
    assert [s["session_id"] for s in report["sessions"]] == ["big-chars", "small-chars"]
    assert report["summary"] == (
        "2 active sessions — weakest link: small-chars 90% FULL"
    )
    assert report["weakest_link"]["session_id"] == "small-chars"
    assert report["weakest_link"]["fill_percent"] == 90.0
    assert report["weakest_link"]["status"] == "FULL"
    assert report["format"] == "text"
    assert report["text"].splitlines()[0] == report["summary"]

    empty = tools.gauge_report(limit=0)
    assert empty["summary"] == "0 active sessions — weakest link: none"
    assert empty["weakest_link"] is None


def test_gauge_report_markdown_and_json_are_stable(monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: 1_700_000_000.0)
    tools.record_llm_call("release-review", 380_000, model="claude-opus")
    tools.record_llm_call("standup-notes", 200_000, model="opus")

    markdown = tools.gauge_report(format="md")
    body = markdown["markdown"]
    assert body.startswith("# Hermes Gauge\n")
    assert "<u>**release-review 95% FULL**</u>" in body
    assert "| Session | Fill | Status | Turns | Model |" in body
    assert "<u>**release-review**</u>" in body
    assert "| standup-notes | 50% | OK | 1 | opus |" in body
    assert "_Generated 2023-11-14 22:13 UTC_" in body
    assert "json" not in markdown or markdown["format"] == "md"
    assert "text" not in markdown

    payload = tools.gauge_report(format="json")
    assert payload["format"] == "json"
    assert "markdown" not in payload
    assert "text" not in payload
    assert payload["summary"] == (
        "2 active sessions — weakest link: release-review 95% FULL"
    )
    assert set(payload["weakest_link"]) == {
        "session_id", "name", "model", "fill_percent", "fill_display", "status", "turns",
    }
    assert payload["weakest_link"]["fill_percent"] == 95.0
    assert payload["session_count"] == 2
    assert isinstance(payload["generated_at"], float)

    fallback = tools.gauge_report(format="nope")
    assert fallback["format"] == "text"
    assert fallback["text"].startswith(fallback["summary"])


def test_gauge_render_draws_the_global_svg_and_can_write_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path)
    tools.record_llm_call("release-review", 380_000, model="claude-opus")
    tools.record_llm_call("standup-notes", 40_000, model="hermes-3")

    drawn = tools.gauge_render()
    assert drawn["written"] is False
    assert drawn["path"] is None
    assert drawn["session_id"] == "release-review"
    assert drawn["status"] == "FULL"
    assert drawn["fill_percent"] == 95.0
    assert drawn["svg"].startswith("<svg ")
    assert 'fill="#032A27"' in drawn["svg"]
    assert 'fill="#C45C48"' in drawn["svg"]
    assert drawn["svg"].count("<rect ") == 1 + 9 + tools.gauge_math.lit_pills(95.0)
    assert "http://" not in drawn["svg"].split(">", 1)[-1]
    assert not (tmp_path / "gauge.svg").exists()

    saved = tools.gauge_render(write=True)
    path = tmp_path / "gauge.svg"
    assert saved["written"] is True
    assert saved["path"] == str(path)
    assert path.read_text(encoding="utf-8") == saved["svg"]

    tools.record_llm_call("calm", 40_000, model="hermes-3")
    tools.gauge_render(write="true")
    files = list(tmp_path.glob("*.svg"))
    assert files == [path]

    watch = tools.gauge_math.render_svg(70, "WATCH", session_id="notes")
    assert 'fill="#E8B54A"' in watch
    assert 'fill="#C45C48"' not in watch
    assert tools.gauge_math.lit_pills(0) == 0
    assert tools.gauge_math.lit_pills(92) == 8
    assert tools.gauge_math.lit_pills(100) == 9


def test_slash_gauge_answers_in_one_line_or_with_the_report():
    tools.record_llm_call("release-review", 280_000, model="opus")
    line = tools.gauge_slash(session_id="release-review")
    assert line == "release-review — ≈70.0% WATCH"
    report = tools.gauge_slash("report")
    assert report.splitlines()[0] == (
        "1 active session — weakest link: release-review 70% WATCH"
    )
    md = tools.gauge_slash("md")
    assert md.startswith("# Hermes Gauge\n")


# --- v0.5: preferences, alert route, chars_per_token ------------------------


def _config_file(tmp_path, monkeypatch, text=None):
    target = tmp_path / "config.yaml"
    shutil.copy(Path(tools.__file__).with_name("config.yaml"), target)
    if text is not None:
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(tools, "_CONFIG_PATH", target)
    return target


def test_slash_config_shows_every_preference_and_where_it_comes_from(tmp_path, monkeypatch):
    _config_file(tmp_path, monkeypatch)
    view = tools.gauge_slash("config")
    assert view.splitlines()[0] == "Hermes Gauge — preferences"
    for key in ("thresholds.watch", "thresholds.full", "alerts.enabled", "alerts.on",
                "alerts.cooldown_minutes", "alerts.route", "output_reserve", "chars_per_token"):
        assert key in view
    assert "alerts.route" in view and "session  (default)" in view
    assert tools.gauge_slash("config show") == view


def test_slash_config_set_persists_and_the_tools_read_it_back(tmp_path, monkeypatch):
    target = _config_file(tmp_path, monkeypatch)
    answer = tools.gauge_slash(
        "config set alerts.cooldown_minutes=5 thresholds.watch=50% alerts.route=home"
    )
    assert answer.splitlines() == [
        "Saved.",
        "alerts.cooldown_minutes = 5 min",
        "thresholds.watch = 50 %",
        "alerts.route = home",
    ]
    assert "cooldown_minutes: 5" in target.read_text(encoding="utf-8")
    config = tools.load_config()
    assert config["thresholds"]["watch"] == 0.5
    settings = tools.gauge_math.alert_settings(config)
    assert (settings["cooldown_minutes"], settings["route"]) == (5, "home")
    # The new watch level reaches the reading itself, not only the file.
    tools.record_llm_call("sess", 220_000, model="opus")
    assert tools.gauge_check(session_id="sess")["status"] == "WATCH"


def test_slash_config_keeps_the_case_of_chat_ids(tmp_path, monkeypatch):
    _config_file(tmp_path, monkeypatch)
    tools.gauge_slash("config set alerts.route=custom:slack:C0123ABCD")
    assert tools.load_config()["gauge"]["alerts"]["route"] == "custom:slack:C0123ABCD"


def test_slash_config_refuses_bad_values_and_changes_nothing(tmp_path, monkeypatch):
    target = _config_file(tmp_path, monkeypatch)
    before = target.read_text(encoding="utf-8")
    answer = tools.gauge_slash("config set alerts.route=mars thresholds.watch=0.9")
    assert answer.startswith("Nothing was changed.")
    assert "alerts.route:" in answer
    assert target.read_text(encoding="utf-8") == before
    assert tools.gauge_slash("config set").startswith("Usage: /gauge config")
    assert tools.gauge_slash("config bogus").startswith("Usage: /gauge config")


def test_config_api_returns_dicts_with_sources(tmp_path, monkeypatch):
    _config_file(tmp_path, monkeypatch)
    snapshot = tools.config_view()
    assert isinstance(snapshot, dict) and snapshot["flat"]["alerts.route"] == "session"
    result = tools.config_set({"alerts.route": "home:telegram"})
    assert result["ok"] is True and result["changed"] == {"alerts.route": "home:telegram"}
    assert tools.config_view()["sources"]["alerts.route"] == "file"


def _route_config(tmp_path, monkeypatch, route):
    target = _config_file(tmp_path, monkeypatch)
    tools.config_set({"alerts.route": route})
    return target


def test_default_route_keeps_the_alert_payload_exactly_as_before(tmp_path, monkeypatch):
    _config_file(tmp_path, monkeypatch)
    sent = _capture_alerts(monkeypatch)
    tools.record_llm_call("sess", 280_000, model="opus")
    tools.gauge_check(session_id="sess")
    assert sent == [
        {
            "message": (
                _watch_text("sess")
                + "\n\nHermes Gauge — session sess : 70 % effectif, seuil débile 70 % : "
                "recommandé = compacter, puis nouvelle session."
            ),
            "session_id": "sess",
        },
    ]
    assert all("route" not in item for item in sent)


def test_custom_route_goes_through_hermes_send_not_the_session(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "custom:telegram:-1001234567890")
    sends = []
    monkeypatch.setattr(tools, "_ROUTE_SENDER", lambda target, message: sends.append((target, message)) or True)
    posts = []

    class Ctx:
        def post_message(self, text):
            posts.append(text)
            return {"ok": True}

    tools.bind_context(Ctx())
    tools.record_llm_call("sess", 340_000, model="opus")
    tools.gauge_check(session_id="sess")
    assert sends == [
        (
            "telegram:-1001234567890",
            _full_text("sess")
            + "\n\nHermes Gauge — session sess : 85 % effectif, seuil débile 70 % : "
            "recommandé = compacter, puis nouvelle session.",
        ),
    ]
    assert posts == []


def test_home_route_follows_the_platform_the_session_lives_on(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "home")
    sends = []
    monkeypatch.setattr(tools, "_ROUTE_SENDER", lambda target, message: sends.append(target) or True)

    class Ctx:
        def session_surface(self):
            return "Telegram"

        def post_message(self, text):
            raise AssertionError("home must not post into the session thread")

    tools.bind_context(Ctx())
    tools.record_llm_call("sess", 340_000, model="m")
    tools.gauge_check(session_id="sess")
    assert sends == ["telegram"]


def test_named_home_route_does_not_need_a_messaging_session(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "home:telegram")
    sends = []
    monkeypatch.setattr(tools, "_ROUTE_SENDER", lambda target, message: sends.append(target) or True)

    class Ctx:
        def session_surface(self):
            return "desktop"

    tools.bind_context(Ctx())
    tools.record_llm_call("sess", 340_000, model="m")
    tools.gauge_check(session_id="sess")
    assert sends == ["telegram"]


def test_home_route_on_a_local_surface_stays_on_the_session_instead_of_vanishing(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "home")
    sink = _capture_alerts(monkeypatch)

    class Ctx:
        def session_surface(self):
            return "desktop"

    tools.bind_context(Ctx())
    tools.record_llm_call("sess", 340_000, model="m")
    tools.gauge_check(session_id="sess")
    assert len(sink) == 1
    assert all(item["route"] == "home" for item in sink)
    assert all(item["target"] is None for item in sink)
    assert all(item["fallback"] == "no_home_platform" for item in sink)

    # Without the sink the same alert is posted on the bound session.
    monkeypatch.setattr(tools, "_ALERT_SINK", None)
    posts = []

    class Posting(Ctx):
        def post_message(self, text):
            posts.append(text)
            return {"ok": True}

    tools.bind_context(Posting())
    tools.record_llm_call("other", 340_000, model="m")
    tools.gauge_check(session_id="other")
    assert len(posts) == 1
    # model "m" keeps 0.60 of the 200k window, so 170k tokens is the cap.
    assert any('"other" is 100% full' in text for text in posts)
    assert any("no home channel was found" in text for text in posts)


def test_a_failed_routed_send_falls_back_to_the_session(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "custom:telegram:42")
    monkeypatch.setattr(tools, "_ROUTE_SENDER", lambda target, message: False)
    posts = []

    class Ctx:
        def post_message(self, text):
            posts.append(text)
            return {"ok": True}

    tools.bind_context(Ctx())
    tools.record_llm_call("sess", 340_000, model="m")
    tools.gauge_check(session_id="sess")
    assert len(posts) == 1


def test_routed_alert_keeps_the_once_per_crossing_rule(tmp_path, monkeypatch):
    _route_config(tmp_path, monkeypatch, "custom:telegram:42")
    sends = []
    monkeypatch.setattr(tools, "_ROUTE_SENDER", lambda target, message: sends.append(target) or True)
    tools.record_llm_call("sess", 340_000, model="m")
    tools.gauge_check(session_id="sess")
    tools.gauge_check(session_id="sess")
    assert sends == ["telegram:42"]


def test_hermes_send_command_line_and_missing_binary(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_BIN", "/opt/hermes")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    assert tools._hermes_send("telegram:-100", "hello") is True
    assert seen["argv"] == ["/opt/hermes", "send", "--quiet", "--to", "telegram:-100", "hello"]
    assert seen["kwargs"]["timeout"] > 0
    assert "shell" not in seen["kwargs"]

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1))
    assert tools._hermes_send("telegram", "hello") is False

    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(tools.subprocess, "run", boom)
    assert tools._hermes_send("telegram", "hello") is False

    monkeypatch.delenv("HERMES_BIN")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools._hermes_send("telegram", "hello") is False


def test_chars_per_token_changes_the_estimate_and_the_default_does_not(tmp_path, monkeypatch):
    _config_file(tmp_path, monkeypatch)
    tools.record_llm_call("sess", 280_000, model="m")
    assert tools.gauge_check(session_id="sess")["estimated_tokens"] == 140_000
    tools.config_set({"chars_per_token": 2})
    reading = tools.gauge_check(session_id="sess")
    assert reading["estimated_tokens"] == 280_000
    assert reading["status"] == "FULL"
    assert sum(reading["breakdown"].values()) == reading["estimated_tokens"]


# --- v0.5: recorded_turns carries a timestamp --------------------------------


def _turn_rows(conn):
    return conn.execute(
        "SELECT turn_id, chars, created, absolute FROM recorded_turns ORDER BY rowid"
    ).fetchall()


def test_migration_adds_created_to_an_older_recorded_turns_table(memory_db):
    memory_db.execute(
        """CREATE TABLE recorded_turns (
               session_id TEXT NOT NULL,
               turn_id TEXT NOT NULL,
               chars INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (session_id, turn_id)
           )"""
    )
    memory_db.execute("INSERT INTO recorded_turns VALUES ('old', 't1', 120)")
    memory_db.commit()

    tools._db()
    tools._db()  # a second open must not try the ALTER again

    columns = [row[1] for row in memory_db.execute("PRAGMA table_info(recorded_turns)")]
    assert columns == ["session_id", "turn_id", "chars", "created", "absolute"]
    assert _turn_rows(memory_db) == [("t1", 120, None, 0)]


def test_a_new_database_has_the_columns_from_the_start(memory_db):
    tools._db()
    columns = [row[1] for row in memory_db.execute("PRAGMA table_info(recorded_turns)")]
    assert "created" in columns and "absolute" in columns


def test_each_turn_is_dated_once_and_keeps_its_first_date(memory_db, monkeypatch):
    now = {"t": 5_000.0}
    monkeypatch.setattr(tools.time, "time", lambda: now["t"])

    tools._commit_turn("sess", "t1", 400, model="m")
    now["t"] = 5_090.0
    tools._commit_turn("sess", "t1", 700, model="m")  # same turn, a later frame
    now["t"] = 5_200.0
    tools._commit_turn("sess", "t2", 80, model="m")

    rows = _turn_rows(memory_db)
    assert rows == [("t1", 700, 5_000.0, 0), ("t2", 80, 5_200.0, 0)]


def test_an_absolute_measurement_is_flagged_so_a_curve_can_tell_it_apart(memory_db, monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: 9_000.0)
    tools._commit_turn("sess", "t1", 300, model="m")
    tools._commit_turn("sess", "t2", 900, absolute=True, model="m", direct=True)
    tools._commit_turn("sess", "t1", 450, absolute=True, model="m", direct=True)

    assert _turn_rows(memory_db) == [("t1", 450, 9_000.0, 1), ("t2", 900, 9_000.0, 1)]


def test_the_hook_path_dates_turns(memory_db, monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: 7_777.0)
    tools.handle_post_llm_call(session_id="sess", assistant_response="x" * 40, turn_id="a", model="m")
    tools.handle_stream_end(session_id="sess", turn_id="b", final_text="y" * 20, model="m")
    assert [(row[0], row[2]) for row in _turn_rows(memory_db)] == [("a", 7_777.0), ("b", 7_777.0)]


# --- v0.5: the report joins Hermes' state.db ---------------------------------


NOW = 1_700_000_000.0
DAY = 86_400.0


def _use_state_db(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(tools, "_STATE_DB", make_state_db(tmp_path / "state.db", rows))


def _three_sessions(monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 2 * 3600)
    tools.record_llm_call("sess-live", 380_000, model="claude-opus")
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 3 * DAY)
    tools.record_llm_call("sess-old", 360_000, model="claude-opus")
    tools.record_llm_call("sess-shelved", 100_000, model="opus")
    monkeypatch.setattr(tools.time, "time", lambda: NOW)


def test_report_shows_titles_last_activity_and_state(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-live", "title": "Product launch", "thread_id": "9", "last_activity_at": NOW - 600},
        {"id": "sess-old", "title": "Vendor contract", "last_activity_at": NOW - 3 * DAY},
        {"id": "sess-shelved", "title": "Reading list", "archived": 1, "last_activity_at": NOW - 3 * DAY},
    ])
    _three_sessions(monkeypatch)

    report = tools.gauge_report(format="json")
    by_id = {s["session_id"]: s for s in report["sessions"]}
    assert by_id["sess-live"]["name"] == "Product launch · thread 9"
    assert by_id["sess-live"]["title"] == "Product launch"
    assert by_id["sess-live"]["state"] == "active"
    assert by_id["sess-live"]["last_activity"] == NOW - 600
    assert by_id["sess-old"]["state"] == "frozen"
    assert by_id["sess-shelved"]["state"] == "archived"
    assert report["by_state"] == {"active": 1, "frozen": 1, "archived": 1}
    assert report["names_from_state_db"] is True
    # The id is still there, untouched, for anything that already reads it.
    assert set(by_id) == {"sess-live", "sess-old", "sess-shelved"}


def test_report_text_names_sessions_and_groups_active_first(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-live", "title": "Product launch", "last_activity_at": NOW - 600},
        {"id": "sess-old", "title": "Vendor contract", "last_activity_at": NOW - 3 * DAY},
    ])
    _three_sessions(monkeypatch)

    report = tools.gauge_report(format="text")
    lines = report["text"].splitlines()
    assert lines[0] == report["summary"]
    assert report["summary"] == (
        "3 sessions (1 active, 2 frozen) — weakest link: Product launch 95% FULL"
    )
    rows = [line for line in lines[2:] if "turns" in line]
    assert rows[0].startswith("Product launch  95%  FULL  turns 1  claude-opus  last 2023-11-14 22:03  active")
    assert rows[1].startswith("Vendor contract  90%  FULL")
    assert rows[1].endswith("frozen")
    # No title in Hermes: the id is the name.
    assert rows[2].startswith("sess-shelved  25%  OK  turns 1  opus")


def test_weakest_link_is_taken_among_active_sessions_first(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-live", "title": "Calm chat", "last_activity_at": NOW - 60},
        {"id": "sess-old", "title": "Forgotten", "last_activity_at": NOW - 3 * DAY},
    ])
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 60)
    tools.record_llm_call("sess-live", 120_000, model="opus")
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 3 * DAY)
    tools.record_llm_call("sess-old", 380_000, model="opus")
    monkeypatch.setattr(tools.time, "time", lambda: NOW)

    report = tools.gauge_report(format="json")
    assert report["weakest_link"]["session_id"] == "sess-live"
    assert report["weakest_link"]["name"] == "Calm chat"
    assert "Calm chat 30% OK" in report["summary"]


def test_report_markdown_has_last_activity_and_state_columns(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-live", "title": "Pipes | and\nlines", "last_activity_at": NOW - 600},
    ])
    _three_sessions(monkeypatch)
    body = tools.gauge_report(format="md")["markdown"]
    assert "| Session | Fill | Status | Turns | Model | Last activity | State |" in body
    assert "<u>**Pipes \\| and lines**</u> | 95% | FULL | 1 | claude-opus | 2023-11-14 22:03 | active |" in body
    assert "| sess-shelved | 25% | OK | 1 | opus | 2023-11-11 22:13 | frozen |" in body


def test_report_without_a_state_db_reads_as_before_with_last_seen(monkeypatch):
    _three_sessions(monkeypatch)
    report = tools.gauge_report(format="json")
    assert report["names_from_state_db"] is False
    live = next(s for s in report["sessions"] if s["session_id"] == "sess-live")
    assert live["name"] == "sess-live" and live["title"] is None
    assert live["last_activity"] == live["last_seen"]
    assert live["state"] == "active"
    assert next(s for s in report["sessions"] if s["session_id"] == "sess-old")["state"] == "frozen"


def test_report_ignores_a_corrupt_state_db(monkeypatch, tmp_path):
    broken = tmp_path / "state.db"
    broken.write_bytes(b"this is not sqlite")
    monkeypatch.setattr(tools, "_STATE_DB", broken)
    tools.record_llm_call("sess-live", 4_000, model="m")
    report = tools.gauge_report(format="json")
    assert report["names_from_state_db"] is False
    assert report["sessions"][0]["name"] == "sess-live"


def test_report_write_saves_a_markdown_file_and_returns_a_media_line(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path / "data")
    _use_state_db(monkeypatch, tmp_path, [{"id": "sess-live", "title": "Product launch", "last_activity_at": NOW}])
    _three_sessions(monkeypatch)

    report = tools.gauge_report(format="text", write=True)
    path = Path(report["path"])
    assert report["written"] is True
    assert path.parent == tmp_path / "data" / "reports"
    assert path.name == "gauge-report-20231114-221320.md"
    assert path.read_text(encoding="utf-8").startswith("# Hermes Gauge\n")
    assert "Product launch" in path.read_text(encoding="utf-8")
    assert report["media"] == f"MEDIA:{path}"
    assert report["text"].splitlines()[0] == report["summary"]
    assert report["text"].splitlines()[-1] == f"MEDIA:{path}"
    assert "| Session |" not in report["text"]  # the table is in the file, not in the chat

    as_md = tools.gauge_report(format="md", write="true")
    assert as_md["markdown"].endswith(as_md["media"])

    as_json = tools.gauge_report(format="json", write=True)
    assert as_json["written"] is True and "text" not in as_json and "markdown" not in as_json


def test_report_write_keeps_only_the_latest_files(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(tools, "REPORTS_KEEP", 3)
    tools.record_llm_call("s", 4_000, model="m")
    for second in range(6):
        monkeypatch.setattr(tools.time, "time", lambda second=second: NOW + second)
        tools.gauge_report(write=True)
    kept = sorted(path.name for path in (tmp_path / "reports").glob("*.md"))
    assert kept == [
        "gauge-report-20231114-221323.md",
        "gauge-report-20231114-221324.md",
        "gauge-report-20231114-221325.md",
    ]


def test_report_write_failure_falls_back_to_chat_text(monkeypatch, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be")
    monkeypatch.setattr(tools, "_DATA_DIR", blocker)
    tools.record_llm_call("s", 4_000, model="m")
    report = tools.gauge_report(write=True)
    assert report["written"] is False and report["path"] is None
    assert "Could not write the report" in report["write_error"]
    assert report["text"].startswith(report["summary"]) and "turns" in report["text"]


def test_slash_report_write_returns_the_summary_and_the_media_line(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path)
    tools.record_llm_call("s", 4_000, model="m")
    answer = tools.gauge_slash("report write")
    first, *_middle, last = answer.splitlines()
    assert first.startswith("1 active session")
    assert last.startswith("MEDIA:") and last.endswith(".md")
    assert Path(last[len("MEDIA:"):]).is_file()

    in_md = tools.gauge_slash("report md write")
    assert in_md.splitlines()[-1].startswith("MEDIA:")

    plain = tools.gauge_slash("report")
    assert "MEDIA:" not in plain and "turns 1" in plain

    as_json = json.loads(tools.gauge_slash("report json"))
    assert as_json["written"] is False


def test_report_schema_advertises_write():
    schemas = _load_schemas()
    assert schemas.GAUGE_REPORT["parameters"]["properties"]["write"]["type"] == "boolean"


# --- v0.5: audit — peaks, compactions, composition ---------------------------


def _compact(session_id, reason, at, monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: at)
    tools.handle_compaction(session_id=session_id, reason=reason)


def _session_with_history(monkeypatch):
    """One session: climbs to 95%, is compacted twice, is reset once, climbs again.

    The window is reported once (200_000). A compaction clears the count but
    keeps the window, so the second snapshot still knows it.
    """
    window = dict(context_length=200_000, confirm_window=True)
    breakdown = {"system_prompt": 16_000, "tools": 120_000, "skills": 8_000, "conversation": 236_000}
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 5 * 3600)
    tools.record_llm_call("sess-a", 380_000, model="claude-opus", breakdown=breakdown, **window)
    _compact("sess-a", "post_tool", NOW - 4 * 3600, monkeypatch)
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 3 * 3600)
    tools.record_llm_call("sess-a", 300_000, model="claude-opus")  # window not reported again
    _compact("sess-a", "proactive_prune", NOW - 2 * 3600, monkeypatch)
    _compact("sess-a", "new_session", NOW - 3600, monkeypatch)
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 600)
    tools.record_llm_call(
        "sess-a", 120_000, model="claude-opus", breakdown={"tools": 60_000, "conversation": 60_000}, **window
    )
    monkeypatch.setattr(tools.time, "time", lambda: NOW)


def test_audit_counts_compactions_apart_from_resets_and_remembers_the_peak(monkeypatch):
    _session_with_history(monkeypatch)
    audit = tools.gauge_audit(format="json")

    assert audit["session_count"] == 1
    assert audit["totals"]["compactions"] == 2
    assert audit["totals"]["resets"] == 1
    session = audit["sessions"][0]
    assert session["compactions"]["count"] == 2
    assert session["compactions"]["last_reason"] == "proactive_prune"
    assert session["compactions"]["last_at"] == NOW - 2 * 3600
    assert session["resets"] == 1
    # The live gauge only shows what is left; the peak is what the session reached before.
    assert session["current"]["status"] == "OK"
    assert session["peak"]["status"] == "FULL"
    assert session["peak"]["fill_percent"] > 90
    assert session["peak"]["source"] in ("compaction", "turns")
    # Only the third snapshot, taken while the gauge was still measuring, has no percentage.
    assert [e["inferred"] for e in session["history"]] == [False, False, True]
    assert session["history"][1]["fill_percent"] == 75.0
    assert audit["totals"]["highest_peak"]["session_id"] == "sess-a"
    assert [e["kind"] for e in session["history"]] == ["compaction", "compaction", "reset"]
    assert all(isinstance(e["estimated_tokens"], int) for e in session["history"])


def test_audit_composition_adds_up_and_orders_the_parts(monkeypatch):
    _session_with_history(monkeypatch)
    session = tools.gauge_audit(format="json")["sessions"][0]
    comp = session["composition"]
    assert set(comp) == {"system_prompt", "tools", "skills", "conversation"}
    assert sum(part["tokens"] for part in comp.values()) == session["current"]["estimated_tokens"]
    assert pytest.approx(sum(part["share"] for part in comp.values()), abs=0.3) == 100.0
    assert comp["tools"]["share"] == comp["conversation"]["share"] == 50.0


def test_audit_text_in_english_and_french(monkeypatch):
    _session_with_history(monkeypatch)
    english = tools.gauge_audit(format="text")["text"]
    assert english.splitlines()[0] == "Context audit — 1 session · 2 compactions · 1 reset"
    assert "Highest peak: sess-a" in english
    assert "2 compactions (last 2023-11-14 20:13, proactive_prune)" in english
    assert "Composition: conversation 50% · tools 50%" in english

    french = tools.gauge_audit(format="text", lang="fr-FR")["text"]
    assert french.splitlines()[0] == "Audit du contexte — 1 session · 2 compactages · 1 remise à zéro"
    assert "Plus haut pic" in french and "outils 50%" in french


def test_audit_uses_titles_and_puts_active_sessions_first(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-a", "title": "Product launch", "last_activity_at": NOW - 60},
        {"id": "sess-old", "title": "Vendor contract", "last_activity_at": NOW - 5 * DAY},
    ])
    _session_with_history(monkeypatch)
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 5 * DAY)
    tools.record_llm_call("sess-old", 392_000, model="claude-opus")
    monkeypatch.setattr(tools.time, "time", lambda: NOW)

    audit = tools.gauge_audit(format="json")
    assert [s["name"] for s in audit["sessions"]] == ["Product launch", "Vendor contract"]
    assert [s["state"] for s in audit["sessions"]] == ["active", "frozen"]
    # The frozen one has the higher peak and still tops the overall ranking.
    assert audit["totals"]["highest_peak"]["name"] == "Vendor contract"
    assert audit["totals"]["composition_scope"] == "active"


def test_audit_keeps_sessions_known_only_from_the_compaction_log(monkeypatch):
    tools.record_llm_call("gone", 380_000, model="claude-opus")
    tools.handle_compaction(session_id="gone", reason="post_tool")
    tools._db().execute("DELETE FROM usage WHERE session_id = 'gone'")
    tools._db().commit()
    audit = tools.gauge_audit(format="json")
    session = audit["sessions"][0]
    assert session["session_id"] == "gone"
    assert session["current"] is None and session["composition"] is None
    assert session["compactions"]["count"] == 1
    assert session["peak"]["estimated_tokens"] > 0


def test_audit_for_one_session_and_for_nothing(monkeypatch):
    assert tools.gauge_audit(format="text")["text"] == "No session recorded yet."
    assert tools.gauge_audit(lang="fr")["text"] == "Aucune session enregistrée pour l'instant."
    _session_with_history(monkeypatch)
    tools.record_llm_call("sess-b", 4_000, model="m")
    one = tools.gauge_audit(session_id="sess-b", format="json")
    assert one["session_count"] == 1 and one["sessions"][0]["session_id"] == "sess-b"
    assert one["sessions"][0]["peak"]["source"] in ("now", "turns")
    assert tools.gauge_audit(limit=1, format="json")["shown"] == 1


def test_audit_peak_follows_the_configured_thresholds(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("thresholds:\n  watch: 0.10\n  full: 0.20\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_CONFIG_PATH", config)
    tools.record_llm_call("s", 120_000, model="claude-opus")  # 30%
    assert tools.gauge_audit(format="json")["sessions"][0]["peak"]["status"] == "FULL"


def test_audit_write_saves_a_markdown_table_and_returns_a_media_line(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path)
    _session_with_history(monkeypatch)
    result = tools.gauge_audit(write=True, format="text", lang="fr")
    path = Path(result["path"])
    assert path.name == "gauge-audit-20231114-221320.md"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# Hermes Gauge — Audit du contexte\n")
    assert "| Session | État | Maintenant | Pic | Compactages | RAZ | Conversation | Outils | Système | Skills |" in body
    assert result["text"].splitlines()[-1] == f"MEDIA:{path}"


def test_slash_audit_variants(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path)
    _session_with_history(monkeypatch)
    assert tools.gauge_slash("audit").startswith("Context audit — 1 session")
    assert tools.gauge_slash("audit fr").startswith("Audit du contexte")
    assert tools.gauge_slash("audit md").startswith("# Hermes Gauge — Context audit")
    assert json.loads(tools.gauge_slash("audit json"))["totals"]["compactions"] == 2
    assert tools.gauge_slash("audit write").splitlines()[-1].startswith("MEDIA:")
    assert tools.gauge_slash("audit sess-a").startswith("Context audit — 1 session")
    assert tools.gauge_slash("AUDIT").startswith("Context audit")


# --- v0.5: the incident report ----------------------------------------------------


@pytest.fixture
def utc_stamps(monkeypatch):
    """Incident pages write local time; tests read it in UTC."""
    import time as _time
    monkeypatch.setattr(tools._sibling("incidents"), "_localtime", _time.gmtime)


def _log_at(session_id, kind, detail, at, monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: at)
    tools._log_hook(session_id, kind, detail)
    monkeypatch.setattr(tools.time, "time", lambda: NOW)


def _incident_scenario(monkeypatch, tmp_path):
    _use_state_db(monkeypatch, tmp_path, [
        {"id": "sess-a", "title": "Product launch", "thread_id": "7", "last_activity_at": NOW - 600},
    ])
    _session_with_history(monkeypatch)
    _log_at("sess-a", "provider_error", "Connection error.", NOW - 3600, monkeypatch)
    _log_at("sess-a", "missed", "post_llm_call did not run after an incomplete stream", NOW - 3600, monkeypatch)
    _log_at("", "missed", "gauge_check flushed a stream the end hook never closed", NOW - 1800, monkeypatch)


def test_incident_report_reads_both_logs_and_names_the_sessions(monkeypatch, tmp_path, utc_stamps):
    _incident_scenario(monkeypatch, tmp_path)
    report = tools.gauge_incidents()

    assert report["lang"] == "en" and report["format"] == "json"
    assert report["kpis"]["compactions"] == 2        # the /new is a reset, not a compaction
    assert report["kpis"]["resets"] == 1
    assert report["kpis"]["network_errors"] == 1
    assert report["kpis"]["missed_turns"] == 2
    assert report["names_from_state_db"] is True
    assert [c["name"] for c in report["cards"]] == ["Product launch · thread 7"]
    kinds = [e["kind"] for e in report["timeline"]]
    assert kinds.count("compaction") == 2 and kinds.count("reset") == 1
    assert "html" not in report and report["written"] is False
    assert "Hermes Gauge report" in report["llm"]


def test_incident_report_html_and_file(monkeypatch, tmp_path, utc_stamps):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path / "data")
    _incident_scenario(monkeypatch, tmp_path)

    shown = tools.gauge_incidents(format="html", lang="en")
    assert shown["html"].startswith("<!DOCTYPE html>") and shown["lang"] == "en"
    assert "Were there incidents?" in shown["html"] and shown["written"] is False

    saved = tools.gauge_incidents(write=True)
    path = Path(saved["path"])
    assert path.parent == tmp_path / "data" / "reports"
    assert path.name == "gauge-incidents-20231114-221320.html"
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert saved["media"] == f"MEDIA:{path}" and "html" not in saved


def test_incident_report_days_and_bad_days(monkeypatch, tmp_path):
    _incident_scenario(monkeypatch, tmp_path)
    recent = tools.gauge_incidents(days=1)
    assert recent["kpis"]["compactions"] == 2 and recent["period"]["days"] == 1.0
    assert tools.gauge_incidents(days="0")["period"]["days"] is None
    assert tools.gauge_incidents(days="soon")["period"]["days"] is None
    assert tools.gauge_incidents(days=-3)["period"]["days"] is None
    narrow = tools.gauge_incidents(days=1 / 24)  # one hour: the compactions are older than that
    assert narrow["kpis"]["compactions"] == 0


def test_incident_report_session_known_only_from_the_hook_log(monkeypatch, tmp_path, utc_stamps):
    _log_at("ghost", "provider_error", "Connection error.", NOW - 100, monkeypatch)
    report = tools.gauge_incidents()
    assert report["kpis"]["network_errors"] == 1
    assert report["cards"][0]["session_id"] == "ghost" and report["cards"][0]["name"] == "ghost"
    assert report["cards"][0]["status"] == "UNKNOWN"


def test_incident_report_sparkline_uses_the_dated_turns(monkeypatch, tmp_path, utc_stamps):
    window = dict(context_length=200_000, confirm_window=True)
    for index in range(8):
        monkeypatch.setattr(tools.time, "time", lambda index=index: NOW - 8 * 3600 + index * 600)
        tools._commit_turn("sess-s", f"t{index}", 40_000, model="claude-opus", **window)
    _compact("sess-s", "post_tool", NOW - 2 * 3600, monkeypatch)
    monkeypatch.setattr(tools.time, "time", lambda: NOW)

    card = tools.gauge_incidents()["cards"][0]
    points = card["spark"]["points"]
    assert len(points) == 8
    assert [p[1] for p in points][:3] == [10.0, 20.0, 30.0]       # 40k chars → 20k tokens each
    assert card["spark"]["events"] == [] or all(e["kind"] == "compaction" for e in card["spark"]["events"])
    assert "<polyline" in tools.gauge_incidents(format="html")["html"]


def test_slash_incidents_writes_the_page_and_answers_with_the_verdict(monkeypatch, tmp_path, utc_stamps):
    monkeypatch.setattr(tools, "_DATA_DIR", tmp_path / "data")
    _incident_scenario(monkeypatch, tmp_path)
    answer = tools.gauge_slash("incidents")
    lines = answer.splitlines()
    assert lines[0].startswith("Yes")
    assert lines[-1].startswith("MEDIA:") and lines[-1].endswith(".html")
    assert Path(lines[-1][len("MEDIA:"):]).is_file()

    english = tools.gauge_slash("incidents en 1")
    assert english.splitlines()[0].startswith("Yes")
    assert tools.gauge_slash("incidents jours 2").splitlines()[-1].startswith("MEDIA:")
    assert tools.gauge_slash("incident").splitlines()[-1].startswith("MEDIA:")


def test_slash_incidents_reports_a_write_failure(monkeypatch, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be")
    monkeypatch.setattr(tools, "_DATA_DIR", blocker)
    answer = tools.gauge_slash("incidents")
    assert "Could not write the incident report" in answer and "MEDIA:" not in answer


def test_audit_ranks_peaks_by_fill_not_by_raw_tokens(monkeypatch):
    # A small-window model at 90% matters more than a large-window one at 40%.
    tools.record_llm_call("small", 180_000, model="opus", context_length=100_000, confirm_window=True)  # 90k / 100k
    tools.record_llm_call("large", 320_000, model="opus", context_length=400_000, confirm_window=True)  # 160k / 400k
    audit = tools.gauge_audit(format="json")
    assert audit["totals"]["highest_peak"]["session_id"] == "small"
    assert [s["session_id"] for s in audit["sessions"]] == ["small", "large"]
    assert audit["sessions"][0]["window_tokens"] == 100_000


# --- v0.6: a WATCH snapshot is not a compaction ------------------------------------


def _cross_into_watch(session_id="sess-w"):
    """One turn that takes a 200k-window session to 70%, through the hook path."""
    tools.record_llm_call(
        session_id, 280_000, model="claude-opus", context_length=200_000, confirm_window=True
    )
    tools._after_turn(session_id)


def test_a_single_watch_crossing_is_not_a_compaction(monkeypatch):
    _cross_into_watch()
    # The in-flight copy is still written: it is what keeps the peak.
    assert [e["reason"] for e in tools.compaction_events("sess-w")] == ["watch"]

    audit = tools.gauge_audit(format="json", lang="en")
    assert audit["totals"]["compactions"] == 0
    assert audit["totals"]["resets"] == 0
    session = audit["sessions"][0]
    assert session["compactions"] == {"count": 0, "last_at": None, "last_reason": None}
    assert session["resets"] == 0 and session["history"] == []
    # ... and the peak still knows the session reached 70%.
    assert session["peak"]["fill_percent"] == 70.0 and session["peak"]["status"] == "WATCH"
    text = tools.gauge_audit(format="text", lang="en")["text"]
    assert "0 compactions" in text and "(last" not in text


def test_a_watch_crossing_does_not_reach_the_incident_report(monkeypatch):
    _cross_into_watch()
    report = tools.gauge_incidents(lang="en")
    assert report["kpis"]["compactions"] == 0 and report["kpis"]["resets"] == 0
    assert [e["kind"] for e in report["timeline"]] == []
    card = report["cards"][0]  # it is a card because the session is at WATCH now
    assert card["compactions"] == 0 and card["last_compaction_at"] is None
    said = " ".join(card["happened"] + card["implies"] + card["fix"]).lower()
    assert "summariz" not in said and "compaction" not in said
    assert "summariz" not in report["verdict"]["message"].lower()
    assert "compaction" not in report["verdict"]["message"].lower()


def test_a_watch_snapshot_does_not_restart_the_dated_series(monkeypatch):
    window = dict(context_length=200_000, confirm_window=True)
    for index in range(4):
        monkeypatch.setattr(tools.time, "time", lambda index=index: NOW - 3600 + index * 600)
        tools._commit_turn("sess-w", f"t{index}", 100_000, model="claude-opus", **window)
        tools._after_turn("sess-w")  # the third turn crosses into WATCH and writes the snapshot
    assert [e["reason"] for e in tools.compaction_events("sess-w")] == ["watch"]
    rows, _known = tools._audit_rows(tools._db(), ["sess-w"], NOW, 4)
    assert [tokens for _at, tokens in rows[0]["series"]] == [50_000, 100_000, 150_000, 200_000]


def test_a_real_compaction_next_to_a_snapshot_is_counted_once(monkeypatch):
    _cross_into_watch()
    tools.handle_compaction(session_id="sess-w", reason="post_tool")
    session = tools.gauge_audit(format="json")["sessions"][0]
    assert session["compactions"]["count"] == 1
    assert [e["kind"] for e in session["history"]] == ["compaction"]


# --- installed: degradation, policy, gauge_action, setup audit --------


def _watch_text(who, pct="70"):
    return (
        f'"{who}" is {pct}% full: room for a bit more, not for anything big. '
        "Wrap up this topic, or ask for a summary before starting a new one. "
        "Hermes Gauge only watches; it never changes your chat."
    )


def _full_text(who, pct="85"):
    return (
        f'"{who}" is {pct}% full. Quality drops from here. '
        "Ask for a summary, then send /new and paste it. "
        "Hermes Gauge only watches; it never changes your chat."
    )


def _status_pushes(sent):
    """WATCH/FULL alert pushes. A dull recommendation due on the same turn rides in the same message."""
    return [item for item in sent if "only watches" in item.get("message", "")]


def test_two_hooks_on_one_turn_claim_the_crossing_once(monkeypatch):
    """post_api_request and post_llm_call (or two processes) read the same stale row."""
    sent = _capture_alerts(monkeypatch)
    tools.record_llm_call("race", 340_000, model="opus")
    conn = tools._db()
    fields_a, usage_a = tools._reading(conn, "race")
    fields_b, usage_b = tools._reading(conn, "race")
    assert usage_a["notified_status"] == usage_b["notified_status"]
    for fields in (fields_a, fields_b):
        fields["policy_verdict"] = tools.gauge_math.policy_verdict(
            fields.get("model"), fields.get("fill_percent"), tools.load_config()
        )
    # Both hooks hold the same "previous" status; only one may own the crossing.
    assert tools._notify(conn, "race", "FULL", usage_a["notified_status"])
    assert tools._notify(conn, "race", "FULL", usage_b["notified_status"]) is None
    # The cooldown stamp is claimed the same way: the second claim finds it taken.
    assert tools._claim_alert(conn, "race", fields_a) is not None
    assert tools._claim_alert(conn, "race", fields_b) is None
    assert tools._claim_policy(conn, "race", fields_a) is not None
    assert tools._claim_policy(conn, "race", fields_b) is None
    assert sent == []


def test_a_turn_that_crosses_watch_and_the_dull_line_sends_one_bubble(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    tools.record_llm_call("flash", 280_000, model="gemini-2.5-flash",
                          context_length=200_000, confirm_window=True)
    reading = tools.gauge_check(session_id="flash")
    assert reading["status"] in ("WATCH", "FULL")
    assert reading["policy_verdict"]["dull_reached"] is True
    assert len(sent) == 1
    message = sent[0]["message"]
    assert "only watches" in message and "seuil débile 55 %" in message
    assert message.count("Hermes Gauge") == 2


def test_a_failed_send_releases_the_claim_so_the_next_turn_retries(monkeypatch):
    attempts = []

    def sink(payload):
        attempts.append(payload)

    monkeypatch.setattr(tools, "_ALERT_SINK", sink)
    monkeypatch.setattr(tools, "emit_hermes_notification", lambda *a, **k: {"sent": False, "channel": "none"})
    tools.record_llm_call("retry", 340_000, model="opus")
    tools.gauge_check(session_id="retry")
    row = tools._db().execute(
        "SELECT full_alerted_at, dull_latched FROM usage WHERE session_id = 'retry'"
    ).fetchone()
    assert row[0] is None and row[1] == 0


def test_alert_wording_names_the_conversation_and_does_not_name_a_tool():
    fields = {"fill_percent": 85.0, "status": "FULL"}
    full = tools._alert_message("release-review", fields, "telegram")
    assert full == _full_text("release-review")
    assert "gauge_check" not in full and "gauge_report" not in full
    watch = {"fill_percent": 70.0, "status": "WATCH"}
    assert tools._alert_message("s", watch, "cli") == _watch_text("s")
    assert tools._alert_message("s", fields, "desktop") == _full_text("s")


def test_telegram_route_asks_for_a_numbered_reply_even_from_a_cli_session(monkeypatch):
    sent = _capture_alerts(monkeypatch)

    class Ctx:
        def session_surface(self):
            return "cli"

        def post_message(self, text):
            return {"ok": True}

    tools.bind_context(Ctx())
    monkeypatch.setattr(tools, "load_config", lambda: _config(gauge={
        "alerts": {
            "enabled": True,
            "on": ["WATCH", "FULL"],
            "cooldown_minutes": 30,
            "route": "custom:telegram:4242",
        },
    }))
    tools.record_llm_call("release-review", 340_000, model="opus")
    tools.gauge_check(session_id="release-review")
    push = _status_pushes(sent)[0]
    assert push["target"] == "telegram:4242"
    assert push["message"].startswith(_full_text("release-review"))
    assert "gauge_" not in push["message"]


def test_alerts_do_not_open_a_second_telegram_poller():
    source = Path(tools.__file__).read_text(encoding="utf-8")
    assert "getUpdates" not in source
    assert "inline_keyboard" not in source
    assert "callback_data" not in source
    assert "compact_session" not in source


def test_gauge_action_hands_commands_to_hermes_and_does_not_compact():
    handed = []

    def runner(session_id, commands):
        handed.append((session_id, list(commands)))
        return True

    tools._HERMES_COMMAND = runner
    tools.record_llm_call("release-review", 340_000, model="opus")
    both = tools._gauge_action("compact+new", "release-review")
    assert both["ok"] is True
    assert both["action"] == "compact_new"
    assert both["commands"] == ["/compact", "/new"]
    assert both["executed"] is True
    assert both["status"] == "FULL"
    assert handed == [("release-review", ["/compact", "/new"])]
    reasons = [event["reason"] for event in tools.compaction_events("release-review")]
    assert "compact" not in reasons
    assert "new_session" not in reasons

    compact = tools._gauge_action({"action": "2", "session_id": "release-review"})
    assert compact["action"] == "compact"
    assert compact["commands"] == ["/compact"]
    fresh = tools._gauge_action(action="/new", session_id="release-review")
    assert fresh["commands"] == ["/new"]
    raw = tools.gauge_action("1", "release-review")
    parsed = json.loads(raw)
    assert parsed["action"] == "compact_new"
    assert parsed["commands"] == ["/compact", "/new"]


def test_dismiss_quiets_the_status_until_it_changes(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    clock = {"t": 2_000.0}
    monkeypatch.setattr(tools.time, "time", lambda: clock["t"])
    tools.record_llm_call("release-review", 280_000, model="opus")
    tools.gauge_check(session_id="release-review")
    assert len(_status_pushes(sent)) == 1
    dismissed = tools._gauge_action(action="rien", session_id="release-review")
    assert dismissed["action"] == "dismiss"
    assert dismissed["executed"] is True
    assert dismissed["commands"] == []
    assert "No /compact" in dismissed["detail"]

    clock["t"] += 31 * 60
    tools.record_llm_call("release-review", 10_000, model="opus", replace=True)
    tools.gauge_check(session_id="release-review")
    tools.record_llm_call("release-review", 280_000, model="opus", replace=True)
    tools.gauge_check(session_id="release-review")
    assert len(_status_pushes(sent)) == 1

    tools.record_llm_call("release-review", 340_000, model="opus", replace=True)
    tools.gauge_check(session_id="release-review")
    assert len(_status_pushes(sent)) == 2
    assert "85% full" in _status_pushes(sent)[-1]["message"]


def test_gauge_action_rejects_unknown_input_and_skips_handoff_without_hermes():
    tools.record_llm_call("release-review", 280_000, model="opus")
    missing = tools._gauge_action("compact", session_id="nobody")
    assert missing["ok"] is False
    assert missing["error"] == "unknown session"
    bad = tools._gauge_action("explode", "release-review")
    assert bad["ok"] is False
    quiet = tools._gauge_action("compact", "release-review")
    assert quiet["ok"] is True
    assert quiet["executed"] is False
    assert quiet["commands"] == ["/compact"]
    assert "tapez" not in quiet["detail"]
    assert "/compact" in quiet["detail"]
    reasons = [event["reason"] for event in tools.compaction_events("release-review")]
    assert reasons == [] or "compact" not in reasons


def test_tools_accept_positional_and_keyword_dispatch():
    tools.record_llm_call("sess", 200_000, model="opus")

    positional = tools.gauge_check("sess")
    bundled = tools.gauge_check({"session_id": "sess", "model": "opus"})
    keyword = tools.gauge_check(session_id="sess", model="opus")
    assert positional["session_id"] == bundled["session_id"] == keyword["session_id"] == "sess"
    assert positional["fill_percent"] == bundled["fill_percent"] == keyword["fill_percent"] == 50.0
    assert positional["status"] == "OK"

    report = tools.gauge_report({"limit": 1, "format": "json"})
    assert report["format"] == "json"
    assert report["session_count"] == 1
    assert "text" not in report
    markdown = tools.gauge_report(limit=1, format="md")
    assert markdown["format"] == "md"
    assert "markdown" in markdown

    tools.record_llm_call("sized", 3_600, model="opus")
    updated = tools.gauge_set_window({"model": "opus", "window_tokens": 3_000})
    assert updated == {"model": "opus", "window_tokens": 3000, "reset": False}
    assert tools.gauge_check(session_id="sized")["status"] == "WATCH"
    # Second positional is the window, same as the historical call shape.
    narrowed = tools.gauge_set_window("opus", 2_000)
    assert narrowed["window_tokens"] == 2_000
    assert tools.gauge_check("sized")["status"] == "FULL"
    reset = tools.gauge_set_window("opus")
    assert reset == {"model": "opus", "window_tokens": 200_000, "reset": True}


@pytest.mark.parametrize("model,factor", [
    ("gemini-2.5-flash", 0.25),
    ("gpt-5-nano", 0.25),
    ("llama-lite", 0.25),
    ("gpt-4o-mini", 0.40),
    ("glm-5.2", 0.40),
    ("claude-haiku-4-5", 0.50),
    ("model-air", 0.25),
    ("claude-opus-4-6", 1.0),
    ("gemini-3-flash", 0.25),
    ("gemini-2.5-pro", 1.0),
    ("hermes-3", 0.60),
    ("gpt-4.1", 0.60),
    ("small", 0.60),
    ("model-fast", 0.60),
    ("", 0.60),
])
def test_degradation_factor_uses_the_family_table(model, factor):
    assert tools.gauge_math.degradation_factor(model) == factor


def test_degradation_factor_prefers_exact_ids_over_substrings():
    config = {
        "effective_factors": {"glm": 0.3, "flash": 0.8},
        "effective_models": {"glm-4.5": 0.7, "gemini-2.5-flash": 0.33},
    }
    assert tools.gauge_math.degradation_factor("glm-4.6", config) == 0.3
    assert tools.gauge_math.degradation_factor("glm-4.5", config) == 0.7
    assert tools.gauge_math.degradation_factor("gemini-2.5-flash", config) == 0.33
    assert tools.gauge_math.degradation_factor(
        "gemini-2.5-flash", {"effective_factors": {"flash": 0.8}}
    ) == 0.8
    assert tools.gauge_math.degradation_factor("x", {"effective_models": {"x": 5}}) == 1.0
    assert tools.gauge_math.degradation_factor("x", {"effective_models": {"x": 0.01}}) == 0.05

    shipped = tools.load_config()
    assert shipped["effective_models"] == {}
    assert shipped["effective_factors"] == {}


def test_compose_scores_watch_and_full_on_the_effective_window():
    # 30_000 / 50_000 = 60% WATCH. The same count is 15% of the nominal window.
    watched = tools.gauge_math.compose(
        30_000, 200_000, 0, "detected", True, 0.60, 0.85,
        approximate=False, degrade=0.25,
    )
    assert watched["nominal_window"] == 200_000
    assert watched["effective_window"] == 50_000
    assert watched["window_tokens"] == 50_000
    assert watched["degrade_factor"] == 0.25
    assert watched["fill_percent"] == 60.0
    assert watched["status"] == "WATCH"
    assert watched["remaining_tokens"] == 20_000
    assert watched["fill_display"] == "60.0%"

    # 45_000 / 50_000 = 90% FULL. Nominally that would still be OK.
    full = tools.gauge_math.compose(
        45_000, 200_000, 0, "detected", True, 0.60, 0.85,
        approximate=True, degrade=0.25,
    )
    assert full["status"] == "FULL"
    assert full["fill_percent"] == 90.0
    assert full["fill_display"] == "≈90.0%"
    assert full["remaining_tokens"] == 5_000
    assert full["nominal_window"] == 200_000


def test_include_factors_lists_the_tier_and_a_stored_override_wins():
    tools.record_llm_call(
        "s", 120_000, model="gemini-2.5-flash",
        context_length=200_000, confirm_window=True,
    )
    plain = tools.gauge_check(session_id="s")
    assert "factors" not in plain
    # 60_000 estimated tokens / 50_000 effective window → FULL.
    assert plain["degrade_factor"] == 0.25
    assert plain["effective_window"] == 50_000
    assert plain["nominal_window"] == 200_000
    assert plain["status"] == "FULL"

    listed = tools.gauge_check({"session_id": "s", "include_factors": True})
    factors = listed["factors"]
    assert factors["source"] == "family"
    assert factors["matched"] == "flash"
    assert factors["factor"] == 0.25
    flash = next(tier for tier in factors["tiers"] if tier["factor"] == 0.25)
    assert flash["matched"] == ["flash"]

    stored = tools.gauge_set_window(
        {"model": "gemini-2.5-flash", "session_id": "s", "degrade": 0.5}
    )
    assert stored["degrade_target"] == "s"
    assert stored["degrade_factor"] == 0.5
    assert stored["reset"] is False
    assert "window_tokens" not in stored
    tuned = tools.gauge_check(session_id="s", include_factors=True)
    assert tuned["window_source"] == "detected"
    assert tuned["effective_window"] == 100_000
    assert tuned["degrade_factor"] == 0.5
    assert tuned["factors"]["source"] == "stored"
    # 60_000 / 100_000 = 60% of the effective window (30% of nominal).
    assert tuned["status"] == "WATCH"
    assert tuned["fill_percent"] == 60.0

    assert tools.set_degradation("s", "reset")["reset"] is True
    assert tools.gauge_check(session_id="s")["degrade_factor"] == 0.25
    assert "error" in tools.set_degradation("s", "nope")


def test_model_row_applies_until_a_session_row_overrides_it(monkeypatch):
    monkeypatch.setattr(tools, "load_config", lambda: _config(
        effective_factors={"custom": 0.5},
        effective_models={"custom-model": 0.8},
    ))
    tools.record_llm_call(
        "s", 0, model="custom-model", context_length=100_000, confirm_window=True,
    )
    assert tools.gauge_check(session_id="s")["degrade_factor"] == 0.8
    tools.gauge_set_window(model="custom-model", degrade=0.3)
    assert tools.gauge_check(session_id="s")["effective_window"] == 30_000
    tools.gauge_set_window(model="custom-model", session_id="s", degrade=0.6)
    reading = tools.gauge_check(session_id="s")
    assert reading["degrade_factor"] == 0.6
    assert reading["effective_window"] == 60_000
    assert reading["window_source"] == "detected"


def test_gauge_audit_on_a_temp_database_flags_setup_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_HERMES_HOME", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 100\n  user_char_limit: 80\n",
        encoding="utf-8",
    )
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("x" * 95, encoding="utf-8")
    (memories / "MEMORY.md.bak-old").write_text("stale backup", encoding="utf-8")
    note = "alpha bravo charlie delta echo foxtrot golf hotel india juliet extra words"
    profile = tmp_path / "profiles" / "dev" / "memories"
    profile.mkdir(parents=True)
    (profile / "MEMORY.md").write_text(note + "\n§\n" + note, encoding="utf-8")

    tools.record_llm_call(
        "s",
        400_000,
        model="gemini-2.5-flash",
        direct=True,
        context_length=200_000,
        confirm_window=True,
        breakdown={
            "system_prompt": 200_000,
            "tools": 0,
            "skills": 0,
            "conversation": 200_000,
        },
    )
    audit = tools.gauge_audit("s")
    assert audit["session_id"] == "s"
    assert audit["thresholds"] == {"watch": 0.60, "full": 0.85}
    assert audit["config_path"].endswith("config.yaml")
    assert audit["effective_factors"] == {}
    assert audit["effective_models"] == {}
    assert audit["degrade_factor"] == 0.25
    assert audit["effective_window"] == 50_000
    assert audit["active_factors"]["source"] == "family"
    assert audit["active_factors"]["matched"] == "flash"
    assert audit["overhead_tokens"] == 50_000
    assert audit["conversation_tokens"] == 50_000
    assert audit["watch_tokens"] == 30_000
    assert audit["full_tokens"] == 42_500
    assert audit["fill_effective_pct"] == 200.0
    assert audit["fill_nominal_pct"] == 50.0

    areas = {finding["area"] for finding in audit["findings"]}
    assert "sweet-spot" in areas
    assert "thresholds" in areas
    watch = next(f for f in audit["findings"] if f["area"] == "thresholds" and "WATCH" in f["detail"])
    full = next(f for f in audit["findings"] if f["area"] == "thresholds" and "FULL" in f["detail"])
    assert watch["severity"] == "alert"
    assert "inatteignable" in watch["detail"]
    assert full["severity"] == "alert"

    memory = audit["memory"]
    assert memory["files"] == 2
    assert memory["entries"] == 3
    assert memory["alert"] is True
    assert memory["max_fill_ratio"] >= 0.95
    default = next(store for store in memory["stores"] if store["name"] == "default")
    assert default["memory_chars"] == 95
    assert default["memory_budget"] == 100
    assert default["memory_fill"] == 0.95
    assert default["bytes"] == 95
    dev = next(store for store in memory["stores"] if store["name"] == "dev")
    assert dev["memory_budget"] == 100
    assert dev["entries"] == 2
    assert memory["duplicate_count"] == 1
    assert memory["duplicates"][0]["similarity"] == 1.0
    assert memory["duplicates"][0]["left"].startswith("dev/MEMORY.md#")
    assert memory["top_entries"]
    assert memory["top_entries"][0]["chars"] >= memory["top_entries"][-1]["chars"]
    assert len(memory["top_entries"]) <= 5
    assert any(
        finding["severity"] == "alert" and "default/MEMORY.md" in finding["detail"]
        for finding in audit["findings"]
    )
    assert any(
        finding["severity"] == "warn" and "quasi identiques" in finding["detail"]
        for finding in audit["findings"]
    )
    assert audit["status"] == "ALERT"


def test_memory_duplicates_need_more_than_80_percent_shared_tokens(tmp_path):
    left = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    right = "alpha bravo charlie delta echo foxtrot golf hotel kilo lima"
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text(left + "\n§\n" + right, encoding="utf-8")
    metrics, findings = tools.audit_memory_bank(tmp_path)
    assert metrics["duplicate_count"] == 0
    assert metrics["max_fill_ratio"] is None
    assert not any("quasi identiques" in finding["detail"] for finding in findings)

    (memories / "MEMORY.md").write_text("unrelated heading", encoding="utf-8")
    (memories / "USER.md").write_text(left + "\n§\n" + left, encoding="utf-8")
    metrics, findings = tools.audit_memory_bank(tmp_path)
    assert metrics["duplicate_count"] == 1
    assert metrics["duplicates"][0]["similarity"] == 1.0
    assert any(finding["severity"] == "warn" for finding in findings)


def test_gauge_audit_names_the_fix_when_fallback_lingers(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_HERMES_HOME", tmp_path)
    turns = tools.FALLBACK_TURNS_BEFORE_WARN + 1
    for _ in range(turns):
        tools.record_llm_call("old", 40, model="hermes-3")
    audit = tools.gauge_audit({"session_id": "old"})
    assert audit["reading"]["window_source"] == "fallback"
    assert audit["reading"]["turns"] == turns
    assert audit["fallback_turns"] == turns
    assert audit["fallback_turn_limit"] == tools.FALLBACK_TURNS_BEFORE_WARN
    detail = next(f["detail"] for f in audit["findings"] if "gauge_set_window" in f["detail"])
    assert "hermes-3" in detail
    assert "old" in detail
    assert "config.yaml" in detail
    assert audit["memory"]["files"] == 0
    assert audit["memory"]["alert"] is False
    assert not any(f["area"] == "thresholds" for f in audit["findings"])

    tools.record_llm_call("fresh", 40, model="hermes-3")
    fresh = tools.gauge_audit(session_id="fresh")
    assert fresh["reading"]["turns"] == 1
    assert any("fallback" in finding["detail"] for finding in fresh["findings"])
    assert not any("gauge_set_window" in finding["detail"] for finding in fresh["findings"])


def _policy_pushes(sent):
    # Dull recommendations name the seuil. v0.6 status alerts also say
    # "effectif", so that word no longer distinguishes the two pushes.
    return [
        item for item in sent
        if "seuil débile" in item["message"]
    ]


def test_shipped_policy_observes_and_does_not_execute():
    config = tools.load_config()
    policy = tools.gauge_math.resolve_policy(config)
    assert policy["mode"] == "audit"
    assert policy["dull_at"]["default"] == 0.70
    assert policy["dull_at"]["flash"] == 0.55
    assert policy["dull_at"]["mini"] == 0.60
    assert policy["on_dull"] == ["compact", "new_session"]
    assert policy["cooldown_minutes"] == 60
    text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
    parsed = tools.gauge_math.parse_config(text)
    assert parsed["policy"]["preset"] == "standard"
    assert "mode" not in parsed["policy"]
    standard = parsed["policy"]["presets"]["standard"]
    assert standard["mode"] == "audit"
    assert standard["dull_at"]["mini"] == 0.60
    assert standard["on_dull"] == ["compact", "new_session"]
    assert parsed["policy"]["presets"]["prudent"]["dull_at"]["default"] == 0.55
    assert "never compacts" in text
    skill = (
        Path(__file__).resolve().parents[1] / "skills" / "gauge-viewer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "never compacts" in skill
    assert "gauge_policy" in skill
    assert "preset: prudent" in skill
    source = Path(tools.__file__).read_text(encoding="utf-8")
    assert "compact_session" not in source


def test_policy_merges_overrides_onto_defaults_and_clamps_dull_at():
    resolved = tools.gauge_math.resolve_policy({"policy": {
        "mode": "act",
        "cooldown_minutes": 5,
    }})
    assert resolved["mode"] == "act"
    assert resolved["cooldown_minutes"] == 5.0
    assert resolved["dull_at"]["default"] == 0.70
    assert resolved["dull_at"]["flash"] == 0.55
    assert resolved["on_dull"] == ["compact", "new_session"]

    clamped = tools.gauge_math.resolve_policy({"policy": {
        "mode": "yolo",
        "dull_at": {"default": 0.01, "flash": 5, "haiku": "nope"},
        "cooldown_minutes": -4,
    }})
    assert clamped["mode"] == "audit"
    assert clamped["dull_at"]["default"] == 0.1
    assert clamped["dull_at"]["flash"] == 1.0
    assert clamped["dull_at"]["mini"] == 0.60
    assert "haiku" not in clamped["dull_at"]
    assert clamped["cooldown_minutes"] == 0.0
    assert tools.gauge_math.clamp_dull("nope") is None


def test_policy_verdict_names_the_model_tier_and_audit_never_acts(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    clock = {"t": 5_000.0}
    monkeypatch.setattr(tools.time, "time", lambda: clock["t"])

    tools.record_llm_call(
        "flash", 60_000, model="gemini-2.5-flash",
        context_length=200_000, confirm_window=True,
    )
    reading = tools.gauge_check(session_id="flash")
    verdict = reading["policy_verdict"]
    assert set(verdict) == {
        "mode", "dull_at_applied", "fill_effective_pct", "dull_reached", "recommendation",
    }
    assert verdict["mode"] == "audit"
    assert verdict["dull_at_applied"] == 0.55
    assert verdict["dull_reached"] is True
    assert verdict["fill_effective_pct"] == 60.0
    assert verdict["recommendation"] == (
        "60 % effectif, seuil débile 55 % pour ce modèle flash : "
        "recommandé = compacter, puis nouvelle session."
    )
    pushes = _policy_pushes(sent)
    assert len(pushes) == 1
    assert "action recommandée" not in pushes[0]["message"]
    assert pushes[0]["message"].endswith(verdict["recommendation"])
    reasons = [event["reason"] for event in tools.compaction_events("flash")]
    assert "compact" not in reasons
    assert "new_session" not in reasons

    tools.gauge_check(session_id="flash")
    assert len(_policy_pushes(sent)) == 1

    # Under the threshold the latch opens. Inside the cooldown a new
    # crossing is silent. After the cooldown it pushes once, still as audit.
    tools.record_llm_call(
        "flash", 20_000, model="gemini-2.5-flash", replace=True,
        context_length=200_000, confirm_window=True,
    )
    under = tools.gauge_check(session_id="flash")
    assert under["policy_verdict"]["dull_reached"] is False
    assert "pas encore atteint" in under["policy_verdict"]["recommendation"]
    clock["t"] += 10 * 60
    tools.record_llm_call(
        "flash", 60_000, model="gemini-2.5-flash", replace=True,
        context_length=200_000, confirm_window=True,
    )
    tools.gauge_check(session_id="flash")
    assert len(_policy_pushes(sent)) == 1

    tools.record_llm_call(
        "flash", 20_000, model="gemini-2.5-flash", replace=True,
        context_length=200_000, confirm_window=True,
    )
    tools.gauge_check(session_id="flash")
    clock["t"] += 61 * 60
    tools.record_llm_call(
        "flash", 60_000, model="gemini-2.5-flash", replace=True,
        context_length=200_000, confirm_window=True,
    )
    tools.gauge_check(session_id="flash")
    assert len(_policy_pushes(sent)) == 2
    assert all("action recommandée" not in item["message"] for item in _policy_pushes(sent))


def test_act_mode_pushes_a_recommendation_and_does_not_execute_it(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(tools, "load_config", lambda: _config(policy={
        "mode": "act",
        "dull_at": {"default": 0.70, "flash": 0.55, "mini": 0.60},
        "on_dull": ["compact", "new_session"],
        "cooldown_minutes": 60,
    }))
    tools.record_llm_call("sess", 280_000, model="opus")
    reading = tools.gauge_check(session_id="sess")
    assert reading["policy_verdict"]["mode"] == "act"
    assert reading["policy_verdict"]["dull_reached"] is True
    pushes = _policy_pushes(sent)
    assert len(pushes) == 1
    assert "action recommandée" in pushes[0]["message"]
    assert "compacte / nouvelle session recommandée" in pushes[0]["message"]
    reasons = [event["reason"] for event in tools.compaction_events("sess")]
    assert "compact" not in reasons
    assert "new_session" not in reasons
    assert "policy" not in reasons


def test_unreachable_dull_threshold_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_HERMES_HOME", tmp_path)
    monkeypatch.setattr(tools, "load_config", lambda: _config(policy={
        "mode": "audit",
        "dull_at": {"default": 0.10},
        "on_dull": ["compact", "new_session"],
        "cooldown_minutes": 60,
    }))
    tools.record_llm_call(
        "tight",
        4_000,
        model="opus",
        direct=True,
        context_length=1_000,
        confirm_window=True,
        breakdown={
            "system_prompt": 4_000,
            "tools": 0,
            "skills": 0,
            "conversation": 0,
        },
    )
    audit = tools.gauge_audit("tight")
    assert audit["policy_verdict"]["dull_at_applied"] == 0.1
    assert audit["policy_verdict"]["mode"] == "audit"
    assert audit["dull_tokens"] == 100
    assert audit["overhead_tokens"] == 1_000
    finding = next(item for item in audit["findings"] if item["area"] == "policy")
    assert finding["severity"] == "alert"
    assert "inatteignable" in finding["detail"]
    assert "100" in finding["detail"]
    assert "1000" in finding["detail"]


def test_gauge_blame_attributes_named_tools_and_falls_back_to_turns():
    history = [
        {"role": "user", "content": "look"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "search", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "read_file", "content": "a" * 100},
        {"role": "tool", "tool_call_id": "c2", "content": "b" * 40},
        {"role": "assistant", "content": "done"},
    ]
    tools.handle_post_llm_call(
        session_id="sess",
        turn_id="t1",
        model="opus",
        conversation_history=history,
        assistant_response="done",
    )
    # Replaying the same turn must not double the rows.
    tools.handle_post_llm_call(
        session_id="sess",
        turn_id="t1",
        model="opus",
        conversation_history=history,
        assistant_response="done",
    )
    history_again = history + [
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c3", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c3", "content": "c" * 50},
        ]},
        {"role": "assistant", "content": "ok"},
    ]
    tools.handle_post_llm_call(
        session_id="sess",
        turn_id="t2",
        model="opus",
        conversation_history=history_again,
        assistant_response="ok",
    )
    conn = tools._db()
    rows = conn.execute(
        """SELECT turn_id, tool_name, result_chars
           FROM turn_tools WHERE session_id = ? ORDER BY rowid""",
        ("sess",),
    ).fetchall()
    assert rows == [
        ("t1", "read_file", 100),
        ("t1", "search", 40),
        ("t2", "read_file", 50),
    ]
    blame = tools.gauge_blame(session_id="sess", top=1)
    assert blame["attribution"] == "tool-level"
    assert blame["by_tool"] == [
        {"tool_name": "read_file", "result_chars": 150, "calls": 2},
    ]
    full = tools.gauge_blame("sess")
    assert full["by_tool"] == [
        {"tool_name": "read_file", "result_chars": 150, "calls": 2},
        {"tool_name": "search", "result_chars": 40, "calls": 1},
    ]
    assert any(
        "principal" in finding["detail"].lower() and "read_file" in finding["detail"]
        for finding in full["findings"]
    )

    named = tools.gauge_math.extract_turn_tools(
        None,
        tool_calls=[{"id": "1", "function": {"name": "web_search"}}],
        tool_results=[{"tool_call_id": "1", "content": "z" * 12}],
    )
    assert named == {
        "attribution": "tool-level",
        "tools": [{"tool_name": "web_search", "result_chars": 12}],
    }

    tools.handle_post_llm_call(
        session_id="plain",
        turn_id="p",
        model="opus",
        assistant_response="hello only",
    )
    plain = tools.gauge_blame("plain")
    assert plain["attribution"] == "turn-only"
    assert plain["by_tool"] == []
    assert "turn-only" in plain["note"]
    assert conn.execute(
        "SELECT COUNT(*) FROM turn_tools WHERE session_id = ?",
        ("plain",),
    ).fetchone()[0] == 0

    tools.handle_post_llm_call(
        session_id="anon",
        turn_id="a",
        model="opus",
        conversation_history=[
            {"role": "user", "content": "go"},
            {"role": "tool", "content": "x" * 80},
        ],
        assistant_response="x",
    )
    anon = tools.gauge_blame(session_id="anon")
    assert anon["attribution"] == "turn-only"
    assert anon["by_tool"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM turn_tools WHERE session_id = ?",
        ("anon",),
    ).fetchone()[0] == 0


def test_gauge_blame_cites_the_tool_on_the_heavy_turn():
    conn = tools._db()
    cumulative = 0
    for index, added in enumerate((1000, 1000, 1000, 1000, 46_000)):
        cumulative += added
        conn.execute(
            "INSERT INTO recorded_turns (session_id, turn_id, chars) VALUES (?, ?, ?)",
            ("heavy", f"t{index}", cumulative),
        )
    conn.execute(
        """INSERT INTO turn_tools (session_id, turn_id, tool_name, result_chars)
           VALUES (?, ?, ?, ?)""",
        ("heavy", "t4", "read_file", 40_000),
    )
    conn.commit()
    blame = tools.gauge_blame(session_id="heavy")
    assert blame["attribution"] == "tool-level"
    assert blame["by_tool"][0]["tool_name"] == "read_file"
    finding = next(item for item in blame["findings"] if item.get("turn_id") == "t4")
    assert finding["tool_name"] == "read_file"
    assert "coupable principal" in finding["detail"]
    assert "read_file" in finding["detail"]


def test_policy_preset_is_overridden_by_explicit_keys_and_defaults_fill_gaps():
    resolved = tools.gauge_math.resolve_policy({"policy": {
        "preset": "custom",
        "presets": {
            "custom": {
                "mode": "audit",
                "dull_at": {"default": 0.50},
            },
        },
        "cooldown_minutes": 12,
    }})
    assert resolved["preset_applied"] == "custom"
    assert resolved["mode"] == "audit"
    assert resolved["sources"]["mode"] == "preset:custom"
    assert resolved["dull_at"]["default"] == 0.50
    assert resolved["sources"]["dull_at.default"] == "preset:custom"
    assert resolved["dull_at"]["flash"] == 0.55
    assert resolved["sources"]["dull_at.flash"] == "default"
    assert resolved["dull_at"]["mini"] == 0.60
    assert resolved["sources"]["dull_at.mini"] == "default"
    assert resolved["on_dull"] == ["compact", "new_session"]
    assert resolved["sources"]["on_dull"] == "default"
    assert resolved["cooldown_minutes"] == 12
    assert resolved["sources"]["cooldown_minutes"] == "config"
    assert "ne compacte jamais" in resolved["effect"]

    prudent = tools.gauge_math.resolve_policy({"policy": {
        "preset": "prudent",
        "mode": "act",
        "dull_at": {"flash": 0.50},
    }})
    assert prudent["mode"] == "act"
    assert prudent["sources"]["mode"] == "config"
    assert prudent["dull_at"]["default"] == 0.55
    assert prudent["sources"]["dull_at.default"] == "preset:prudent"
    assert prudent["dull_at"]["flash"] == 0.50
    assert prudent["sources"]["dull_at.flash"] == "config"
    assert prudent["dull_at"]["mini"] == 0.45
    assert prudent["cooldown_minutes"] == 30
    assert prudent["sources"]["cooldown_minutes"] == "preset:prudent"
    assert "action recommandée" in prudent["effect"]
    assert "ne compacte jamais" in prudent["effect"]

    unknown = tools.gauge_math.resolve_policy({"policy": {"preset": "nope"}})
    assert unknown["preset_known"] is False
    assert unknown["preset_applied"] == "standard"
    assert unknown["mode"] == "audit"
    assert unknown["dull_at"]["default"] == 0.70
    assert "inconnu" in unknown["effect"]


def test_gauge_policy_reports_the_active_preset_and_writes_nothing(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    path = Path(__file__).resolve().parents[1] / "config.yaml"
    before = path.read_text(encoding="utf-8")
    report = tools.gauge_policy()
    assert path.read_text(encoding="utf-8") == before
    assert sent == []
    assert report["preset"] == "standard"
    assert report["preset_applied"] == "standard"
    assert report["preset_known"] is True
    assert report["active"]["mode"] == "audit"
    assert report["active"]["dull_at"]["flash"] == 0.55
    assert report["active"]["on_dull"] == ["compact", "new_session"]
    assert report["active"]["cooldown_minutes"] == 60
    assert report["sources"]["mode"] == "preset:standard"
    assert report["sources"]["dull_at.default"] == "preset:standard"
    assert report["sources"]["cooldown_minutes"] == "preset:standard"
    assert "observe et recommande" in report["effect"]
    assert "ne compacte jamais" in report["effect"]
    assert "ne compacte jamais" in report["will_not"]
    assert "Hermes core" in report["will_not"]

    monkeypatch.setattr(tools, "load_config", lambda: _config(policy={"preset": "prudent"}))
    prudent = tools.gauge_policy()
    assert prudent["active"]["mode"] == "audit"
    assert prudent["active"]["dull_at"]["default"] == 0.55
    assert prudent["active"]["dull_at"]["flash"] == 0.40
    assert prudent["active"]["cooldown_minutes"] == 30
    assert prudent["sources"]["mode"] == "preset:prudent"
    assert sent == []


def test_public_tool_entry_points_return_strings():
    """The Hermes runtime rejects dict tool results — the public wrappers
    must serialize."""
    import json
    out = tools._string_check()
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "session_id" in parsed or "error" in parsed
    out = tools._string_report(format="text")
    assert isinstance(out, str) and out.strip()


# --- v0.6.1: what the chip shows around an automatic summary ----------------------

WINDOW = dict(context_length=200_000, confirm_window=True)


def _measured_session(session_id="prune", tokens=100_000):
    """A session the provider has measured: ``tokens`` of prompt in a 200k window."""
    tools.handle_post_api_request(
        session_id=session_id, model="claude-opus",
        usage={"prompt_tokens": tokens, "context_length": 200_000},
    )
    tools.record_llm_call(session_id, 4_000, model="claude-opus", **WINDOW)


def test_a_provider_count_after_a_prune_re_anchors_the_gauge_at_once():
    _measured_session()
    assert tools.gauge_check(session_id="prune")["fill_percent"] == 50.0

    tools.handle_compaction(session_id="prune", reason="proactive_prune")
    assert tools.gauge_check(session_id="prune")["fill_display"] == "\u2014"

    tools.handle_post_api_request(
        session_id="prune", model="claude-opus",
        usage={"prompt_tokens": 60_000, "context_length": 200_000},
    )
    reading = tools.gauge_check(session_id="prune")
    assert reading["fill_percent"] == 30.0
    assert reading["window_source"] == "detected"
    assert reading["estimated_tokens"] == 60_000


def test_an_interrupted_stream_after_a_prune_does_not_pin_the_gauge_near_zero():
    _measured_session()
    tools.handle_compaction(session_id="prune", reason="proactive_prune")

    # A broken pipe leaves a few hundred characters of reply. That is not the context.
    tools.handle_stream_delta(session_id="prune", delta="x" * 600, turn_id="t9", model="claude-opus")
    tools.handle_api_error(session_id="prune", error="Broken pipe", turn_id="t9", model="claude-opus")
    after_error = tools.gauge_check(session_id="prune")
    assert after_error["fill_percent"] is None
    assert after_error["status"] == "UNKNOWN"

    # A finished turn with only the reply text is just as blind.
    tools.handle_post_llm_call(
        session_id="prune", assistant_response="y" * 900, model="claude-opus", turn_id="t10", **WINDOW,
    )
    assert tools.gauge_check(session_id="prune")["fill_percent"] is None

    # The next real measurement settles it, and there is no dip on the way.
    tools.handle_post_api_request(
        session_id="prune", model="claude-opus", usage={"prompt_tokens": 70_000},
    )
    assert tools.gauge_check(session_id="prune")["fill_percent"] == 35.0


def test_a_full_transcript_after_a_prune_re_anchors_the_gauge():
    _measured_session()
    tools.handle_compaction(session_id="prune", reason="proactive_prune")
    history = [{"role": "user", "content": "z" * 40_000}]
    tools.handle_post_llm_call(
        session_id="prune", assistant_response="ok", model="claude-opus", turn_id="t11",
        conversation_history=history, **WINDOW,
    )
    reading = tools.gauge_check(session_id="prune")
    assert reading["fill_percent"] is not None
    assert reading["fill_percent"] > 0


def test_a_prune_keeps_the_detected_window_it_already_knew():
    _measured_session()
    tools.handle_compaction(session_id="prune", reason="proactive_prune")
    row = tools._db().execute(
        "SELECT detected_window, window_unresolved FROM usage WHERE session_id = ?", ("prune",)
    ).fetchone()
    assert row == (200_000, 0)

    waiting = tools.gauge_check(session_id="prune")
    assert waiting["fill_percent"] is None
    assert waiting["nominal_window"] == 200_000
    assert waiting["window_source"] == "detected"

    # A second prune before any new measurement logs a snapshot that still names the window.
    tools.handle_compaction(session_id="prune", reason="post_tool")
    snapshot = tools.compaction_events("prune")[-1]["snapshot"]
    assert snapshot["window_source"] == "detected"
    assert snapshot["fill_percent"] is None


def test_the_chip_says_when_the_context_was_summarized_and_from_what_to_what(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(tools.time, "time", lambda: now)
    _measured_session()
    monkeypatch.setattr(tools.time, "time", lambda: now + 100)
    tools.handle_compaction(session_id="prune", reason="proactive_prune")

    waiting = tools.check_payload(tools._db(), "prune")
    assert waiting["state"] == "SUMMARIZED"
    note = waiting["last_compaction"]
    assert note["automatic"] is True
    assert note["at"] == now + 100
    assert note["before_pct"] == 50.0
    assert note["after_pct"] is None
    assert note["recent"] is True

    monkeypatch.setattr(tools.time, "time", lambda: now + 130)
    tools.handle_post_api_request(
        session_id="prune", model="claude-opus", usage={"prompt_tokens": 60_000},
    )
    settled = tools.check_payload(tools._db(), "prune")
    assert settled["pct"] == 30.0
    assert settled["last_compaction"]["before_pct"] == 50.0
    assert settled["last_compaction"]["after_pct"] == 30.0
    assert settled["last_compaction"]["recent"] is True

    monkeypatch.setattr(tools.time, "time", lambda: now + 100 + 3600)
    later = tools.check_payload(tools._db(), "prune")
    assert later["last_compaction"]["recent"] is False


def test_a_manual_summary_is_not_called_automatic_and_a_reset_is_not_a_summary(monkeypatch):
    _measured_session()
    tools.handle_compaction(session_id="prune", reason="manual")
    assert tools.check_payload(tools._db(), "prune")["last_compaction"]["automatic"] is False

    _measured_session("fresh")
    tools.handle_session_reset(old_session_id="fresh", new_session_id="fresh-2", reason="new_session")
    assert tools.check_payload(tools._db(), "fresh").get("last_compaction") is None


def test_a_session_never_summarized_has_no_compaction_note():
    _measured_session()
    assert tools.check_payload(tools._db(), "prune").get("last_compaction") is None


def test_the_chip_and_gauge_check_read_one_number_and_say_where_it_comes_from():
    _measured_session("same", tokens=40_000)
    chip = tools.check_payload(tools._db(), "same")
    tool = tools.gauge_check(session_id="same")
    assert chip["pct"] == tool["fill_percent"]
    assert chip["tokens_in"] == tool["estimated_tokens"]
    assert chip["basis"] == tool["basis"] == "provider"
    assert chip["session_id_full"] == "same"

    tools.record_llm_call("replies", 8_000, model="claude-opus", **WINDOW)
    assert tools.check_payload(tools._db(), "replies")["basis"] == "estimate"

    tools.record_llm_call(
        "whole", 40_000, model="claude-opus", replace=True, direct=True, **WINDOW
    )
    assert tools.check_payload(tools._db(), "whole")["basis"] == "transcript"


def test_audit_still_reads_back_a_snapshot_written_without_a_window(monkeypatch):
    """Before v0.6.1 a prune could log ``unresolved`` and no percentage. Those rows stay readable."""
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 5 * 3600)
    tools.record_llm_call("old", 380_000, model="claude-opus", **WINDOW)
    monkeypatch.setattr(tools.time, "time", lambda: NOW - 4 * 3600)
    tools.handle_compaction(session_id="old", reason="post_tool")
    legacy = {"estimated_tokens": 150_000, "fill_percent": None, "window_source": "unresolved", "turns": 3}
    conn = tools._db()
    conn.execute(
        "INSERT INTO compaction_log (session_id, reason, snapshot, created) VALUES (?, ?, ?, ?)",
        ("old", "proactive_prune", json.dumps(legacy), NOW - 3 * 3600),
    )
    conn.commit()
    monkeypatch.setattr(tools.time, "time", lambda: NOW)

    history = tools.gauge_audit(format="json")["sessions"][0]["history"]
    assert [e["inferred"] for e in history] == [False, True]
    assert history[1]["fill_percent"] == 75.0
