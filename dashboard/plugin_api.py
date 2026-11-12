"""HTTP backend for the desktop half of the hermes-gauge plugin.

Serves /api/plugins/hermes-gauge/* to the Hermes Desktop app.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def _load_effective_window():
    """Load the sibling module by path so a file-based plugin loader can see it."""
    path = Path(__file__).resolve().with_name("effective_window.py")
    name = "hermes_gauge_effective_window"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_windows = _load_effective_window()

# The plugin proper sits beside this folder: ``<plugin>/dashboard`` and
# ``<plugin>/hermes-gauge``. Its preferences, audit and incident report are
# loaded from there, so the panel and ``/gauge`` read one file and one validator.
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes-gauge"
_CONFIG_PATH = None  # tests point this at a temp copy of config.yaml
_TOOLS = None  # tests may put a prepared module here
DEFAULT_WATCH = 60.0
DEFAULT_FULL = 85.0


def _plugin_module(name):
    """Load ``hermes-gauge/<name>.py`` by path, sharing the cache the plugin uses."""
    key = f"hermes_gauge_{name}"
    path = _PLUGIN_DIR / f"{name}.py"
    cached = sys.modules.get(key)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _tools():
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = _plugin_module("tools")
    # The chip and /gauge read one file. Tests pin that file on this module.
    _TOOLS._CONFIG_PATH = _CONFIG_PATH
    return _TOOLS


def _config_file():
    if _CONFIG_PATH is not None:
        return Path(_CONFIG_PATH)
    return _plugin_module("prefs").resolve_config_path()


def _prefs_in_force():
    """``(watch %, full %, chars per token)`` from config.yaml, defaults if unreadable.

    The chip must keep working when the plugin folder or the file is missing,
    so any failure here returns the v0.4 numbers.
    """
    try:
        config = _plugin_module("prefs").effective(_config_file())
        thresholds = config.get("thresholds") or {}
        watch = float(thresholds.get("watch", DEFAULT_WATCH / 100.0)) * 100.0
        full = float(thresholds.get("full", DEFAULT_FULL / 100.0)) * 100.0
        per = _plugin_module("gauge_math").chars_per_token_from(config)
        if not (0.0 < watch < full <= 100.0):
            raise ValueError("unusable thresholds")
        return round(watch, 2), round(full, 2), per
    except Exception:  # noqa: BLE001 - a bad file must never take the chip down
        return DEFAULT_WATCH, DEFAULT_FULL, _windows.CHARS_PER_TOKEN


def _hermes_home() -> Path:
    """Hermes home of the active profile; the environment is only the fallback outside Hermes."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        home = os.environ.get("HERMES_HOME")
        return Path(home).expanduser() if home else Path.home() / ".hermes"
    return Path(get_hermes_home())


def _db_path() -> str:
    return str(_hermes_home() / "plugin-data" / "hermes-gauge" / "data.db")


def _colors(pct: float, watch: float = DEFAULT_WATCH, full: float = DEFAULT_FULL) -> dict:
    """Bands on the effective fill: OK below ``watch``, WATCH below ``full``, FULL above.

    The defaults are 60 and 85. config.yaml can move them.
    """
    if pct < watch:
        return {"color": "#00e676", "state": "OK"}
    if pct < full:
        return {"color": "#ffb300", "state": "WATCH"}
    return {"color": "#ff5252", "state": "FULL"}


def _percent(part: int, whole: int) -> float:
    return round(100.0 * part / max(int(whole), 1), 1)


def _optional(row, key):
    """A column the row may not have. Missing or absent stays ``None``."""
    try:
        names = row.keys()
    except AttributeError:
        return None
    if key not in names:
        return None
    return row[key]


def _tokens_in(row, chars_per_token: int) -> int:
    """Token count for one usage row.

    A provider count wins, then a measured full transcript (``direct_chars``,
    divided by ``chars_per_token``). A row that only has ``chars_in`` uses
    that same division: ``/check`` treats those characters as the context.
    """
    provider = _optional(row, "provider_tokens")
    if provider is not None:
        try:
            return max(int(provider), 0)
        except (TypeError, ValueError):
            pass
    direct = _optional(row, "direct_chars")
    if direct is not None:
        return _windows.tokens_from_chars(direct, chars_per_token)
    return _windows.tokens_from_chars(row["chars_in"], chars_per_token)


def _reading(row, window: int, watch: float = DEFAULT_WATCH, full: float = DEFAULT_FULL,
             chars_per_token: int = _windows.CHARS_PER_TOKEN) -> dict:
    """One /check payload. ``window`` is the detected nominal length, in tokens.

    ``window_chars`` keeps that same integer for clients that already read it.
    ``pct`` is the effective fill, and it is the number that colors ``state``.
    """
    window_tokens = int(window or 0)
    if window_tokens < 1:
        window_tokens = 1
    tokens_in = _tokens_in(row, chars_per_token)
    family, factor = _windows.family_factor(row["model"])
    useful = _windows.effective_tokens(window_tokens, factor)
    pct_nominal = _percent(tokens_in, window_tokens)
    pct_effective = _percent(tokens_in, useful)
    band = _colors(pct_effective, watch, full)
    return {
        "pct": pct_effective,
        "pct_nominal": pct_nominal,
        "pct_effective": pct_effective,
        "state": band["state"],
        "color": band["color"],
        "chars_in": row["chars_in"],
        "chars_per_token": chars_per_token,
        "tokens_in": tokens_in,
        "window_chars": window_tokens,
        "window_tokens": window_tokens,
        "effective_window_tokens": useful,
        "degradation_factor": factor,
        "model_family": family,
        "model": row["model"],
        "session_id": row["session_id"][:18],
        "turns": row["turns"],
        "tools_chars": row["tools_chars"],
        "skills_chars": row["skills_chars"],
        "conversation_chars": row["conversation_chars"],
        "age_s": round(time.time() - row["last_seen"]),
    }


from fastapi import APIRouter, Request  # noqa: E402

router = APIRouter()


def _safe(call):
    """Run an endpoint body. A failure becomes a dict the panel can show, not a 500."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _json_body(request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - empty or malformed body
        return {}
    return payload if isinstance(payload, dict) else {}


@router.get("")
@router.get("/")
@router.get("/check")
def _api_check(session_id: str = "") -> dict:
    """FastAPI entry point mounted under /api/plugins/hermes-gauge (strict JSON)."""
    try:
        body = {"session_id": session_id} if session_id else None
        return json.loads(handle("GET", "/check", body))
    except sqlite3.Error:
        return {"pct": None, "state": "NO DATA", "color": "#7d8a9c", "hint": "Send your first message."}


def handle(method: str, path: str, body: dict | None = None) -> str:
    """Entry point the gateway calls for /api/plugins/hermes-gauge."""
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    path = path.rstrip("/")
    session_id = (body or {}).get("session_id")
    if not session_id and query:
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "session_id" and value:
                session_id = value
                break
    try:
        con = sqlite3.connect(_db_path())
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return json.dumps({
            "pct": None, "state": "NO DATA", "color": "#7d8a9c",
            "hint": "Send your first message.",
        })
    if method == "GET" and (path == "" or path == "/check"):
        try:
            return json.dumps(_tools().check_payload(con, session_id))
        except sqlite3.Error:
            return json.dumps({
                "pct": None, "state": "NO DATA", "color": "#7d8a9c",
                "hint": "Send your first message.",
            })
    return json.dumps({"error": "not found", "path": path})


# --- v0.5 -----------------------------------------------------------------------
# Every route below returns a dict. A refusal is ``{"ok": False, ...}`` with a 200,
# so the panel (``ctx.rest``) can read the reason instead of losing it to an HTTP error.


@router.get("/config")
def _api_config() -> dict:
    """Preferences in force, where each value comes from, and the field rules."""
    return _safe(lambda: _plugin_module("prefs").view(_config_file()))


@router.post("/config")
async def _api_config_set(request: Request) -> dict:
    """Validate and save preferences. Body: ``{"values": {"alerts.route": "home"}}``.

    Same file and same validator as ``/gauge config set``. All or nothing.
    """
    body = await _json_body(request)
    values = body.get("values") if isinstance(body.get("values"), dict) else body
    return _safe(lambda: _plugin_module("prefs").set_values(values, _config_file()))


@router.get("/audit")
def _api_audit(limit: int = 10, lang: str = "en", session_id: str = "") -> dict:
    """Peak history, compactions and composition, per session and overall."""
    return _safe(lambda: _tools().gauge_audit(
        session_id=session_id or None, limit=limit, lang=lang, format="json"
    ))


@router.get("/report/incidents")
def _api_incidents(lang: str = "", days: str = "", format: str = "json") -> dict:
    """The incident report as data. ``format=html`` adds the page as ``html``.

    No ``lang`` follows the language setting, which is English unless it says French.
    """
    return _safe(lambda: _tools().gauge_incidents(
        lang=lang or None, days=days or None, format="html" if format == "html" else "json"
    ))


@router.post("/report/incidents")
async def _api_incidents_write(request: Request) -> dict:
    """Write the incident page to disk. ``path`` and ``url`` say where; open ``url`` in a browser."""
    body = await _json_body(request)

    def build():
        report = _tools().gauge_incidents(
            lang=body.get("lang") or None, days=body.get("days"), write=True
        )
        if report.get("path"):
            report["url"] = Path(report["path"]).as_uri()
        report["ok"] = bool(report.get("written"))
        return report

    return _safe(build)
