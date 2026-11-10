"""Preferences: one config.yaml, one validator, one writer.

`prefs.py` is stdlib only and loads by path, like `tools.py`. Every test works
on a copy of the shipped file in a temp folder, so the repo file never changes.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"hermes_gauge_{name}_under_test", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prefs = _load("prefs")
gauge_math = prefs.gauge_math


@pytest.fixture
def cfg(tmp_path):
    target = tmp_path / "config.yaml"
    shutil.copy(HERE / "config.yaml", target)
    return target


def test_shipped_file_shows_defaults_and_marks_what_is_set(cfg):
    snapshot = prefs.view(cfg)
    assert snapshot["ok"] is True
    flat = snapshot["flat"]
    assert flat["thresholds.watch"] == 0.60
    assert flat["thresholds.full"] == 0.85
    assert flat["alerts.enabled"] is True
    assert flat["alerts.on"] == ["WATCH", "FULL"]
    assert flat["alerts.cooldown_minutes"] == 30
    assert flat["alerts.route"] == "session"
    assert flat["output_reserve"] == 0
    assert flat["chars_per_token"] == 4
    # route and chars_per_token ship as commented examples, so they are defaults.
    assert snapshot["sources"]["alerts.route"] == "default"
    assert snapshot["sources"]["chars_per_token"] == "default"
    assert snapshot["sources"]["thresholds.watch"] == "file"
    assert snapshot["values"]["alerts"]["route"] == "session"
    assert [f["key"] for f in snapshot["fields"]] == [f["key"] for f in prefs.FIELDS]


def test_view_survives_a_missing_file(tmp_path):
    snapshot = prefs.view(tmp_path / "nope.yaml")
    assert snapshot["flat"]["thresholds.full"] == 0.85
    assert set(snapshot["sources"].values()) == {"default"}


def test_set_writes_the_key_in_place_and_keeps_every_comment(cfg):
    before = cfg.read_text(encoding="utf-8")
    result = prefs.set_values({"alerts.cooldown_minutes": "10"}, cfg)
    assert result["ok"] is True
    assert result["changed"] == {"alerts.cooldown_minutes": 10}
    after = cfg.read_text(encoding="utf-8")
    assert "    cooldown_minutes: 10\n" in after
    removed = [line for line in before.splitlines() if line not in after.splitlines()]
    assert removed == ["    cooldown_minutes: 30"]
    assert prefs.view(cfg)["flat"]["alerts.cooldown_minutes"] == 10


def test_every_surface_reads_what_set_wrote(cfg):
    prefs.set_values({
        "thresholds.watch": "50%",
        "thresholds.full": "0.9",
        "alerts.enabled": "off",
        "alerts.on": "FULL",
        "alerts.route": "custom:telegram:-1001234567890",
        "output_reserve": "8000",
        "chars_per_token": "3",
    }, cfg)
    config = prefs.effective(cfg)
    assert config["thresholds"] == {"watch": 0.5, "full": 0.9}
    assert config["output_reserve"] == 8000
    assert gauge_math.chars_per_token_from(config) == 3
    alerts = gauge_math.alert_settings(config)
    assert alerts["enabled"] is False
    assert alerts["on"] == ["FULL"]
    assert alerts["route"] == "custom:telegram:-1001234567890"


def test_new_keys_are_inserted_under_the_right_parent(cfg):
    prefs.set_values({"alerts.route": "home", "chars_per_token": 5}, cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "    route: home\n" in text
    assert "\nchars_per_token: 5\n" in text
    # route landed inside the alerts block, not at the end of the file.
    alerts_at = text.index("  alerts:\n")
    assert alerts_at < text.index("    route: home") < alerts_at + len(text.split("  alerts:\n", 1)[1])
    assert prefs.view(cfg)["sources"]["alerts.route"] == "file"


def test_set_creates_a_missing_file_and_missing_parents(tmp_path):
    target = tmp_path / "config.yaml"
    result = prefs.set_values({"alerts.route": "home:telegram", "thresholds.full": 0.9}, target)
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == (
        "gauge:\n  alerts:\n    route: \"home:telegram\"\n\nthresholds:\n  full: 0.9\n"
    )
    snapshot = prefs.view(target)
    assert snapshot["flat"]["alerts.route"] == "home:telegram"
    assert snapshot["flat"]["thresholds.full"] == 0.9


def test_top_level_alerts_alias_is_read_and_updated_where_it_lives(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("alerts:\n  route: home\n  cooldown_minutes: 5\n", encoding="utf-8")
    snapshot = prefs.view(target)
    assert snapshot["flat"]["alerts.route"] == "home"
    assert snapshot["flat"]["alerts.cooldown_minutes"] == 5
    prefs.set_values({"alerts.route": "session"}, target)
    assert target.read_text(encoding="utf-8").startswith("alerts:\n  route: session\n")
    assert "gauge:" not in target.read_text(encoding="utf-8")
    assert gauge_math.alert_settings(prefs.effective(target))["route"] == "session"


def test_gauge_alerts_wins_over_the_alias(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(
        "alerts:\n  route: home\ngauge:\n  alerts:\n    route: session\n", encoding="utf-8"
    )
    assert gauge_math.alert_settings(prefs.effective(target))["route"] == "session"


@pytest.mark.parametrize("key,raw,code", [
    ("thresholds.watch", "abc", "not_a_number"),
    ("thresholds.watch", "0.01", "out_of_range"),
    ("thresholds.full", "1", "out_of_range"),
    ("alerts.enabled", "maybe", "not_a_boolean"),
    ("alerts.on", "WATCH, CRITICAL", "invalid_choice"),
    ("alerts.cooldown_minutes", "-1", "out_of_range"),
    ("alerts.route", "everywhere", "invalid_route"),
    ("alerts.route", "custom:telegram", "invalid_route"),
    ("alerts.route", "custom:Tele gram:1", "invalid_route"),
    ("output_reserve", "1.5", "not_an_integer"),
    ("chars_per_token", "0", "out_of_range"),
    ("chars_per_token", "9", "out_of_range"),
    ("nope", "1", "unknown_key"),
])
def test_invalid_values_are_refused_with_a_code(cfg, key, raw, code):
    before = cfg.read_text(encoding="utf-8")
    result = prefs.set_values({key: raw}, cfg)
    assert result["ok"] is False
    assert result["changed"] == {}
    assert result["errors"][prefs.canonical_key(key) or key]["code"] == code
    assert cfg.read_text(encoding="utf-8") == before


def test_one_bad_value_writes_nothing_at_all(cfg):
    before = cfg.read_text(encoding="utf-8")
    result = prefs.set_values({"alerts.cooldown_minutes": 5, "alerts.route": "nowhere"}, cfg)
    assert result["ok"] is False
    assert cfg.read_text(encoding="utf-8") == before


def test_watch_must_stay_below_full_including_against_the_stored_value(cfg):
    result = prefs.set_values({"thresholds.watch": "0.9"}, cfg)
    assert result["ok"] is False
    assert result["errors"]["thresholds.watch"]["code"] == "watch_not_below_full"
    result = prefs.set_values({"thresholds.watch": "0.9", "thresholds.full": "0.95"}, cfg)
    assert result["ok"] is True
    assert prefs.view(cfg)["flat"]["thresholds.watch"] == 0.9


def test_percent_forms_are_understood():
    assert prefs.validate("thresholds.watch", "60%")[0] == 0.6
    assert prefs.validate("thresholds.watch", "60")[0] == 0.6
    assert prefs.validate("thresholds.watch", "0,6")[0] == 0.6
    assert prefs.validate("thresholds.full", 0.85)[0] == 0.85


def test_alert_status_list_accepts_the_common_spellings():
    for raw in ("WATCH,FULL", "[WATCH, FULL]", "watch full", ["WATCH", "FULL"]):
        assert prefs.validate("alerts.on", raw)[0] == ["WATCH", "FULL"]
    assert prefs.validate("alerts.on", "none")[0] == []
    assert prefs.validate("alerts.on", "full")[0] == ["FULL"]


def test_gauge_alerts_dotted_alias_is_accepted_as_a_key():
    assert prefs.canonical_key("gauge.alerts.route") == "alerts.route"
    assert prefs.canonical_key("ALERTS.Route") == "alerts.route"
    assert prefs.canonical_key("fallback_window") is None


def test_a_file_that_cannot_be_honored_shows_defaults_not_garbage(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(
        "thresholds:\n  watch: 0.9\n  full: 0.5\nchars_per_token: lots\n"
        "gauge:\n  alerts:\n    route: elsewhere\n",
        encoding="utf-8",
    )
    snapshot = prefs.view(target)
    assert snapshot["flat"]["thresholds.watch"] == 0.6
    assert snapshot["flat"]["thresholds.full"] == 0.85
    assert snapshot["flat"]["chars_per_token"] == 4
    assert snapshot["flat"]["alerts.route"] == "session"
    bad = {f["key"] for f in snapshot["fields"] if "invalid_in_file" in f}
    assert {"thresholds.watch", "thresholds.full", "chars_per_token", "alerts.route"} <= bad
    config = prefs.effective(target)
    assert gauge_math.chars_per_token_from(config) == 4
    assert gauge_math.alert_settings(config)["route"] == "session"


def test_route_grammar():
    parse = gauge_math.parse_route
    assert parse("session")["kind"] == "session"
    assert parse(None) is None
    assert parse("")["kind"] == "session"
    assert parse("HOME")["value"] == "home"
    assert parse("home:Telegram")["target"] == "telegram"
    custom = parse("custom:telegram:-1001234567890:17585")
    assert custom["target"] == "telegram:-1001234567890:17585"
    assert parse("custom:slack:C0123ABCD")["target"] == "slack:C0123ABCD"
    for bad in ("custom:", "custom:telegram:", "custom::1", "custom:telegram:a b", "home:", "home:T T", "x", "custom:telegram:1::2", 5):
        assert parse(bad) is None, bad


def test_parse_assignments_handles_quotes_and_lists():
    assert prefs.parse_assignments('alerts.route=home output_reserve=100') == {
        "alerts.route": "home",
        "output_reserve": "100",
    }
    assert prefs.parse_assignments("alerts.on=[WATCH, FULL] alerts.route='custom:telegram:1'") == {
        "alerts.on": "[WATCH, FULL]",
        "alerts.route": "custom:telegram:1",
    }
    assert prefs.parse_assignments("nothing here") == {}


def test_text_views_are_short_and_readable(cfg):
    text = prefs.render_view_text(prefs.view(cfg))
    assert "thresholds.watch" in text and "60 %" in text
    assert "alerts.route" in text and "(default)" in text
    assert "/gauge config set" in text
    saved = prefs.render_set_text(prefs.set_values({"alerts.route": "home"}, cfg))
    assert saved.splitlines() == ["Saved.", "alerts.route = home"]
    refused = prefs.render_set_text(prefs.set_values({"alerts.route": "mars"}, cfg))
    assert refused.startswith("Nothing was changed.")
    assert "alerts.route:" in refused


def test_an_unwritable_target_reports_instead_of_raising(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    result = prefs.set_values({"output_reserve": 1}, blocked / "config.yaml")
    assert result["ok"] is False
    assert result["errors"]["_"]["code"] == "write_failed"
