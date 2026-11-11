"""JSON shape of GET /check. Uses a temp HERMES_HOME so ~/.hermes stays untouched."""

import asyncio
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest


def _load_api():
    if "fastapi" not in sys.modules:
        try:
            import fastapi  # noqa: F401
        except ImportError:
            fake = types.ModuleType("fastapi")

            class APIRouter:
                def get(self, *_args, **_kwargs):
                    def deco(fn):
                        return fn

                    return deco

                post = get

            class Request:  # only named in annotations; tests pass their own stand-in
                pass

            fake.APIRouter = APIRouter
            fake.Request = Request
            sys.modules["fastapi"] = fake
    path = Path(__file__).resolve().with_name("plugin_api.py")
    spec = importlib.util.spec_from_file_location("hermes_gauge_plugin_api_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_api()

_USAGE = """
CREATE TABLE usage (
    session_id TEXT PRIMARY KEY,
    chars_in INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    detected_window INTEGER,
    tools_chars INTEGER NOT NULL DEFAULT 0,
    skills_chars INTEGER NOT NULL DEFAULT 0,
    conversation_chars INTEGER NOT NULL DEFAULT 0,
    provider_tokens INTEGER,
    direct_chars INTEGER
)
"""

_WINDOWS = """
CREATE TABLE session_windows (
    session_id TEXT PRIMARY KEY,
    model TEXT,
    window_tokens INTEGER NOT NULL
)
"""


def _stand_in_for_plugin_storage(monkeypatch):
    """``tools.py`` imports Hermes' plugin storage. Without Hermes, give it a stand-in."""
    try:
        import plugins.plugin_storage  # noqa: F401
        return
    except ImportError:
        pass

    def plugin_db(name):
        assert name == "hermes-gauge"
        path = Path(os.environ["HERMES_HOME"]) / "plugin-data" / "hermes-gauge" / "data.db"
        return sqlite3.connect(path, check_same_thread=False)

    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_db = plugin_db
    package = types.ModuleType("plugins")
    package.plugin_storage = storage
    monkeypatch.setitem(sys.modules, "plugins", package)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    db_dir = home / "plugin-data" / "hermes-gauge"
    db_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _stand_in_for_plugin_storage(monkeypatch)
    con = sqlite3.connect(db_dir / "data.db")
    con.execute(_USAGE)
    con.execute(_WINDOWS)
    con.commit()
    con.close()
    return home


def _insert(
    home,
    *,
    session_id,
    chars_in,
    model,
    detected_window,
    turns=4,
    tools_chars=10,
    skills_chars=20,
    conversation_chars=None,
    session_window=None,
    provider_tokens=None,
    direct_chars=None,
):
    seen = time.time()
    conv = chars_in if conversation_chars is None else conversation_chars
    con = sqlite3.connect(home / "plugin-data" / "hermes-gauge" / "data.db")
    con.execute(
        """INSERT INTO usage (
               session_id, chars_in, turns, model, first_seen, last_seen,
               detected_window, tools_chars, skills_chars, conversation_chars,
               provider_tokens, direct_chars
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            chars_in,
            turns,
            model,
            seen,
            seen,
            detected_window,
            tools_chars,
            skills_chars,
            conv,
            provider_tokens,
            direct_chars,
        ),
    )
    if session_window is not None:
        con.execute(
            "INSERT INTO session_windows (session_id, model, window_tokens) VALUES (?, ?, ?)",
            (session_id, model, session_window),
        )
    con.commit()
    con.close()


def _check(body=None):
    return json.loads(api.handle("GET", "/check", body))


def test_check_json_for_glm_flash_reports_tokens_and_effective_fill(hermes_home):
    """Curl-shaped /check payload for the 231,289-character GLM-5.3-flash case."""
    _insert(
        hermes_home,
        session_id="glm-flash-demo",
        chars_in=231_289,
        model="GLM-5.3-flash",
        detected_window=1_000_000,
        conversation_chars=231_289,
    )
    payload = _check()
    age = payload.pop("age_s")
    for extra in ("name", "idle", "room", "next", "hint", "language", "system_chars"):
        payload.pop(extra, None)
    assert isinstance(age, int) and age >= 0
    # v0.6.1: where the count comes from, and the last summary (none here).
    assert payload.pop("basis") == "estimate"
    assert payload.pop("last_compaction") is None
    assert payload.pop("session_id_full") == "glm-flash-demo"
    assert payload == {
        "pct": 46.3,
        "pct_nominal": 11.6,
        "pct_effective": 46.3,
        "state": "OK",
        "color": "#00e676",
        "chars_in": 231_289,
        "chars_per_token": 4,
        "tokens_in": 115_644,
        "window_chars": 1_000_000,
        "window_tokens": 1_000_000,
        "effective_window_tokens": 250_000,
        "degradation_factor": 0.25,
        "model_family": "flash",
        "model": "GLM-5.3-flash",
        "session_id": "glm-flash-demo",
        "turns": 4,
        "tools_chars": 10,
        "skills_chars": 20,
        "conversation_chars": 231_289,
    }
    assert payload["pct"] == payload["pct_effective"]


def test_provider_tokens_win_over_the_character_estimate(hermes_home):
    """A live reply can be 271 characters while the provider counted 90,248 tokens."""
    _insert(
        hermes_home,
        session_id="20261110_041358_35d2",
        chars_in=271,
        model="z-ai/glm-5.3-flash",
        detected_window=1_048_576,
        provider_tokens=90_248,
    )
    payload = _check()
    assert payload["tokens_in"] == 90_248
    assert payload["chars_in"] == 271
    assert payload["model_family"] == "flash"
    assert payload["degradation_factor"] == 0.25
    assert payload["effective_window_tokens"] == 262_144
    assert payload["pct_nominal"] == 8.6
    assert payload["pct"] == 34.4
    assert payload["state"] == "OK"


def test_direct_chars_are_counted_once(hermes_home):
    _insert(
        hermes_home,
        session_id="transcript",
        chars_in=100,
        model="claude-opus-4-6",
        detected_window=200_000,
        direct_chars=480_000,
    )
    payload = _check()
    assert payload["tokens_in"] == 120_000
    assert payload["pct"] == 60.0
    assert payload["state"] == "WATCH"


def test_state_follows_pct_effective_not_the_nominal_fill(hermes_home):
    # Unknown family keeps 0.60 of a 200k window (120k useful).
    # 102_000 tokens is 51% nominal and 85% effective → FULL.
    _insert(
        hermes_home,
        session_id="quiet-model",
        chars_in=204_000,
        model="hermes-3",
        detected_window=200_000,
    )
    payload = _check()
    assert payload["tokens_in"] == 102_000
    assert payload["pct_nominal"] == 51.0
    assert payload["pct_effective"] == 85.0
    assert payload["pct"] == 85.0
    assert payload["state"] == "FULL"
    assert payload["color"] == "#ff5252"
    assert payload["model_family"] == "unknown"
    assert payload["effective_window_tokens"] == 120_000


def test_pro_model_colors_the_token_fill_not_chars_over_window(hermes_home):
    # 480_000 chars / 200_000 window was 240% under the old ratio.
    # In tokens that is 120_000 / 200_000 = 60% on a factor-1.0 model → WATCH.
    _insert(
        hermes_home,
        session_id="opus-session",
        chars_in=240_000,
        model="claude-opus-4-6",
        detected_window=200_000,
    )
    payload = _check()
    assert payload["model_family"] == "pro"
    assert payload["degradation_factor"] == 1.0
    assert payload["window_chars"] == payload["window_tokens"] == 200_000
    assert payload["tokens_in"] == 120_000
    assert payload["pct_nominal"] == 60.0
    assert payload["pct_effective"] == 60.0
    assert payload["state"] == "WATCH"
    assert payload["color"] == "#ffb300"


@pytest.mark.parametrize(
    ("chars_in", "pct", "state", "color"),
    [
        (239_600, 59.9, "OK", "#00e676"),
        (240_000, 60.0, "WATCH", "#ffb300"),
        (339_600, 84.9, "WATCH", "#ffb300"),
        (340_000, 85.0, "FULL", "#ff5252"),
    ],
)
def test_effective_percent_band_edges(hermes_home, chars_in, pct, state, color):
    _insert(
        hermes_home,
        session_id="band-" + str(chars_in),
        chars_in=chars_in,
        model="claude-sonnet",
        detected_window=200_000,
    )
    payload = _check()
    assert payload["pct_effective"] == pct
    assert payload["pct"] == pct
    assert payload["state"] == state
    assert payload["color"] == color


def test_missing_detected_window_falls_back_to_one_mebibyte(hermes_home):
    _insert(
        hermes_home,
        session_id="fallback-flash",
        chars_in=231_289,
        model="GLM-5.3-flash",
        detected_window=None,
    )
    payload = _check()
    assert payload["window_tokens"] == 200_000
    assert payload["window_chars"] == 200_000
    assert payload["effective_window_tokens"] == 50_000
    assert payload["tokens_in"] == 115_644
    assert payload["state"] == "FULL"


def test_session_window_is_used_when_detection_is_empty(hermes_home):
    _insert(
        hermes_home,
        session_id="stored-window",
        chars_in=231_289,
        model="GLM-5.3-flash",
        detected_window=0,
        session_window=1_000_000,
    )
    payload = _check()
    assert payload["window_tokens"] == 1_000_000
    assert payload["effective_window_tokens"] == 250_000
    assert payload["pct_effective"] == 46.3


def test_no_data_keeps_the_previous_shape(hermes_home):
    payload = _check()
    assert payload["pct"] is None and payload["state"] == "NO DATA" and payload["color"] == "#7d8a9c"
    assert "message" in payload["hint"].lower()


def test_unknown_path_still_404s(hermes_home):
    assert json.loads(api.handle("POST", "/check")) == {
        "error": "not found",
        "path": "/check",
    }


def test_session_id_query_and_truncation(hermes_home):
    long_id = "session-abcdefghijklmnop"
    _insert(
        hermes_home,
        session_id=long_id,
        chars_in=400,
        model="gpt-4o-mini",
        detected_window=128_000,
    )
    _insert(
        hermes_home,
        session_id="other",
        chars_in=800,
        model="claude-opus",
        detected_window=200_000,
    )
    payload = _check({"session_id": long_id})
    assert payload["session_id"] == long_id[:18]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["model_family"] == "mini"
    assert payload["degradation_factor"] == 0.4
    assert payload["chars_in"] == 400


# --- v0.5: preferences, audit, incident report -----------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temp copy of the shipped config.yaml, used by the API and by the plugin alike."""
    target = tmp_path / "config.yaml"
    shutil.copy(api._PLUGIN_DIR / "config.yaml", target)
    monkeypatch.setattr(api, "_CONFIG_PATH", target)
    return target


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _post(route, payload):
    return asyncio.run(route(FakeRequest(payload)))


def _load_gauge_tools(home):
    """Load tools.py with a stand-in for Hermes' plugin storage, then put the real modules back."""
    names = ("plugins", "plugins.plugin_storage")
    saved = {name: sys.modules.get(name) for name in names}
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_db(name):
        assert name == "hermes-gauge"
        path = Path(os.environ["HERMES_HOME"]) / "plugin-data" / "hermes-gauge" / "data.db"
        return sqlite3.connect(path, check_same_thread=False)

    storage.plugin_db = plugin_db
    package = types.ModuleType("plugins")
    package.plugin_storage = storage
    sys.modules["plugins"], sys.modules["plugins.plugin_storage"] = package, storage
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_gauge_tools_api_test", api._PLUGIN_DIR / "tools.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


@pytest.fixture
def gauge_tools(hermes_home, cfg, tmp_path, monkeypatch):
    module = _load_gauge_tools(hermes_home)
    module._CONFIG_PATH = cfg
    module._DATA_DIR = tmp_path / "plugin-files"
    module._STATE_DB = "/nonexistent/hermes-gauge-tests/state.db"
    monkeypatch.setattr(api, "_TOOLS", module)
    return module


def test_check_is_unchanged_when_config_is_the_shipped_one(hermes_home, cfg):
    _insert(hermes_home, session_id="s", chars_in=204_000, model="hermes-3", detected_window=200_000)
    payload = _check()
    assert payload["pct"] == 85.0 and payload["state"] == "FULL" and payload["chars_per_token"] == 4
    assert "thresholds" not in payload


def test_check_follows_the_thresholds_in_config(hermes_home, cfg):
    _insert(hermes_home, session_id="s", chars_in=100_000, model="hermes-3", detected_window=200_000)
    assert _check()["state"] == "OK"  # 50_000 tokens of 120_000 useful: 41.7%
    result = api._plugin_module("prefs").set_values(
        {"thresholds.watch": "0.30", "thresholds.full": "0.40"}, cfg
    )
    assert result["ok"] is True
    again = _check()
    assert again["pct"] == 41.7 and again["state"] == "FULL"


def test_check_follows_chars_per_token_in_config(hermes_home, cfg):
    _insert(hermes_home, session_id="s", chars_in=100_000, model="hermes-3", detected_window=200_000)
    assert _check()["tokens_in"] == 50_000
    api._plugin_module("prefs").set_values({"chars_per_token": "2"}, cfg)
    payload = _check()
    assert payload["tokens_in"] == 100_000 and payload["chars_per_token"] == 2


def test_check_falls_back_to_v04_numbers_when_config_is_unusable(hermes_home, cfg, tmp_path, monkeypatch):
    _insert(hermes_home, session_id="s", chars_in=204_000, model="hermes-3", detected_window=200_000)
    cfg.write_text("thresholds:\n  watch: 0.9\n  full: 0.5\n", encoding="utf-8")  # watch above full
    assert _check()["state"] == "FULL" and _check()["pct"] == 85.0
    cfg.write_bytes(b"\xff\xfe not text")
    assert _check()["state"] == "FULL"
    monkeypatch.setattr(api, "_CONFIG_PATH", None)
    monkeypatch.setattr(api, "_PLUGIN_DIR", tmp_path / "missing-plugin-folder")
    assert _check()["state"] == "FULL"  # chip survives a deploy without the plugin folder


def test_get_config_returns_a_dict_with_values_sources_and_fields(cfg):
    view = api._api_config()
    assert isinstance(view, dict) and view["ok"] is True
    assert view["flat"]["thresholds.watch"] == 0.6
    assert view["flat"]["alerts.route"] == "session"
    assert view["sources"]["alerts.route"] == "default"
    assert {field["key"] for field in view["fields"]} >= {
        "thresholds.watch", "thresholds.full", "alerts.enabled", "alerts.on",
        "alerts.cooldown_minutes", "alerts.route", "output_reserve", "chars_per_token",
    }


def test_post_config_writes_the_same_file_the_plugin_reads(cfg, gauge_tools):
    before = cfg.read_text(encoding="utf-8")
    result = _post(api._api_config_set, {"values": {"alerts.route": "home", "alerts.cooldown_minutes": 10}})
    assert result["ok"] is True
    assert sorted(result["changed"]) == ["alerts.cooldown_minutes", "alerts.route"]
    assert result["flat"]["alerts.route"] == "home" and result["sources"]["alerts.route"] == "file"
    # What the panel saved is what /gauge config and the alert code now read.
    assert gauge_tools.config_view()["flat"]["alerts.route"] == "home"
    assert gauge_tools.gauge_math.alert_settings(gauge_tools.load_config())["route"] == "home"
    assert "# Where the push lands." in cfg.read_text(encoding="utf-8")  # comments survive
    assert cfg.read_text(encoding="utf-8") != before


def test_post_config_accepts_a_flat_body(cfg):
    assert _post(api._api_config_set, {"chars_per_token": "3"})["ok"] is True
    assert api._api_config()["flat"]["chars_per_token"] == 3


def test_post_config_refuses_bad_values_and_writes_nothing(cfg):
    before = cfg.read_text(encoding="utf-8")
    bad_route = _post(api._api_config_set, {"values": {"alerts.route": "somewhere", "chars_per_token": "0"}})
    assert bad_route["ok"] is False
    assert bad_route["errors"]["alerts.route"]["code"] == "invalid_route"
    assert "chars_per_token" in bad_route["errors"]
    crossed = _post(api._api_config_set, {"values": {"thresholds.watch": "0.95", "thresholds.full": "0.9"}})
    assert crossed["ok"] is False
    assert {error["code"] for error in crossed["errors"].values()} == {"watch_not_below_full"}
    assert cfg.read_text(encoding="utf-8") == before


def test_post_config_with_an_empty_or_broken_body_is_a_refusal_not_an_error(cfg):
    assert _post(api._api_config_set, {})["ok"] is False
    assert _post(api._api_config_set, ValueError("not json"))["ok"] is False
    assert _post(api._api_config_set, ["not", "a", "dict"])["ok"] is False
    assert _post(api._api_config_set, {"nope": 1})["errors"]["nope"]["code"] == "unknown_key"


def test_audit_route_returns_a_dict_with_compaction_counts(hermes_home, gauge_tools):
    gauge_tools.record_llm_call("sess-a", 380_000, model="claude-opus", context_length=200_000, confirm_window=True)
    gauge_tools.handle_compaction(session_id="sess-a", reason="post_tool")
    gauge_tools.handle_compaction(session_id="sess-a", reason="new_session")
    audit = api._api_audit(limit=5, lang="fr")
    assert isinstance(audit, dict)
    assert audit["totals"]["compactions"] == 1 and audit["totals"]["resets"] == 1
    assert audit["sessions"][0]["peak"]["status"] == "WATCH" or audit["sessions"][0]["peak"]["fill_percent"] > 0
    assert audit["lang"] == "fr" and "text" not in audit
    one = api._api_audit(limit=5, lang="en", session_id="sess-a")
    assert one["session_count"] == 1


def test_incident_route_returns_data_and_optionally_the_page(hermes_home, gauge_tools):
    gauge_tools.record_llm_call("sess-a", 380_000, model="claude-opus", context_length=200_000, confirm_window=True)
    gauge_tools.handle_compaction(session_id="sess-a", reason="proactive_prune")
    data = api._api_incidents()
    assert isinstance(data, dict) and data["kpis"]["compactions"] == 1
    assert data["verdict"]["level"] in ("red", "amber", "green")
    assert "html" not in data and "llm" in data
    page = api._api_incidents(lang="en", format="html")
    assert page["html"].startswith("<!DOCTYPE html>") and page["lang"] == "en"
    assert api._api_incidents(days="0")["period"]["days"] is None


def test_incident_post_writes_the_page_and_returns_a_file_url(hermes_home, gauge_tools):
    gauge_tools.record_llm_call("sess-a", 4_000, model="claude-opus")
    result = _post(api._api_incidents_write, {"lang": "en", "days": 7})
    assert result["ok"] is True and result["written"] is True
    path = Path(result["path"])
    assert path.is_file() and path.suffix == ".html"
    assert result["url"] == path.as_uri() and result["url"].startswith("file://")
    assert result["media"] == f"MEDIA:{path}"
    assert "html" not in result and result["lang"] == "en"
    assert _post(api._api_incidents_write, ValueError("no body"))["ok"] is True  # defaults apply


def test_routes_turn_a_failure_into_a_dict(hermes_home, gauge_tools, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(gauge_tools, "gauge_audit", boom)
    result = api._api_audit()
    assert result == {"ok": False, "error": "RuntimeError: database is locked"}
