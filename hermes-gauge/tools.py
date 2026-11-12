"""Handlers — what runs."""

import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from plugins.plugin_storage import plugin_db

try:
    from . import gauge_math
except ImportError:  # loaded by path from pytest, not as a package
    import importlib.util

    _MATH_PATH = Path(__file__).with_name("gauge_math.py")
    _math_spec = importlib.util.spec_from_file_location("hermes_gauge_math", _MATH_PATH)
    gauge_math = importlib.util.module_from_spec(_math_spec)
    _math_spec.loader.exec_module(gauge_math)



def _sibling(name):
    """Load a module that sits next to this file, as a package member or by path."""
    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    key = f"hermes_gauge_{name}"
    path = Path(__file__).with_name(f"{name}.py")
    cached = sys.modules.get(key)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


_LOG = logging.getLogger("hermes_gauge")

# In-flight stream text, keyed by (session_id, turn_id). Not the source of
# truth — a commit writes SQLite once per turn, and a later final frame only
# adds the characters the buffer had not already counted.
_BUFFERS = {}

# PluginContext captured at register(). Alerts post through it. Tests replace
# ``_ALERT_SINK`` and point ``_DATA_DIR`` at a temp folder.
_PLUGIN_CTX = None
_ALERT_SINK = None
_DATA_DIR = None
# Tests point these at temp files. ``_CONFIG_PATH`` None means the shipped
# ``config.yaml`` beside this file; ``_ROUTE_SENDER`` replaces ``hermes send``.
_CONFIG_PATH = None
_STATE_DB = None  # tests point this at a fixture; None means Hermes' own state.db
_ROUTE_SENDER = None
# Tests replace this. None asks Hermes through PluginContext.inject_message.
_HERMES_COMMAND = None
# Tests point this at a temporary Hermes root for the memory-bank audit.
_HERMES_HOME = None

# Active memory bank. A fill above this share of the configured char budget
# is an alert. Two entries are near-duplicates when the smaller token set
# shares more than this fraction, and only when both sets are long enough
# that a short heading cannot trip the check.
_MEMORY_BUDGET_ALERT = 0.90
_MEMORY_DUP_ABOVE = 0.80
_MEMORY_MIN_TOKENS = 8
# Still on the labeled fallback after this many turns: name the command
# that records a real window (gauge_set_window, or models: in config.yaml).
FALLBACK_TURNS_BEFORE_WARN = 3
_MEMORY_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ_]+")

_USAGE_EXTRA = (
    ("anchor_valid", "INTEGER NOT NULL DEFAULT 1"),
    ("window_unresolved", "INTEGER NOT NULL DEFAULT 0"),
    ("detected_window", "INTEGER"),
    ("provider_tokens", "INTEGER"),
    ("direct_chars", "INTEGER"),
    ("notified_status", "TEXT"),
    ("watch_alerted_at", "REAL"),
    ("full_alerted_at", "REAL"),
    ("system_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("tools_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("skills_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("conversation_chars", "INTEGER NOT NULL DEFAULT 0"),
    # Dull-threshold hysteresis. Separate from notified_status so a WATCH
    # alert and a dull recommendation can each fire once per crossing.
    ("dull_latched", "INTEGER NOT NULL DEFAULT 0"),
    ("dull_alerted_at", "REAL"),
)

# Added to ``recorded_turns`` in v0.5. ``created`` dates a turn so a fill curve
# can sit on a real time axis; rows written before it stay NULL and are never
# given an invented date. ``absolute`` marks a whole-transcript measurement
# (the row is the fill itself) against a reply size that adds to the fill.
_TURNS_EXTRA = (
    ("created", "REAL"),
    ("absolute", "INTEGER NOT NULL DEFAULT 0"),
)


def config_path():
    """The one ``config.yaml`` every surface reads and writes.

    Tests pin ``_CONFIG_PATH``. Otherwise the file lives under
    ``plugin-data/hermes-gauge/`` so a plugin update does not wipe it.
    The copy shipped next to this module is only the defaults.
    """
    if _CONFIG_PATH is not None:
        return Path(_CONFIG_PATH)
    return _sibling("prefs").resolve_config_path()


def _parsed_config(path):
    """Parsed YAML, or None when the file is missing or unreadable.

    A bad file must not take the gauge down: the caller keeps the defaults.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        return gauge_math.parse_config(text)
    except Exception:
        return None


def load_config():
    """Defaults, then the shipped file, then the user's file.

    A test that pins ``_CONFIG_PATH`` reads that file alone, on top of the defaults.
    An unreadable file is skipped, so the gauge keeps the defaults.
    """
    config = gauge_math.default_config()
    if _CONFIG_PATH is not None:
        parsed = _parsed_config(_CONFIG_PATH)
        if not parsed:
            return config
        return gauge_math.merge_config(config, parsed)
    shipped = Path(__file__).with_name("config.yaml")
    parsed = _parsed_config(shipped)
    if parsed:
        config = gauge_math.merge_config(config, parsed)
    user = config_path()
    if user.resolve() != shipped.resolve():
        parsed = _parsed_config(user)
        if parsed:
            config = gauge_math.merge_config(config, parsed)
    return config


def _db():
    conn = plugin_db("hermes-gauge")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage (
               session_id TEXT PRIMARY KEY,
               chars_in INTEGER NOT NULL DEFAULT 0,
               turns INTEGER NOT NULL DEFAULT 0,
               model TEXT,
               first_seen REAL NOT NULL,
               last_seen REAL NOT NULL,
               anchor_valid INTEGER NOT NULL DEFAULT 1,
               window_unresolved INTEGER NOT NULL DEFAULT 0,
               detected_window INTEGER,
               provider_tokens INTEGER,
               direct_chars INTEGER,
               notified_status TEXT,
               watch_alerted_at REAL,
               full_alerted_at REAL,
               system_chars INTEGER NOT NULL DEFAULT 0,
               tools_chars INTEGER NOT NULL DEFAULT 0,
               skills_chars INTEGER NOT NULL DEFAULT 0,
               conversation_chars INTEGER NOT NULL DEFAULT 0
           )"""
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
    for name, decl in _USAGE_EXTRA:
        if name not in existing:
            conn.execute(f"ALTER TABLE usage ADD COLUMN {name} {decl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS windows (
               model TEXT PRIMARY KEY,
               window_tokens INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS model_degradation (
               target TEXT PRIMARY KEY,
               factor REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_windows (
               session_id TEXT PRIMARY KEY,
               model TEXT,
               window_tokens INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agents (
               session_id TEXT NOT NULL,
               agent_id TEXT NOT NULL,
               model TEXT,
               chars_in INTEGER NOT NULL DEFAULT 0,
               detected_window INTEGER,
               direct_chars INTEGER,
               PRIMARY KEY (session_id, agent_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS recorded_turns (
               session_id TEXT NOT NULL,
               turn_id TEXT NOT NULL,
               chars INTEGER NOT NULL DEFAULT 0,
               created REAL,
               absolute INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (session_id, turn_id)
           )"""
    )
    existing_turns = {row[1] for row in conn.execute("PRAGMA table_info(recorded_turns)")}
    for name, decl in _TURNS_EXTRA:
        if name not in existing_turns:
            conn.execute(f"ALTER TABLE recorded_turns ADD COLUMN {name} {decl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS turn_tools (
               session_id TEXT NOT NULL,
               turn_id TEXT NOT NULL,
               tool_name TEXT NOT NULL,
               result_chars INTEGER NOT NULL DEFAULT 0
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hook_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT,
               kind TEXT NOT NULL,
               detail TEXT,
               created REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS compaction_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT,
               reason TEXT,
               snapshot TEXT,
               created REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alert_actions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL,
               action TEXT NOT NULL,
               status TEXT,
               created REAL NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _log_hook(session_id, kind, detail):
    conn = _db()
    conn.execute(
        "INSERT INTO hook_log (session_id, kind, detail, created) VALUES (?, ?, ?, ?)",
        (session_id or "", kind, detail, time.time()),
    )
    conn.commit()


def hook_events(session_id=None):
    """Missed-hook and provider-error log. ``session_id`` None returns every row."""
    conn = _db()
    if session_id is None:
        rows = conn.execute(
            "SELECT session_id, kind, detail, created FROM hook_log ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT session_id, kind, detail, created FROM hook_log "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [
        {"session_id": sid, "kind": kind, "detail": detail, "created": created}
        for sid, kind, detail, created in rows
    ]


def _usage_row(conn, session_id):
    return conn.execute(
        """SELECT chars_in, turns, model, first_seen, last_seen,
                  anchor_valid, window_unresolved, detected_window, provider_tokens,
                  direct_chars, notified_status, system_chars, tools_chars,
                  skills_chars, conversation_chars
           FROM usage WHERE session_id = ?""",
        (session_id,),
    ).fetchone()


def _as_usage(row):
    if row is None:
        return None
    keys = (
        "chars_in", "turns", "model", "first_seen", "last_seen",
        "anchor_valid", "window_unresolved", "detected_window", "provider_tokens",
        "direct_chars", "notified_status", "system_chars", "tools_chars",
        "skills_chars", "conversation_chars",
    )
    return dict(zip(keys, row))


def _context_length_from(kwargs):
    for key in ("context_length", "model_context_length", "context_window", "max_context_tokens"):
        value = kwargs.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    usage = kwargs.get("usage")
    if isinstance(usage, dict):
        for key in ("context_length", "context_window", "max_context_tokens"):
            if usage.get(key):
                try:
                    return int(usage[key])
                except (TypeError, ValueError):
                    continue
    return None


def _prompt_tokens(usage):
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens", "total_tokens"):
        if usage.get(key) is not None:
            try:
                return max(int(usage[key]), 0)
            except (TypeError, ValueError):
                continue
    return None


def record_llm_call(session_id, chars, model=None, **extra):
    """Accumulate usage per session.

    ``confirm_window=False`` marks the max unknown (em dash) unless a real
    ``context_length`` or a per-session override is already stored.
    ``replace=True`` drops the previous anchor instead of adding to it.
    ``reply_only=True`` says ``chars`` is the size of a reply, not of the context.
    Such an update cannot re-anchor a gauge that a summary cleared.
    """
    if not session_id:
        return
    try:
        incoming = int(chars or 0)
    except (TypeError, ValueError):
        incoming = 0
    if incoming < 0:
        incoming = 0
    now = time.time()
    conn = _db()
    current = _as_usage(_usage_row(conn, session_id))
    anchor_lost = current is not None and int(current["anchor_valid"] or 0) == 0
    provider = extra.get("provider_tokens")
    try:
        provider_known = provider is not None and int(provider) > 0
    except (TypeError, ValueError):
        provider_known = False
    replace = bool(extra.get("replace")) or anchor_lost
    touch = extra.get("touch_chars") is not False
    if (
        anchor_lost
        and touch
        and extra.get("reply_only")
        and not extra.get("replace")
        and not extra.get("direct")
        and not provider_known
    ):
        # After a summary the old count is gone. A reply (or half a reply, when a
        # stream breaks) says nothing about how big the context is now. Anchoring on
        # it would pin the gauge near 0% until the next real measurement.
        touch = False

    if (
        current is None
        and not touch
        and extra.get("provider_tokens") is None
        and not extra.get("context_length")
        and extra.get("confirm_window") is not False
    ):
        conn.commit()
        return

    if current is None:
        conn.execute(
            """INSERT INTO usage (session_id, chars_in, turns, model, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, incoming if touch else 0, 1 if touch else 0, model, now, now),
        )
    elif not touch:
        if model:
            conn.execute(
                "UPDATE usage SET model = ?, last_seen = ? WHERE session_id = ?",
                (model, now, session_id),
            )
        else:
            conn.execute(
                "UPDATE usage SET last_seen = ? WHERE session_id = ?",
                (now, session_id),
            )
    elif replace:
        conn.execute(
            """UPDATE usage SET
                   chars_in = ?,
                   turns = turns + 1,
                   model = COALESCE(?, model),
                   last_seen = ?,
                   anchor_valid = 1
               WHERE session_id = ?""",
            (incoming, model, now, session_id),
        )
    else:
        conn.execute(
            """UPDATE usage SET
                   chars_in = chars_in + ?,
                   turns = turns + 1,
                   model = COALESCE(?, model),
                   last_seen = ?
               WHERE session_id = ?""",
            (incoming, model, now, session_id),
        )

    sets = []
    values = []
    context_length = extra.get("context_length")
    if context_length:
        sets.append("detected_window = ?")
        values.append(int(context_length))
        sets.append("window_unresolved = 0")
    elif extra.get("confirm_window") is False:
        session_override = conn.execute(
            "SELECT window_tokens FROM session_windows WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        detected = current["detected_window"] if current else None
        if not session_override and not detected and not context_length:
            sets.append("window_unresolved = 1")

    if extra.get("provider_tokens") is not None:
        sets.append("provider_tokens = ?")
        values.append(int(extra["provider_tokens"]))
        if provider_known:
            # The provider just counted the whole prompt: that is a measurement.
            # It re-anchors a gauge a summary had cleared, without waiting for a turn to end.
            sets.append("anchor_valid = 1")

    if extra.get("direct"):
        sets.append("direct_chars = ?")
        values.append(incoming if touch or extra.get("replace") else incoming)
    elif touch and extra.get("direct") is not True and current and current.get("direct_chars"):
        # A later reply-sized update returns to the mirror estimate.
        if not extra.get("keep_direct"):
            sets.append("direct_chars = NULL")

    breakdown = extra.get("breakdown")
    if isinstance(breakdown, dict):
        for column, key in (
            ("system_chars", "system_prompt"),
            ("tools_chars", "tools"),
            ("skills_chars", "skills"),
            ("conversation_chars", "conversation"),
        ):
            sets.append(f"{column} = ?")
            try:
                values.append(max(int(breakdown.get(key) or 0), 0))
            except (TypeError, ValueError):
                values.append(0)

    if sets:
        values.append(session_id)
        conn.execute(
            f"UPDATE usage SET {', '.join(sets)} WHERE session_id = ?",
            values,
        )

    agent_id = extra.get("agent_id")
    if agent_id:
        detected = int(context_length) if context_length else None
        direct = incoming if extra.get("direct") else None
        existing = conn.execute(
            "SELECT chars_in FROM agents WHERE session_id = ? AND agent_id = ?",
            (session_id, agent_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO agents
                   (session_id, agent_id, model, chars_in, detected_window, direct_chars)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, agent_id, model, incoming if touch else 0, detected, direct),
            )
        else:
            agent_chars = incoming if replace else existing[0] + (incoming if touch else 0)
            conn.execute(
                """UPDATE agents SET
                       chars_in = ?,
                       model = COALESCE(?, model),
                       detected_window = COALESCE(?, detected_window),
                       direct_chars = COALESCE(?, direct_chars)
                   WHERE session_id = ? AND agent_id = ?""",
                (agent_chars, model, detected, direct, session_id, agent_id),
            )
    conn.commit()


def set_degradation(session_or_model, factor):
    """Store the reliable-window factor for a session id or a model id.

    ``factor`` None, or the string ``reset``, deletes the row so config and
    the built-in tiers apply again. Numbers are clamped to 0.05–1.0.
    A session row is consulted before a model row.
    """
    target = str(session_or_model or "").strip()
    if not target:
        return {"error": "session or model is required"}
    conn = _db()
    if factor is None or (
        isinstance(factor, str) and factor.strip().lower() in ("", "reset", "clear", "none")
    ):
        conn.execute("DELETE FROM model_degradation WHERE target = ?", (target,))
        conn.commit()
        return {"target": target, "factor": None, "reset": True}
    clamped = gauge_math.clamp_factor(factor)
    if clamped is None:
        return {"error": "degrade must be a number"}
    conn.execute(
        """INSERT INTO model_degradation (target, factor) VALUES (?, ?)
           ON CONFLICT(target) DO UPDATE SET factor = excluded.factor""",
        (target, clamped),
    )
    conn.commit()
    return {"target": target, "factor": clamped, "reset": False}


def _stored_degrade(conn, session_id, model):
    """Session target wins over a model target. Model ids are case-insensitive."""
    if session_id:
        row = conn.execute(
            "SELECT factor FROM model_degradation WHERE target = ?",
            (session_id,),
        ).fetchone()
        if row is not None:
            return row[0]
    if not model:
        return None
    row = conn.execute(
        "SELECT factor FROM model_degradation WHERE lower(target) = lower(?)",
        (str(model),),
    ).fetchone()
    return row[0] if row else None


def _limiter_model(agents, fallback):
    """Model that owns the smallest known window, else ``fallback``."""
    if not agents or len(agents) < 2:
        return fallback
    known = []
    for agent in agents:
        window = agent.get("window")
        if window is None:
            window = agent.get("window_tokens")
        if isinstance(window, bool) or not isinstance(window, (int, float)):
            continue
        known.append((window, agent.get("model")))
    if not known:
        return fallback
    return min(known, key=lambda item: item[0])[1] or fallback


def _applied_degrade(conn, session_id, model, config, agents=None):
    """Factor in force, the model it was resolved from, and any stored override."""
    chosen = _limiter_model(agents, model)
    stored = _stored_degrade(conn, session_id, chosen)
    if stored is not None:
        clamped = gauge_math.clamp_factor(stored)
        if clamped is not None:
            return clamped, chosen, stored
    return gauge_math.degradation_factor(chosen, config), chosen, None


def _model_override(conn, model):
    if not model:
        return None
    row = conn.execute(
        "SELECT window_tokens FROM windows WHERE model = ?",
        (model,),
    ).fetchone()
    return row[0] if row else None


def _session_override(conn, session_id):
    if not session_id:
        return None
    row = conn.execute(
        "SELECT window_tokens FROM session_windows WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0] if row else None


def _resolve(conn, session_id, model, detected=None, unresolved=False):
    config = load_config()
    nominal, source = gauge_math.resolve_nominal(
        model,
        session_override=_session_override(conn, session_id),
        detected=detected,
        model_override=_model_override(conn, model),
        config=config,
        unresolved=unresolved,
    )
    return nominal, source, config


def _thresholds(config):
    """Watch and full ratios. A pair that is not 0 < watch < full ≤ 1 uses the defaults."""
    thresholds = (config or {}).get("thresholds") or {}
    try:
        watch = float(thresholds.get("watch", gauge_math.DEFAULT_WATCH))
        full = float(thresholds.get("full", gauge_math.DEFAULT_FULL))
    except (TypeError, ValueError):
        return gauge_math.DEFAULT_WATCH, gauge_math.DEFAULT_FULL
    if not (0.0 < watch < full <= 1.0):
        return gauge_math.DEFAULT_WATCH, gauge_math.DEFAULT_FULL
    return watch, full


def _agent_rows(conn, session_id):
    return conn.execute(
        """SELECT agent_id, model, chars_in, detected_window, direct_chars
           FROM agents WHERE session_id = ? ORDER BY agent_id""",
        (session_id,),
    ).fetchall()


def _estimate_from_usage(usage, cpt=None):
    if usage.get("provider_tokens") is not None:
        return max(int(usage["provider_tokens"]), 0), False, True
    if usage.get("direct_chars") is not None:
        return gauge_math.estimate_tokens(
            usage["direct_chars"], direct=True, chars_per_token=cpt
        ), True, True
    return gauge_math.estimate_tokens(usage.get("chars_in") or 0, chars_per_token=cpt), True, False


def _basis(usage):
    """Where the count comes from: the provider, a full transcript, or an estimate."""
    if not usage:
        return "estimate"
    if usage.get("provider_tokens") is not None:
        return "provider"
    if usage.get("direct_chars") is not None:
        return "transcript"
    return "estimate"


def _char_parts(usage):
    return {
        "system_prompt": usage.get("system_chars") or 0,
        "tools": usage.get("tools_chars") or 0,
        "skills": usage.get("skills_chars") or 0,
        "conversation": usage.get("conversation_chars") or 0,
    }


def _recommendation(status, remaining_tokens):
    if status == "FULL":
        return ("Context almost exhausted. Summarize what matters, then start "
                "a fresh session (/new) and paste the summary.")
    if status == "WATCH":
        return (f"About {remaining_tokens // 1000}k tokens of headroom left. "
                "Finish the current topic first; avoid starting anything big.")
    if status == "UNKNOWN":
        return ("Context window is not confirmed yet, so this gauge shows an "
                "em dash instead of a percentage.")
    return "Plenty of headroom. Carry on."


_ALERT_COLUMNS = {"WATCH": "watch_alerted_at", "FULL": "full_alerted_at"}


def bind_context(ctx):
    """Remember the PluginContext so a hook can post a notification later."""
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx


# Surfaces that are not a messaging platform: ``home`` has nothing to resolve there.
_LOCAL_SURFACES = frozenset({
    "", "cli", "tui", "desktop", "acp", "local", "api_server", "webhook", "cron", "subagent",
})


def _session_surface():
    """Platform of the session that is bound to this call, lowercased. Never raises."""
    ctx = _PLUGIN_CTX
    getter = getattr(ctx, "session_surface", None)
    if getter is None:
        return ""
    try:
        return str(getter() or "").strip().lower()
    except Exception:
        return ""


def _route_target(route):
    """``(parsed_route, hermes_send_target, fallback_reason)`` for an alert route.

    ``session`` has no target: the alert posts into the session's own thread.
    ``home`` follows the platform the session lives on, because a plugin cannot
    know which home channel is meant on a multi-platform setup. A session on a
    local surface has no such platform, so it stays on ``session``.
    """
    parsed = gauge_math.parse_route(route) or gauge_math.parse_route(gauge_math.ROUTE_SESSION)
    if parsed["kind"] == "session":
        return parsed, None, None
    if parsed["kind"] == "custom" or parsed["platform"]:
        return parsed, parsed["target"], None
    surface = _session_surface()
    if surface and surface not in _LOCAL_SURFACES:
        return parsed, surface, None
    return parsed, None, "no_home_platform"


def _hermes_send(target, message):
    """``hermes send --to target message``, the documented way to reach a chosen chat.

    Returns True when it exits 0. Never raises.
    """
    exe = os.environ.get("HERMES_BIN") or shutil.which("hermes")
    if not exe:
        return False
    try:
        done = subprocess.run(
            [exe, "send", "--quiet", "--to", target, message],
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _send_routed(target, message):
    """Hand a routed alert to ``hermes send`` without holding up the turn."""
    sender = _ROUTE_SENDER
    if sender is not None:
        return bool(sender(target, message))

    def run():
        if not _hermes_send(target, message):
            _LOG.warning("hermes-gauge: alert to %s was not delivered", target)

    threading.Thread(target=run, name="hermes-gauge-route", daemon=True).start()
    return True


def emit_hermes_notification(message, session_id=None, route=None):
    """Deliver one short alert.

    ``route`` is ``alerts.route``. ``session`` (the default) posts on the session
    Hermes already bound: ``PluginContext.post_message`` is the public seam, it
    publishes ``notification.show`` on the local surface and posts into the bound
    chat on a messaging adapter. A plugin cannot name another chat through it, so
    ``home`` and ``custom:<platform>:<chat_id>`` go through ``hermes send``. When a
    route cannot be resolved the alert stays on the session instead of vanishing.
    ``_ALERT_SINK`` replaces both in tests. Never raises.
    """
    parsed, target, fallback = _route_target(route)
    payload = {"message": message, "session_id": session_id}
    if parsed["kind"] != "session":
        payload["route"] = parsed["value"]
        payload["target"] = target
        if fallback:
            payload["fallback"] = fallback
    sink = _ALERT_SINK
    if sink is not None:
        sink(payload)
        return {"sent": True, "channel": "sink", "session_id": session_id}
    if target is not None:
        if _send_routed(target, message):
            return {
                "sent": True,
                "channel": "hermes_send",
                "route": parsed["value"],
                "target": target,
                "session_id": session_id,
            }
    ctx = _PLUGIN_CTX
    if ctx is None or not hasattr(ctx, "post_message"):
        return {"sent": False, "channel": "none", "session_id": session_id}
    try:
        handle = ctx.post_message(message)
    except Exception as exc:
        return {
            "sent": False,
            "channel": "post_message",
            "error": type(exc).__name__,
            "session_id": session_id,
        }
    if isinstance(handle, dict):
        ok = bool(handle.get("ok"))
    else:
        ok = bool(getattr(handle, "ok", False))
    return {"sent": ok, "channel": "post_message", "session_id": session_id}


# Telegram's CallbackQueryHandler only dispatches its own prefixes (mp/cp/gt/ea/sc/cl/
# update_prompt). A gauge: callback is not delivered to the agent. Alerts stay text
# and this plugin does not poll Telegram; gauge_action runs the reply.
_ACTION_COMMANDS = {
    "compact_new": ["/compact", "/new"],
    "compact": ["/compact"],
    "new": ["/new"],
    "dismiss": [],
}
_ACTION_ALIASES = {
    "1": "compact_new",
    "compact+new": "compact_new",
    "compact_new": "compact_new",
    "compact-new": "compact_new",
    "compactnew": "compact_new",
    "2": "compact",
    "compact": "compact",
    "/compact": "compact",
    "compress": "compact",
    "/compress": "compact",
    "3": "new",
    "new": "new",
    "/new": "new",
    "new_session": "new",
    "4": "dismiss",
    "0": "dismiss",
    "rien": "dismiss",
    "nothing": "dismiss",
    "dismiss": "dismiss",
}


def _normalize_action(action):
    text = str(action or "").strip().lower().replace(" ", "")
    return _ACTION_ALIASES.get(text)


def _active_language(explicit=None):
    """``en`` or ``fr`` from an explicit word, else the saved preference."""
    try:
        choice = (load_config() or {}).get("language")
    except Exception:
        choice = "auto"
    return _sibling("prefs").resolve_language(choice, explicit)


def _say(english, french):
    """English unless the language setting resolves to French."""
    return french if _active_language() == "fr" else english


def _frozen_after_s(config=None):
    config = config if config is not None else load_config()
    try:
        hours = float((config or {}).get("frozen_after_hours", 72))
    except (TypeError, ValueError):
        hours = 72
    if hours < 1:
        hours = 1
    return hours * 3600


def _session_label(session_id, lang):
    meta, _known = _state_meta([session_id])
    return _sibling("sessions").display_name(session_id, meta.get(session_id), lang=lang)


_ALERT_TEXT = {
    "en": {
        "full": (
            '"{who}" is {pct}% full. Quality drops from here. '
            "Ask for a summary, then send /new and paste it. "
            "Hermes Gauge only watches; it never changes your chat."
        ),
        "watch": (
            '"{who}" is {pct}% full: room for a bit more, not for anything big. '
            "Wrap up this topic, or ask for a summary before starting a new one. "
            "Hermes Gauge only watches; it never changes your chat."
        ),
        "home": "Sent here because no home channel was found.",
        "summarized": 'Hermes just summarized "{who}". Older details may be gone.',
        "next": {
            "OK": "Plenty of room. Carry on.",
            "WATCH": "Wrap up this topic before starting a new one.",
            "FULL": "Ask for a summary, then send /new and paste it.",
            "UNKNOWN": "The window is unknown. Set it with /gauge window <tokens>.",
            "SUMMARIZED": "Just summarized — measuring again.",
            "NO DATA": "Send your first message.",
        },
    },
    "fr": {
        "full": (
            "« {who} » est pleine à {pct} %. La qualité baisse à partir d'ici. "
            "Demande un résumé, puis envoie /new et colle-le. "
            "Hermes Gauge ne fait qu'observer ; elle ne modifie jamais ta conversation."
        ),
        "watch": (
            "« {who} » est pleine à {pct} % : encore un peu de place, pas pour un gros sujet. "
            "Termine ce sujet, ou demande un résumé avant d'en ouvrir un autre. "
            "Hermes Gauge ne fait qu'observer ; elle ne modifie jamais ta conversation."
        ),
        "home": "Envoyé ici : aucun canal d'accueil trouvé.",
        "summarized": "Hermes vient de résumer « {who} ». Les détails plus anciens peuvent avoir disparu.",
        "next": {
            "OK": "Encore de la place. Continue.",
            "WATCH": "Termine ce sujet avant d'en ouvrir un autre.",
            "FULL": "Demande un résumé, puis envoie /new et colle-le.",
            "UNKNOWN": "La fenêtre est inconnue. Fixe-la avec /gauge window <jetons>.",
            "SUMMARIZED": "Vient d'être résumé — nouvelle mesure.",
            "NO DATA": "Envoie ton premier message.",
        },
    },
}


def _alert_copy(lang):
    return _ALERT_TEXT["fr"] if lang == "fr" else _ALERT_TEXT["en"]


def _alert_message(session_id, fields, surface="cli"):
    """One plain sentence. No tool name. The title of the conversation, not its id."""
    label = gauge_math.percent_label(fields.get("fill_percent"))
    status = fields.get("status")
    if not label or status not in _ALERT_COLUMNS:
        return None
    lang = _active_language()
    who = _session_label(session_id, lang)
    key = "full" if status == "FULL" else "watch"
    return _alert_copy(lang)[key].format(who=who, pct=label)


def _alert_dismissed(conn, session_id, status):
    """True when the latest choice for this session dismissed this same status."""
    try:
        row = conn.execute(
            "SELECT action, status FROM alert_actions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    return row[0] == "dismiss" and row[1] == status


def _record_alert_action(conn, session_id, action, status):
    conn.execute(
        "INSERT INTO alert_actions (session_id, action, status, created) VALUES (?, ?, ?, ?)",
        (session_id, action, status, time.time()),
    )
    conn.commit()


def _handoff_commands(session_id, commands):
    """Ask Hermes to run slash commands. The plugin does not summarize or reset itself.

    ``_HERMES_COMMAND`` replaces the call in tests. Otherwise ``inject_message`` queues
    the command on the bound session. Returns False when nothing accepted it.
    """
    runner = _HERMES_COMMAND
    try:
        if runner is not None:
            return bool(runner(session_id, list(commands)))
        ctx = _PLUGIN_CTX
        inject = getattr(ctx, "inject_message", None) if ctx is not None else None
        if not callable(inject):
            return False
        for command in commands:
            try:
                accepted = inject(command, session_key=session_id)
            except TypeError:
                accepted = inject(command)
            if not accepted:
                return False
        return True
    except Exception:
        return False


def _gauge_action(action=None, session_id=None, **kwargs):
    """Record a reply to an alert and, when asked, hand ``/compact`` / ``/new`` to Hermes."""
    if isinstance(action, dict):
        kwargs = {**action, **kwargs}
        action = kwargs.get("action")
        session_id = kwargs.get("session_id", session_id)
    action = kwargs.get("action", action)
    session_id = kwargs.get("session_id", session_id)
    name = _normalize_action(action)
    if name is None:
        return {
            "ok": False,
            "error": "action must be compact, new, compact+new, or dismiss",
            "session_id": session_id,
        }
    if session_id is None or not str(session_id).strip():
        return {"ok": False, "error": "session_id is required", "action": name}
    session_id = str(session_id).strip()
    conn = _db()
    if conn.execute(
        "SELECT 1 FROM usage WHERE session_id = ?", (session_id,)
    ).fetchone() is None:
        return {"ok": False, "error": "unknown session", "action": name, "session_id": session_id}
    fields, _usage = _reading(conn, session_id)
    status = (fields or {}).get("status")
    commands = list(_ACTION_COMMANDS[name])
    _record_alert_action(conn, session_id, name, status)
    if name == "dismiss":
        return {
            "ok": True,
            "action": "dismiss",
            "session_id": session_id,
            "status": status,
            "fill_percent": (fields or {}).get("fill_percent"),
            "commands": [],
            "executed": True,
            "detail": "Alert dismissed. No /compact or /new was sent.",
        }
    executed = _handoff_commands(session_id, commands)
    typed = _say(" then ", " puis ").join(commands)
    if executed:
        detail = f"Hermes accepted {typed}."
    else:
        detail = f"Hermes did not take the command. Tell the user to run: {typed}."
    return {
        "ok": True,
        "action": name,
        "session_id": session_id,
        "status": status,
        "fill_percent": (fields or {}).get("fill_percent"),
        "commands": commands,
        "executed": executed,
        "detail": detail,
    }


def gauge_action(*args, action=None, session_id=None, **kwargs):
    """Public tool entry — a JSON string, which the Hermes runtime accepts."""
    if args and action is None:
        action = args[0]
    if len(args) > 1 and session_id is None and not isinstance(args[0], dict):
        session_id = args[1]
    result = _gauge_action(action, session_id=session_id, **kwargs)
    return json.dumps(result, ensure_ascii=False)


def _claim_alert(conn, session_id, fields):
    """``(message, release, route)`` for a WATCH/FULL alert, or None. Nothing is sent here.

    The cooldown stamp is claimed with one conditional ``UPDATE`` so two hooks (or
    two processes) cannot both pass the check. ``release`` puts the old stamp back
    when the send fails, so the next turn tries again.
    """
    settings = gauge_math.alert_settings(load_config())
    status = fields.get("status")
    if not settings["enabled"] or status not in settings["on"]:
        return None
    column = _ALERT_COLUMNS.get(status)
    message = _alert_message(session_id, fields)
    if column is None or message is None:
        return None
    _parsed, _target, fallback = _route_target(settings["route"])
    if fallback == "no_home_platform":
        message = f"{message} {_alert_copy(_active_language())['home']}"
    if _alert_dismissed(conn, session_id, status):
        return None
    row = conn.execute(
        f"SELECT {column} FROM usage WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    last = row[0] if row else None
    now = time.time()
    window = float(settings["cooldown_minutes"]) * 60.0
    claimed = conn.execute(
        f"UPDATE usage SET {column} = ? WHERE session_id = ? "
        f"AND ({column} IS NULL OR ? <= 0 OR (? - {column}) >= ?)",
        (now, session_id, window, now, window),
    ).rowcount
    conn.commit()
    if not claimed:
        return None

    def release():
        conn.execute(
            f"UPDATE usage SET {column} = ? WHERE session_id = ? AND {column} = ?",
            (last, session_id, now),
        )
        conn.commit()

    return message, release, settings["route"]


def _claim_policy(conn, session_id, fields):
    """``(message, release, route)`` for the dull recommendation, or None. Nothing is sent here.

    This never compacts, never rotates the session, and never calls Hermes
    core. ``audit`` and ``act`` differ only in the wording of the push.
    The latch and the cooldown are claimed with conditional ``UPDATE``s, so a
    crossing is pushed once even when two hooks run it at the same time.
    """
    verdict = fields.get("policy_verdict")
    if not isinstance(verdict, dict) or not session_id:
        return None
    config = load_config()
    policy = gauge_math.resolve_policy(config)
    row = conn.execute(
        "SELECT dull_latched, dull_alerted_at FROM usage WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    latched = bool(row[0])
    last = row[1]
    if not verdict.get("dull_reached"):
        if latched:
            conn.execute(
                "UPDATE usage SET dull_latched = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
        return None
    if latched:
        return None
    now = time.time()
    window = float(policy["cooldown_minutes"]) * 60.0
    claimed = conn.execute(
        """UPDATE usage SET dull_latched = 1, dull_alerted_at = ?
           WHERE session_id = ? AND dull_latched = 0
             AND (dull_alerted_at IS NULL OR ? <= 0 OR (? - dull_alerted_at) >= ?)""",
        (now, session_id, window, now, window),
    ).rowcount
    if not claimed:
        # Inside the cooldown: latch quietly so this crossing is not pushed later.
        conn.execute(
            "UPDATE usage SET dull_latched = 1 WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return None
    conn.commit()
    lang = _active_language()
    message = gauge_math.policy_push_message(
        _session_label(session_id, lang), verdict, policy, lang
    )

    def release():
        conn.execute(
            "UPDATE usage SET dull_latched = 0, dull_alerted_at = ? "
            "WHERE session_id = ? AND dull_alerted_at = ?",
            (last, session_id, now),
        )
        conn.commit()

    return message, release, gauge_math.alert_settings(config)["route"]


def _join_pushes(messages):
    """One Telegram bubble for everything a single turn has to say."""
    return "\n\n".join(message for message in messages if message)


def _push_turn(conn, session_id, fields, alert=True):
    """Send at most one message for this turn: the status alert and the dull push together.

    A turn that crosses WATCH on a ``flash`` model used to post both, and each
    twice when two hooks raced. Now both are claimed first, then sent as one.
    Returns the message, or None.
    """
    claims = []
    if alert:
        claim = _claim_alert(conn, session_id, fields)
        if claim:
            claims.append(claim)
    # Independent of the WATCH/FULL alert: one dull push per crossing.
    policy = _claim_policy(conn, session_id, fields)
    if policy:
        claims.append(policy)
    if not claims:
        return None
    message = _join_pushes([item[0] for item in claims])
    route = claims[0][2]
    result = emit_hermes_notification(message, session_id=session_id, route=route)
    if not result or not result.get("sent"):
        detail = "" if not isinstance(result, dict) else str(result.get("error") or result.get("channel") or "")
        _log_hook(session_id, "alert_failed", detail[:300])
        for _message, release, _route in claims:
            release()
        return None
    return message


def _after_turn(session_id):
    """A hook just wrote this active session. Push if the status crossed."""
    if not session_id:
        return
    try:
        conn = _db()
        fields, usage = _reading(conn, session_id)
        if usage is None:
            return
        _finish(conn, session_id, fields, usage, notify=True)
    except Exception as exc:
        try:
            _log_hook(session_id, "alert_error", str(exc)[:300])
        except Exception:
            return


def _notify(conn, session_id, status, previous):
    """Return the crossing message once. The status flip is claimed atomically.

    Two hooks can read the same ``previous`` (``post_api_request`` and
    ``post_llm_call`` land on one turn; the gateway and a CLI can share the
    database). Only the caller whose ``UPDATE`` changes the row owns the crossing.
    """
    order = {"OK": 0, "WATCH": 1, "FULL": 2}
    if status not in order:
        return None
    prev = previous if previous in order else "OK"
    message = None
    if order[status] > order[prev] and status in ("WATCH", "FULL"):
        if status == "WATCH":
            message = ("Context crossed into WATCH. Finish the current topic "
                       "before starting something new.")
        else:
            message = "Context crossed into FULL. Summarize and start a fresh session."
    if status != previous:
        claimed = conn.execute(
            "UPDATE usage SET notified_status = ? "
            "WHERE session_id = ? AND COALESCE(notified_status, '') = ?",
            (status, session_id, previous or ""),
        ).rowcount
        conn.commit()
        if not claimed:
            return None
    return message


def _subject_agents(conn, session_id, cpt=None):
    rows = _agent_rows(conn, session_id)
    if len(rows) < 2:
        return None
    config = load_config()
    cpt = cpt or gauge_math.chars_per_token_from(config)
    reserve = config.get("output_reserve") or 0
    described = []
    for agent_id, model, chars, detected, direct in rows:
        if direct is not None:
            estimated = gauge_math.estimate_tokens(direct, direct=True, chars_per_token=cpt)
            approximate = True
        else:
            estimated = gauge_math.estimate_tokens(chars, chars_per_token=cpt)
            approximate = True
        nominal, source, _config = _resolve(
            conn, None, model, detected=detected, unresolved=not detected
        )
        # Per-agent resolution must not inherit another session's override.
        # ``_resolve(..., session_id=None)`` skips the session table.
        usable = gauge_math.effective_window(nominal, reserve) if source != "unresolved" else None
        if source == "unresolved":
            usable = None
        described.append({
            "agent_id": agent_id,
            "model": model or "unknown",
            "estimated_tokens": estimated,
            "window_tokens": usable,
            "window_source": source if usable is not None else "unresolved",
            "window": usable,
            "source": source if usable is not None else "unresolved",
            "approximate": approximate,
        })
    return described


def _reading(conn, session_id, model_hint=None, notify=False):
    usage = _as_usage(_usage_row(conn, session_id))
    config = load_config()
    cpt = gauge_math.chars_per_token_from(config)
    agents = _subject_agents(conn, session_id, cpt)
    watch, full = _thresholds(config)
    reserve = config.get("output_reserve") or 0

    if agents:
        link = gauge_math.weakest_link(agents)
        estimated = sum(agent["estimated_tokens"] for agent in agents)
        anchor_valid = True if usage is None else bool(usage.get("anchor_valid", 1))
        unresolved = usage is not None and bool(usage.get("window_unresolved"))
        if not anchor_valid or unresolved or link["window"] is None:
            fields = gauge_math.compose(
                estimated, None, reserve, "unresolved", False, watch, full
            )
        else:
            degrade, _chosen, _stored = _applied_degrade(
                conn, session_id, (agents[0] or {}).get("model"), config, agents=agents
            )
            fields = gauge_math.compose(
                estimated,
                link["window"],
                0,  # already an effective window
                link["source"],
                True,
                watch,
                full,
                approximate=True,
                degrade=degrade,
            )
            # compose() deducts reserve again if we pass nominal. We passed
            # the effective window and reserve 0 so the weakest link is exact.
        fields["window_label"] = link["label"]
        fields["agents"] = [
            {key: agent[key] for key in (
                "agent_id", "model", "estimated_tokens", "window_tokens", "window_source"
            )}
            for agent in agents
        ]
        model = model_hint or (usage["model"] if usage else None) or agents[0]["model"]
        turns = usage["turns"] if usage else 0
        parts_source = usage or {}
        direct = bool(parts_source.get("direct_chars")) if usage else False
        fields["breakdown"] = gauge_math.breakdown_tokens(
            _char_parts(parts_source) if usage else {},
            fields["estimated_tokens"],
            direct=direct,
            chars_per_token=cpt,
        )
        fields["model"] = model or "unknown"
        fields["turns"] = turns
        fields["session_id"] = session_id
        return fields, usage

    if usage is None:
        nominal, source, config = _resolve(conn, session_id, model_hint, unresolved=False)
        degrade, _chosen, _stored = _applied_degrade(conn, session_id, model_hint, config)
        fields = gauge_math.compose(
            0, nominal, config.get("output_reserve") or 0, source, True, watch, full,
            degrade=degrade,
        )
        fields.update({
            "session_id": session_id,
            "model": model_hint or "unknown",
            "turns": 0,
            "window_label": None,
            "agents": [],
            "breakdown": gauge_math.breakdown_tokens({}, 0),
            "notification": None,
            "in_flight": None,
        })
        fields["recommendation"] = _recommendation(fields["status"], fields["remaining_tokens"] or 0)
        return fields, None

    estimated, approximate, direct = _estimate_from_usage(usage, cpt)
    detected = usage.get("detected_window")
    unresolved = bool(usage.get("window_unresolved")) and not detected and not _session_override(conn, session_id)
    model = model_hint or usage.get("model") or "unknown"
    nominal, source, config = _resolve(
        conn, session_id, model, detected=detected, unresolved=unresolved
    )
    anchor_valid = bool(usage.get("anchor_valid", 1))
    degrade, _chosen, _stored = _applied_degrade(conn, session_id, model, config)
    fields = gauge_math.compose(
        estimated,
        nominal,
        config.get("output_reserve") or 0,
        source,
        anchor_valid,
        watch,
        full,
        approximate=approximate,
        degrade=degrade,
    )
    fields["breakdown"] = gauge_math.breakdown_tokens(
        _char_parts(usage), fields["estimated_tokens"] or 0, direct=direct,
        chars_per_token=cpt,
    )
    fields["model"] = model
    fields["turns"] = usage["turns"]
    fields["session_id"] = session_id
    fields["window_label"] = None
    fields["agents"] = []
    return fields, usage


def _finish(conn, session_id, fields, usage, notify):
    status = fields["status"]
    remaining = fields["remaining_tokens"]
    fields["recommendation"] = _recommendation(status, remaining or 0)
    config = load_config()
    fields["policy_verdict"] = gauge_math.policy_verdict(
        fields.get("model"), fields.get("fill_percent"), config,
        _sibling("prefs").resolve_language((config or {}).get("language")),
    )
    fields["notification"] = None
    fields["in_flight"] = None
    fields["basis"] = _basis(usage)
    if status == "WATCH":
        fields["in_flight"] = {
            "note": "Compaction is close. This is what is in the window right now.",
            "model": fields.get("model"),
            "turns": fields.get("turns"),
            "estimated_tokens": fields.get("estimated_tokens"),
            "breakdown": fields.get("breakdown"),
        }
    if notify and usage is not None and session_id:
        previous = usage.get("notified_status")
        message = _notify(conn, session_id, status, previous)
        fields["notification"] = message
        _push_turn(conn, session_id, fields, alert=bool(message))
        if message and status == "WATCH":
            conn.execute(
                """INSERT INTO compaction_log (session_id, reason, snapshot, created)
                   VALUES (?, ?, ?, ?)""",
                (session_id, "watch", json.dumps(fields["in_flight"]), time.time()),
            )
            conn.commit()
    return fields


def flush_pending(session_id=None):
    """Commit stream buffers the end hook never closed. Logs a missed hook."""
    keys = [key for key in list(_BUFFERS) if session_id is None or key[0] == session_id]
    committed = []
    for key in keys:
        buf = _BUFFERS.pop(key, None)
        if not buf:
            continue
        sid, turn_id = key
        text = buf.get("text") or ""
        _log_hook(sid, "missed", "gauge_check flushed a stream the end hook never closed")
        _commit_turn(
            sid,
            turn_id,
            len(text),
            model=buf.get("model"),
            context_length=buf.get("context_length"),
            confirm_window=bool(buf.get("context_length")),
        )
        committed.append(sid)
    return committed


def _gauge_check(*args, session_id=None, model=None, conversation_history=None, content=None,
                recalculate=False, include_factors=False, **kwargs):
    # Hermes dispatcher may pass the injected session_id positionally AND in
    # kwargs; accept either, ignore stray positional payloads.
    for a in args:
        if isinstance(a, str) and a and not session_id:
            session_id = a
        elif isinstance(a, dict):
            for k in ("session_id", "model", "conversation_history", "content",
                      "recalculate", "include_factors"):
                if k not in kwargs and k in a:
                    kwargs[k] = a[k]
            if not session_id:
                session_id = a.get("session_id")
    if not model:
        model = kwargs.get("model")
    if not include_factors and "include_factors" in kwargs:
        include_factors = kwargs.get("include_factors")
    if conversation_history is None and "conversation_history" in kwargs:
        conversation_history = kwargs.get("conversation_history")
    if content is None and "content" in kwargs:
        content = kwargs.get("content")
    if "recalculate" in kwargs and not recalculate:
        recalculate = kwargs.get("recalculate")
    if not session_id:
        return {"error": "No active session — cannot measure."}
    flush_pending(session_id)
    conn = _db()
    history = conversation_history if conversation_history is not None else kwargs.get("messages")
    usage = _as_usage(_usage_row(conn, session_id))
    need_measure = history is not None or content is not None
    missing = usage is None or not int(usage.get("anchor_valid") or 0)
    if need_measure and (recalculate or missing):
        if missing:
            _log_hook(session_id, "missed", "gauge_check recalculated without a hook update")
        else:
            _log_hook(session_id, "recalc", "gauge_check recalculated on demand")
        if history is not None:
            total, buckets = gauge_math.measure_messages(history)
            record_llm_call(
                session_id,
                total,
                model=model or (usage or {}).get("model"),
                replace=True,
                direct=True,
                breakdown=buckets,
                context_length=_context_length_from(kwargs),
                confirm_window=bool(_context_length_from(kwargs)),
            )
        else:
            record_llm_call(
                session_id,
                len(content or ""),
                model=model,
                replace=True,
                direct=True,
                context_length=_context_length_from(kwargs),
                confirm_window=bool(_context_length_from(kwargs)),
            )
        conn = _db()
    fields, usage = _reading(conn, session_id, model_hint=model, notify=False)
    usage = _as_usage(_usage_row(conn, session_id))
    fields = _finish(conn, session_id, fields, usage, notify=usage is not None)
    if _flag(include_factors):
        config = load_config()
        _factor, chosen, stored = _applied_degrade(
            conn, session_id, fields.get("model"), config, agents=fields.get("agents")
        )
        fields["factors"] = gauge_math.active_factors(chosen, config, stored=stored)
    return fields


def gauge_check(*args, session_id=None, model=None, conversation_history=None, content=None,
                recalculate=False, include_factors=False, **kwargs):
    """Public tool entry point — MUST return a string (runtime rejects dicts)."""
    fields = _gauge_check(
        *args, session_id=session_id, model=model,
        conversation_history=conversation_history, content=content,
        recalculate=recalculate, include_factors=include_factors, **kwargs,
    )
    return json.dumps(fields, ensure_ascii=False, indent=2)


def _state_meta(session_ids):
    """Titles and activity from Hermes' ``state.db``; ``{}`` when it is unreadable."""
    sess = _sibling("sessions")
    conn = sess.open_state(_STATE_DB)
    if conn is None:
        return {}, False
    try:
        return sess.session_meta(conn, session_ids), True
    finally:
        conn.close()


REPORTS_KEEP = 30


def _write_report_file(body, generated_at, prefix="gauge-report", ext="md"):
    """Save a report under ``reports/`` and prune the oldest files of its kind."""
    folder = data_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(generated_at))
    path = folder / f"{prefix}-{stamp}.{ext}"
    path.write_text(body, encoding="utf-8")
    try:
        old = sorted(folder.glob(f"{prefix}-*.{ext}"))[:-REPORTS_KEEP]
        for stale in old:
            stale.unlink()
    except OSError:
        pass
    return path


def _gauge_report(*args, limit=20, format="text", write=False, **kwargs):
    # tolerate positional dispatch: first positional may be the args dict
    for a in args:
        if isinstance(a, dict):
            if "format" in a:
                format = a.get("format", format)
            if "limit" in a:
                limit = a["limit"]
            if "write" in a:
                write = a["write"]
        elif isinstance(a, int) and not isinstance(a, bool):
            limit = a
        elif isinstance(a, str) and a and "format" not in kwargs:
            format = a
    if "format" in kwargs:
        format = kwargs["format"]
    if "limit" in kwargs:
        limit = kwargs["limit"]
    if "write" in kwargs:
        write = kwargs["write"]
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    kind = str(format or "text").strip().lower()
    if kind not in ("text", "md", "json"):
        kind = "text"
    conn = _db()
    rows = conn.execute(
        """SELECT session_id, chars_in, turns, model, first_seen, last_seen
           FROM usage ORDER BY chars_in DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    sessions = []
    for sid, _chars, _turns, _model, first, last in rows:
        fields, _usage = _reading(conn, sid, notify=False)
        sessions.append({
            "session_id": sid,
            "model": fields["model"],
            "estimated_tokens": fields["estimated_tokens"] or 0,
            "fill_percent": fields["fill_percent"] if fields["fill_percent"] is not None else 0.0,
            "fill_display": fields["fill_display"],
            "status": fields["status"] if fields["fill_percent"] is not None else _status_or_ok(fields),
            "window_tokens": fields["window_tokens"],
            "window_source": fields["window_source"],
            "window_label": fields.get("window_label"),
            "turns": fields["turns"],
            "first_seen": first,
            "last_seen": last,
            "approximate": fields["approximate"],
        })
    # Historical report rows stay numeric for a live anchor. An invalidated
    # anchor must not reappear as 0% or as a percentage over 100.
    for session in sessions:
        if session["fill_display"] == gauge_math.FILL_UNKNOWN:
            session["fill_percent"] = None
            session["status"] = "UNKNOWN"
    total_tokens = sum(s["estimated_tokens"] for s in sessions)
    generated_at = time.time()
    # Names, last activity and state come from Hermes' own state.db. Without it
    # the report reads exactly as before, with ids and a last-seen time.
    sess = _sibling("sessions")
    meta, names_known = _state_meta([s["session_id"] for s in sessions])
    for session in sessions:
        info = meta.get(session["session_id"])
        session["title"] = (info or {}).get("title")
        session["name"] = sess.display_name(session["session_id"], info)
        session["source"] = (info or {}).get("source")
        session["thread_id"] = (info or {}).get("thread_id")
        session["last_activity"] = sess.last_activity(info, session["last_seen"])
        session["state"] = sess.classify(info, session["last_seen"], generated_at)
    link = gauge_math.weakest_session(sessions)
    weakest = None
    if link is not None:
        weakest = {
            "session_id": link["session_id"],
            "name": link["name"],
            "model": link["model"],
            "fill_percent": link["fill_percent"],
            "fill_display": link["fill_display"],
            "status": link["status"],
            "turns": link["turns"],
        }
    payload = {
        "summary": gauge_math.summary_line(sessions),
        "weakest_link": weakest,
        "format": kind,
        "sessions": sessions,
        "session_count": len(sessions),
        "by_state": gauge_math.state_counts(sessions),
        "names_from_state_db": names_known,
        "total_estimated_tokens": total_tokens,
        "generated_at": generated_at,
        "written": False,
        "path": None,
    }
    if _flag(write):
        # A report is a deliverable: it goes out as a file, not as a wall of chat text.
        document = gauge_math.render_report_markdown(sessions, generated_at)
        try:
            path = _write_report_file(document, generated_at)
        except OSError as exc:
            payload["write_error"] = f"Could not write the report: {exc}"
        else:
            payload["written"] = True
            payload["path"] = str(path)
            payload["media"] = f"MEDIA:{path}"
            note = (
                f"{payload['summary']}\n\nFull report attached: {path.name}\n{payload['media']}"
            )
            if kind == "text":
                payload["text"] = note
            elif kind == "md":
                payload["markdown"] = note
            return payload
    if kind == "text":
        payload["text"] = gauge_math.render_report_text(sessions, generated_at)
    elif kind == "md":
        payload["markdown"] = gauge_math.render_report_markdown(sessions, generated_at)
    return payload


def gauge_report(*args, limit=20, format="text", write=False, **kwargs):
    """Public tool entry point — MUST return a string (runtime rejects dicts)."""
    payload = _gauge_report(*args, limit=limit, format=format, write=write, **kwargs)
    kind = str(format or "text").strip().lower()
    # A positional dict can override the keyword. Prefer the payload's format.
    if isinstance(payload, dict) and payload.get("format") in ("text", "md", "json"):
        kind = payload["format"]
    if kind == "text" and isinstance(payload, dict) and "text" in payload:
        return payload["text"]
    if kind == "md" and isinstance(payload, dict) and "markdown" in payload:
        return payload["markdown"]
    return json.dumps(payload, ensure_ascii=False, indent=2)


MAX_AUDIT_SESSIONS = 200


def _language(lang):
    return "fr" if str(lang or "").strip().lower().startswith("fr") else "en"


def _known_session_ids(conn, with_hooks=False):
    """Every session the plugin has a trace of, most recently seen first."""
    seen = [r[0] for r in conn.execute("SELECT session_id FROM usage ORDER BY last_seen DESC")]
    logged = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM compaction_log "
            "WHERE session_id IS NOT NULL AND session_id != ''"
        )
    ]
    hooked = []
    if with_hooks:
        hooked = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM hook_log "
                "WHERE session_id IS NOT NULL AND session_id != ''"
            )
        ]
    return list(dict.fromkeys(seen + logged + hooked))[:MAX_AUDIT_SESSIONS]


def _audit_rows(conn, ids, now, cpt, lang="en"):
    """One input row per session for ``audit.build`` and ``incidents.collect``."""
    sess = _sibling("sessions")
    audit = _sibling("audit")
    meta, names_known = _state_meta(ids)
    events = {}
    for event in compaction_events():
        events.setdefault(event["session_id"], []).append(event)
    rows = []
    for sid in ids:
        fields, usage = _reading(conn, sid, notify=False)
        mine = events.get(sid, [])
        # A WATCH snapshot is not a restart: the dated series must keep climbing across it.
        restarts = [e["created"] for e in mine if audit.event_kind(e["reason"]) != audit.KIND_SNAPSHOT]
        info = meta.get(sid)
        rows.append({
            "session_id": sid,
            "name": sess.display_name(sid, info, lang=lang),
            "title": (info or {}).get("title"),
            "state": sess.classify(
                info, (usage or {}).get("last_seen"), now, frozen_after=_frozen_after_s()
            ),
            "last_activity": sess.last_activity(info, (usage or {}).get("last_seen")),
            "model": fields.get("model") if usage else None,
            "reading": fields if usage else None,
            "events": mine,
            "series": sess.turn_series(conn, sid, restarts, chars_per_token=cpt),
        })
    return rows, names_known


def _peaks_audit(session_id=None, limit=10, format="text", lang="en", write=False, **_):
    """Peaks, compactions and composition, per session and overall."""
    audit = _sibling("audit")
    kind = str(format or "text").strip().lower()
    if kind not in ("text", "md", "json"):
        kind = "text"
    lang = _language(lang)
    conn = _db()
    config = load_config()
    watch, full = _thresholds(config)
    cpt = gauge_math.chars_per_token_from(config)
    ids = [session_id] if session_id else _known_session_ids(conn)
    now = time.time()
    rows, names_known = _audit_rows(conn, ids, now, cpt, lang=lang)
    data = audit.build(rows, watch=watch, full=full, now=now, limit=limit)
    data["summary"] = audit.summary(data, lang)
    data["format"] = kind
    data["lang"] = lang
    data["names_from_state_db"] = names_known
    data["written"] = False
    data["path"] = None
    if _flag(write):
        document = audit.render_markdown(data, lang)
        try:
            path = _write_report_file(document, now, prefix="gauge-audit")
        except OSError as exc:
            data["write_error"] = f"Could not write the audit: {exc}"
        else:
            data["written"] = True
            data["path"] = str(path)
            data["media"] = f"MEDIA:{path}"
            note = f"{data['summary']}\n\n{path.name}\n{data['media']}"
            if kind == "md":
                data["markdown"] = note
            elif kind == "text":
                data["text"] = note
            return data
    if kind == "text":
        data["text"] = audit.render_text(data, lang)
    elif kind == "md":
        data["markdown"] = audit.render_markdown(data, lang)
    return data


def gauge_incidents(lang=None, days=None, format="json", write=False, **_):
    """The incident report: verdict, one card per touched session, KPIs, chronology.

    ``format="html"`` adds the page as ``html``. ``write=True`` saves it under
    ``reports/`` and returns its path and a ``MEDIA:`` line.
    """
    incidents = _sibling("incidents")
    kind = "html" if str(format or "").strip().lower() == "html" else "json"
    try:
        span = float(days) if days not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        span = None
    if span is not None and span <= 0:
        span = None
    conn = _db()
    config = load_config()
    watch, full = _thresholds(config)
    cpt = gauge_math.chars_per_token_from(config)
    now = time.time()
    lang = _active_language() if lang is None else incidents.normalize_lang(lang, default=_active_language())
    rows, names_known = _audit_rows(
        conn, _known_session_ids(conn, with_hooks=True), now, cpt, lang=lang
    )
    report = incidents.collect(
        rows, hook_events(), watch=watch, full=full, now=now, lang=lang, days=span,
    )
    report["format"] = kind
    report["names_from_state_db"] = names_known
    report["written"] = False
    report["path"] = None
    if kind == "html" or _flag(write):
        page = incidents.render_html(report)
        if kind == "html":
            report["html"] = page
        if _flag(write):
            try:
                path = _write_report_file(page, now, prefix="gauge-incidents", ext="html")
            except OSError as exc:
                report["write_error"] = f"Could not write the incident report: {exc}"
            else:
                report["written"] = True
                report["path"] = str(path)
                report["media"] = f"MEDIA:{path}"
    return report


def data_dir():
    """``~/.hermes/plugin-data/hermes-gauge``, or ``_DATA_DIR`` in tests."""
    if _DATA_DIR is not None:
        path = Path(_DATA_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("hermes-gauge")
    except Exception:
        path = Path.home() / ".hermes" / "plugin-data" / "hermes-gauge"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def gauge_render(write=False, **_):
    """Draw the current global gauge (the fullest session) as one SVG document."""
    report = _gauge_report(limit=50, format="json")
    link = report.get("weakest_link")
    if link is None:
        percent = None
        status = "OK"
        session_id = None
    else:
        percent = link["fill_percent"]
        status = link["status"]
        session_id = link["session_id"]
    svg = gauge_math.render_svg(percent, status, session_id=session_id)
    result = {
        "svg": svg,
        "fill_percent": percent,
        "status": status,
        "session_id": session_id,
        "summary": report["summary"],
        "written": False,
        "path": None,
    }
    if _flag(write):
        path = data_dir() / "gauge.svg"
        path.write_text(svg, encoding="utf-8")
        result["written"] = True
        result["path"] = str(path)
    return result


def prepare_connection(conn):
    """Make a desktop ``data.db`` readable by ``_reading``. Missing columns are added."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
    for name, decl in _USAGE_EXTRA:
        if name not in existing:
            conn.execute(f"ALTER TABLE usage ADD COLUMN {name} {decl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS windows (
               model TEXT PRIMARY KEY, window_tokens INTEGER NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS model_degradation (
               target TEXT PRIMARY KEY, factor REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_windows (
               session_id TEXT PRIMARY KEY, model TEXT, window_tokens INTEGER NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agents (
               session_id TEXT NOT NULL, agent_id TEXT NOT NULL, model TEXT,
               chars_in INTEGER NOT NULL DEFAULT 0, detected_window INTEGER,
               direct_chars INTEGER, PRIMARY KEY (session_id, agent_id))"""
    )
    conn.commit()


def _background_session(session_id, meta):
    sid = str(session_id or "").lower()
    source = str((meta or {}).get("source") or "").lower()
    if source in ("cron", "subagent"):
        return True
    return sid.startswith("cron") or sid.startswith("subagent")


def latest_interactive_id(conn):
    """The open conversation when the caller did not name one.

    The latest active interactive session. A cron job or a subagent never wins.
    """
    try:
        rows = conn.execute(
            "SELECT session_id, last_seen FROM usage ORDER BY last_seen DESC"
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    meta, _known = _state_meta([row[0] for row in rows])
    sess = _sibling("sessions")
    now = time.time()
    frozen = _frozen_after_s()
    fallback = None
    for sid, seen in rows:
        info = meta.get(sid)
        if _background_session(sid, info):
            continue
        if fallback is None:
            fallback = sid
        if sess.classify(info, seen, now, frozen_after=frozen) == sess.STATE_ACTIVE:
            return sid
    return fallback


def _room_line(remaining, lang):
    if remaining is None:
        return None
    thousands = max(int(remaining) // 1000, 0)
    if lang == "fr":
        return "plus de place" if thousands == 0 else f"environ {thousands} k jetons de place restante"
    return "no room left" if thousands == 0 else f"about {thousands}k tokens of room left"


def _band_color(state):
    return {"OK": "#00e676", "WATCH": "#ffb300", "FULL": "#ff5252"}.get(state, "#7d8a9c")


RECENT_COMPACTION_SECONDS = 60
# Older than this, the popover no longer mentions the summary.
COMPACTION_NOTE_SECONDS = 6 * 3600


def _last_compaction(conn, session_id, now, pct, anchor):
    """The latest summary of this session, for the chip. ``None`` when there is none.

    ``before_*`` is the count the gauge held when Hermes summarized. ``after_*`` is
    the first measurement taken since; it stays ``None`` while the gauge is still
    measuring. A fresh start (``/new``) and the in-flight WATCH copy are not summaries.
    """
    audit = _sibling("audit")
    try:
        rows = conn.execute(
            "SELECT reason, snapshot, created FROM compaction_log "
            "WHERE session_id = ? ORDER BY id DESC LIMIT 25",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        # An older or partial database: the note is a nicety, the chip must still answer.
        return None
    for reason, snapshot, created in rows:
        if audit.event_kind(reason) != audit.KIND_COMPACTION:
            continue
        try:
            before = json.loads(snapshot) if snapshot else {}
        except json.JSONDecodeError:
            before = {}
        if not isinstance(before, dict):
            before = {}
        try:
            at = float(created)
        except (TypeError, ValueError):
            return None
        ago = max(int(now - at), 0)
        if ago > COMPACTION_NOTE_SECONDS:
            return None
        return {
            "at": at,
            "ago_s": ago,
            "reason": str(reason or ""),
            "automatic": str(reason or "").strip().lower() != "manual",
            "recent": ago <= RECENT_COMPACTION_SECONDS,
            "before_pct": before.get("fill_percent"),
            "before_tokens": before.get("estimated_tokens"),
            "after_pct": pct if anchor else None,
        }
    return None


def check_payload(conn, session_id=None):
    """The chip payload. Same fill as ``gauge_check`` and the alerts.

    ``pct`` is the effective percentage. A missing database row is ``NO DATA``.
    A compaction that has not been measured again is ``SUMMARIZED``.
    """
    prepare_connection(conn)
    sid = (str(session_id).strip() if session_id else "") or latest_interactive_id(conn)
    lang = _active_language()
    empty = {
        "pct": None,
        "state": "NO DATA",
        "color": "#7d8a9c",
        "hint": _alert_copy(lang)["next"]["NO DATA"],
    }
    if not sid:
        return empty
    fields, usage = _reading(conn, sid)
    if usage is None:
        return empty
    anchor = bool(usage.get("anchor_valid", 1))
    try:
        age = round(time.time() - float(usage.get("last_seen") or time.time()))
    except (TypeError, ValueError):
        age = 0
    if age < 0:
        age = 0
    state = fields.get("status") or "UNKNOWN"
    pct = fields.get("fill_percent")
    if not anchor:
        state = "SUMMARIZED"
        pct = None
    nominal = fields.get("nominal_window") or 0
    estimated = fields.get("estimated_tokens") or 0
    pct_nominal = None
    if nominal and estimated is not None and pct is not None:
        pct_nominal = gauge_math.fill_percent(estimated / int(nominal))
    family, _factor = gauge_math.model_family(fields.get("model"))
    name = _session_label(sid, lang)
    last_compaction = _last_compaction(conn, sid, time.time(), pct, anchor)
    if last_compaction is not None:
        last_compaction["after_tokens"] = estimated if anchor and pct is not None else None
    return {
        "basis": _basis(usage),
        "last_compaction": last_compaction,
        "session_id_full": str(sid),
        "pct": pct,
        "pct_nominal": pct_nominal,
        "pct_effective": pct,
        "state": state,
        "color": _band_color(state),
        "chars_in": usage.get("chars_in") or 0,
        "chars_per_token": gauge_math.chars_per_token_from(load_config()),
        "tokens_in": estimated,
        "window_chars": nominal,
        "window_tokens": nominal,
        "effective_window_tokens": fields.get("effective_window"),
        "degradation_factor": fields.get("degrade_factor"),
        "model_family": family,
        "model": fields.get("model") or usage.get("model") or "",
        "session_id": str(sid)[:18],
        "turns": usage.get("turns") or 0,
        "tools_chars": usage.get("tools_chars") or 0,
        "skills_chars": usage.get("skills_chars") or 0,
        "conversation_chars": usage.get("conversation_chars") or 0,
        "system_chars": usage.get("system_chars") or 0,
        "age_s": age,
        "idle": age > 3600,
        "name": name,
        "room": _room_line(fields.get("remaining_tokens"), lang),
        "next": _alert_copy(lang)["next"].get(state, ""),
        "hint": _alert_copy(lang)["next"].get(state, ""),
        "language": lang,
    }


def config_view():
    """Active preferences with their source. Same dict the Desktop panel reads."""
    return _sibling("prefs").view(config_path())


def config_set(updates):
    """Validate and persist preferences in ``config.yaml``. All or nothing."""
    result = _sibling("prefs").set_values(updates, config_path())
    if result.get("ok") and "alerts.route" in (result.get("changed") or {}):
        _parsed, _target, fallback = _route_target(result["changed"]["alerts.route"])
        if fallback == "no_home_platform":
            note = _alert_copy(_active_language())["home"]
            result = {**result, "warning": note}
    return result


_CONFIG_HELP = (
    "Usage: /gauge config  |  /gauge config set key=value [key=value ...]\n"
    "Example: /gauge config set alerts.route=home alerts.cooldown_minutes=10"
)


def _slash_config(rest):
    prefs = _sibling("prefs")
    words = rest.split(None, 1)
    action = words[0].lower() if words else "show"
    if action in ("show", "view", "get"):
        return prefs.render_view_text(config_view())
    if action == "set":
        pairs = prefs.parse_assignments(words[1] if len(words) > 1 else "")
        if not pairs:
            return _CONFIG_HELP
        return prefs.render_set_text(config_set(pairs))
    return _CONFIG_HELP


def _slash_window(rest, session_id):
    """``/gauge window 200000`` stores the nominal window for this conversation."""
    text = (rest or "").strip().replace(",", "").replace("_", "").replace(" ", "")
    if not text:
        return "Usage: /gauge window <tokens>   example: /gauge window 200000"
    try:
        tokens = int(text)
    except ValueError:
        return "Usage: /gauge window <tokens>   example: /gauge window 200000"
    if tokens < 1:
        return "The window has to be at least 1 token."
    if not session_id:
        return "No conversation is open, so the window was not saved."
    gauge_set_window(window_tokens=tokens, session_id=session_id)
    lang = _active_language()
    if lang == "fr":
        return f"Fenêtre fixée à {tokens} jetons pour cette conversation."
    return f"Window set to {tokens} tokens for this conversation."


def _slash_audit(words):
    kind, lang, write, target = "text", None, False, None
    for word in words:
        lowered = word.lower()
        if lowered in ("md", "json"):
            kind = lowered
        elif lowered in ("fr", "en"):
            lang = lowered
        elif lowered == "write":
            write = True
        else:
            target = word
    data = gauge_audit(
        session_id=target, format=kind, lang=_active_language(lang), write=write and kind != "json"
    )
    if data.get("written"):
        return data["markdown"] if kind == "md" else data["text"]
    if "write_error" in data:
        return data["write_error"]
    if kind == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    return data["markdown"] if kind == "md" else data["text"]


def _slash_incidents(words):
    lang, days = None, None
    index = 0
    while index < len(words):
        word = words[index].lower()
        if word in ("fr", "en"):
            lang = word
        elif word in ("days", "jours") and index + 1 < len(words):
            days = words[index + 1]
            index += 1
        elif word.isdigit():
            days = word
        index += 1
    report = gauge_incidents(lang=lang, days=days, write=True)
    if report.get("write_error"):
        return report["write_error"] + "\n" + report["verdict"]["message"]
    return f"{report['verdict']['message']}\n\n{Path(report['path']).name}\n{report['media']}"


def gauge_slash(raw_args="", session_id=None, **_kwargs):
    """``/gauge`` for this session, ``/gauge report`` (or md, json) for every session,
    ``/gauge audit`` for peaks and compactions, ``/gauge incidents`` for the incident
    report page, ``/gauge config`` for the preferences."""
    original = (raw_args or "").strip()
    head, _space, tail = original.partition(" ")
    if head.lower() == "config":
        return _slash_config(tail.strip())
    if head.lower() == "window":
        return _slash_window(tail.strip(), session_id)
    if head.lower() == "audit":
        return _slash_audit(tail.split())
    if head.lower() in ("incidents", "incident"):
        return _slash_incidents(tail.split())
    text = original.lower()
    if text in ("report", "md", "json") or text.startswith("report"):
        words = text.split()
        kind = "text"
        if "md" in words:
            kind = "md"
        elif "json" in words:
            kind = "json"
        report = _gauge_report(format=kind, write="write" in words and kind != "json")
        if report.get("written"):
            return report["markdown"] if kind == "md" else report["text"]
        if "write_error" in report:
            return report["write_error"]
        if kind == "md":
            return report["markdown"]
        if kind == "json":
            return json.dumps(report, ensure_ascii=False, indent=2)
        return report["text"]
    if not session_id:
        return _gauge_report(format="text")["text"]
    reading = _gauge_check(session_id=session_id)
    if reading.get("error"):
        return reading["error"]
    return (
        f"{reading.get('session_id')} — {reading.get('fill_display')} "
        f"{reading.get('status')}"
    )


def _status_or_ok(fields):
    if fields["fill_percent"] is None:
        return "UNKNOWN"
    return fields["status"]


def _absorb_window_call(args, model, window_tokens, session_id, degrade, kwargs):
    """Positional string is the model; a dict or keywords carry the rest.

    ``window_set`` / ``degrade_set`` record that the caller passed the
    argument, including an explicit null. A degrade-only call must not
    reset the window.
    """
    window_set = window_tokens is not None or "window_tokens" in kwargs
    degrade_set = degrade is not None or "degrade" in kwargs
    if not model and kwargs.get("model"):
        model = kwargs.get("model")
    if not session_id and kwargs.get("session_id"):
        session_id = kwargs.get("session_id")
    if not window_set and "window_tokens" in kwargs:
        window_tokens = kwargs.get("window_tokens")
        window_set = True
    if not degrade_set and "degrade" in kwargs:
        degrade = kwargs.get("degrade")
        degrade_set = True
    for value in args:
        if isinstance(value, dict):
            if not model and value.get("model"):
                model = value.get("model")
            if not session_id and value.get("session_id"):
                session_id = value.get("session_id")
            if not window_set and "window_tokens" in value:
                window_tokens = value.get("window_tokens")
                window_set = True
            if not degrade_set and "degrade" in value:
                degrade = value.get("degrade")
                degrade_set = True
        elif isinstance(value, str) and value and not model:
            model = value
        elif isinstance(value, int) and not isinstance(value, bool) and not window_set:
            window_tokens = value
            window_set = True
    return model, window_tokens, session_id, degrade, window_set, degrade_set


def _attach_degrade(result, info):
    if not info:
        return result
    result["degrade_factor"] = info.get("factor")
    result["degrade_target"] = info.get("target")
    return result


def gauge_set_window(*args, model=None, window_tokens=None, session_id=None, degrade=None, **kwargs):
    """Override the window, and optionally the reliable-window factor.

    With ``session_id`` (Hermes injects it) the window override is stored on
    that session only and cannot leak onto another session or another model.
    Without it, the legacy per-model table remains so older callers keep
    working — new callers should pass the session.

    ``degrade`` is forwarded to ``set_degradation`` for the session when
    ``session_id`` is set, otherwise for the model. Pass ``degrade="reset"``
    to drop that row. A call that only sets ``degrade`` leaves the window
    override untouched.
    """
    model, window_tokens, session_id, degrade, window_set, degrade_set = _absorb_window_call(
        args, model, window_tokens, session_id, degrade, kwargs
    )
    if not model and not (degrade_set and session_id):
        return {"error": "model is required"}

    degrade_info = None
    if degrade_set:
        degrade_info = set_degradation(session_id or model, degrade)
        if degrade_info.get("error"):
            return degrade_info
        if not window_set:
            result = {
                "model": model,
                "degrade_factor": degrade_info.get("factor"),
                "degrade_target": degrade_info.get("target"),
                "reset": degrade_info.get("reset"),
            }
            if session_id:
                result["session_id"] = session_id
            return result

    conn = _db()
    if session_id:
        if window_tokens is None:
            conn.execute(
                "DELETE FROM session_windows WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            nominal, source, _config = _resolve(conn, session_id, model)
            return _attach_degrade({
                "model": model,
                "session_id": session_id,
                "window_tokens": nominal if source != "unresolved" else None,
                "window_source": source,
                "reset": True,
            }, degrade_info)
        conn.execute(
            """INSERT INTO session_windows (session_id, model, window_tokens)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   window_tokens = excluded.window_tokens,
                   model = excluded.model""",
            (session_id, model, int(window_tokens)),
        )
        conn.execute(
            "UPDATE usage SET window_unresolved = 0 WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return _attach_degrade({
            "model": model,
            "session_id": session_id,
            "window_tokens": int(window_tokens),
            "window_source": "configured",
            "reset": False,
        }, degrade_info)

    if window_tokens is None:
        conn.execute("DELETE FROM windows WHERE model = ?", (model,))
        conn.commit()
        nominal, source, _config = _resolve(conn, None, model)
        window = nominal if source != "unresolved" and nominal is not None else gauge_math.DEFAULT_FALLBACK_WINDOW
        # Preserve the historical return when the only answer is the default.
        if source == "fallback" or nominal is None:
            window = gauge_math.DEFAULT_FALLBACK_WINDOW if nominal is None else nominal
        return _attach_degrade(
            {"model": model, "window_tokens": window, "reset": True}, degrade_info
        )
    conn.execute(
        "INSERT INTO windows (model, window_tokens) VALUES (?, ?) "
        "ON CONFLICT(model) DO UPDATE SET window_tokens = excluded.window_tokens",
        (model, int(window_tokens)),
    )
    conn.commit()
    return _attach_degrade(
        {"model": model, "window_tokens": int(window_tokens), "reset": False},
        degrade_info,
    )


def invalidate_anchor(session_id, reason="compaction"):
    """Drop the usage anchor so a stale fill cannot be shown (Hermes #109760).

    The pre-compaction snapshot is kept in ``compaction_log``. The live row
    no longer has a trustworthy count, so the gauge shows an em dash until a
    real measurement arrives: a provider token count, or a full transcript.
    The window is not touched. A summary does not change the model's context
    length, so the last detected one stays known.
    """
    if not session_id:
        return None
    conn = _db()
    fields, usage = _reading(conn, session_id, notify=False)
    if usage is None:
        return None
    snapshot = {
        "estimated_tokens": fields.get("estimated_tokens"),
        "fill_display": fields.get("fill_display"),
        "fill_percent": fields.get("fill_percent"),
        "model": fields.get("model"),
        "turns": fields.get("turns"),
        "breakdown": fields.get("breakdown"),
        "window_source": fields.get("window_source"),
        "reason": reason,
    }
    conn.execute(
        """INSERT INTO compaction_log (session_id, reason, snapshot, created)
           VALUES (?, ?, ?, ?)""",
        (session_id, reason, json.dumps(snapshot), time.time()),
    )
    conn.execute(
        """UPDATE usage SET
               chars_in = 0,
               anchor_valid = 0,
               provider_tokens = NULL,
               direct_chars = NULL,
               notified_status = NULL,
               dull_latched = 0,
               dull_alerted_at = NULL,
               system_chars = 0,
               tools_chars = 0,
               skills_chars = 0,
               conversation_chars = 0
           WHERE session_id = ?""",
        (session_id,),
    )
    conn.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
    conn.commit()
    return snapshot


def compaction_events(session_id=None):
    conn = _db()
    if session_id is None:
        rows = conn.execute(
            "SELECT session_id, reason, snapshot, created FROM compaction_log ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT session_id, reason, snapshot, created FROM compaction_log "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    events = []
    for sid, reason, snapshot, created in rows:
        try:
            payload = json.loads(snapshot) if snapshot else {}
        except json.JSONDecodeError:
            payload = {"raw": snapshot}
        events.append({
            "session_id": sid,
            "reason": reason,
            "snapshot": payload,
            "created": created,
        })
    return events


def _turn_chars(conn, session_id, turn_id):
    if not turn_id:
        return None
    row = conn.execute(
        "SELECT chars FROM recorded_turns WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    ).fetchone()
    return row[0] if row else None


def _store_turn(conn, session_id, turn_id, chars, absolute=False):
    """Remember a turn. ``created`` is set once, on the first write of the turn."""
    conn.execute(
        """INSERT INTO recorded_turns (session_id, turn_id, chars, created, absolute)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session_id, turn_id) DO UPDATE SET
               chars = excluded.chars,
               absolute = MAX(absolute, excluded.absolute)""",
        (session_id, turn_id, chars, time.time(), 1 if absolute else 0),
    )
    conn.commit()


def _commit_turn(session_id, turn_id, chars, absolute=False, **record_kwargs):
    """Record ``chars`` for a turn.

    A second commit of the same turn adds only the growth, so a flushed
    partial and a later final frame are not double-counted. ``absolute``
    replaces the stored total (a full transcript measurement).
    """
    if not session_id:
        _log_hook("", "missed", "commit without session_id")
        return 0
    chars = max(int(chars or 0), 0)
    conn = _db()
    previous = _turn_chars(conn, session_id, turn_id) if turn_id else None
    if absolute:
        record_llm_call(session_id, chars, replace=True, **record_kwargs)
        if turn_id:
            _store_turn(conn, session_id, turn_id, chars, absolute=True)
        return chars
    if previous is None:
        record_llm_call(session_id, chars, reply_only=True, **record_kwargs)
        if turn_id:
            _store_turn(conn, session_id, turn_id, chars)
        return chars
    growth = chars - previous
    if growth > 0:
        record_llm_call(session_id, growth, reply_only=True, **record_kwargs)
        conn.execute(
            "UPDATE recorded_turns SET chars = ? WHERE session_id = ? AND turn_id = ?",
            (chars, session_id, turn_id),
        )
        conn.commit()
    return max(growth, 0)


def handle_stream_delta(session_id=None, delta=None, turn_id=None, kind=None, model=None, **kwargs):
    """Buffer streaming text. Reasoning deltas are ignored. Nothing is committed."""
    if not session_id:
        _log_hook("", "missed", "on_stream_delta without session_id")
        return {"missed": True}
    if (kind or "text") == "reasoning":
        return {"buffered": False, "ignored": "reasoning"}
    key = (session_id, turn_id or "stream")
    buf = _BUFFERS.setdefault(key, {"text": "", "model": None, "context_length": None})
    buf["text"] += delta or ""
    if model:
        buf["model"] = model
    context_length = _context_length_from(kwargs)
    if context_length:
        buf["context_length"] = context_length
    return {"buffered": True, "chars": len(buf["text"])}


def handle_stream_end(session_id=None, turn_id=None, final_text=None, finished=True,
                      error=None, model=None, **kwargs):
    """Commit a stream once. An error still keeps the partial text and is logged."""
    key = (session_id or "", turn_id or "stream")
    buf = _BUFFERS.pop(key, None)
    if final_text is None:
        final_text = (buf or {}).get("text") or ""
    if not session_id:
        _log_hook("", "missed", "on_stream_end without session_id")
        return {"missed": True}
    if error or not finished:
        _log_hook(session_id, "provider_error", str(error or "stream did not finish"))
        _log_hook(session_id, "missed", "post_llm_call did not run after an incomplete stream")
    context_length = _context_length_from(kwargs) or (buf or {}).get("context_length")
    _commit_turn(
        session_id,
        turn_id or "stream",
        len(final_text or ""),
        model=model or (buf or {}).get("model"),
        context_length=context_length,
        confirm_window=True if context_length else False,
    )
    _after_turn(session_id)
    return {"recorded": True, "chars": len(final_text or ""), "error": bool(error or not finished)}


def handle_api_error(session_id=None, error=None, model=None, turn_id=None, **kwargs):
    """Provider failure: log the miss, keep any buffered partial, do not invent a fill."""
    detail = str(error or kwargs.get("reason") or kwargs.get("error_message") or "provider error")
    _log_hook(session_id or "", "provider_error", detail)
    if not session_id:
        _log_hook("", "missed", "api_request_error without session_id")
        return {"missed": True}
    key = (session_id, turn_id or kwargs.get("api_request_id") or "stream")
    buf = _BUFFERS.pop(key, None)
    context_length = _context_length_from(kwargs)
    if buf and buf.get("text"):
        _log_hook(session_id, "missed", "provider error closed a stream; partial text kept")
        _commit_turn(
            session_id,
            key[1],
            len(buf["text"]),
            model=model or buf.get("model"),
            context_length=context_length or buf.get("context_length"),
            confirm_window=bool(context_length or buf.get("context_length")),
        )
        _after_turn(session_id)
        return {"missed": True, "recorded_partial": True}
    _log_hook(session_id, "missed", "post_llm_call did not run after a provider error")
    if context_length or model:
        record_llm_call(
            session_id,
            0,
            model=model,
            context_length=context_length,
            confirm_window=bool(context_length),
            touch_chars=False,
        )
        _after_turn(session_id)
    return {"missed": True}


def handle_post_api_request(session_id=None, model=None, usage=None, **kwargs):
    """Remember provider usage when a request succeeds. Does not add a fake turn."""
    if not session_id:
        return {"skipped": True}
    prompt = _prompt_tokens(usage if usage is not None else kwargs.get("usage"))
    context_length = _context_length_from({"usage": usage, **kwargs})
    if prompt is None and not context_length:
        return {"skipped": True}
    response_model = model or kwargs.get("response_model")
    record_llm_call(
        session_id,
        0,
        model=response_model,
        context_length=context_length,
        confirm_window=bool(context_length),
        provider_tokens=prompt,
        touch_chars=False,
    )
    _after_turn(session_id)
    return {"recorded": True, "provider_tokens": prompt, "context_length": context_length}


def handle_post_llm_call(session_id=None, assistant_response=None, model=None, turn_id=None,
                         conversation_history=None, **kwargs):
    """Successful turn, and any stream-shaped payload delivered to this hook.

    A turn already committed from ``on_stream_end`` is not counted again.
    ``post_llm_call`` itself does not fire on provider errors or interrupted
    streams; those enter through ``handle_api_error`` / ``handle_stream_end``.
    """
    if kwargs.get("delta") is not None and assistant_response is None and kwargs.get("final_text") is None:
        return handle_stream_delta(
            session_id=session_id,
            delta=kwargs.get("delta"),
            turn_id=turn_id,
            kind=kwargs.get("kind"),
            model=model,
            context_length=_context_length_from(kwargs),
        )
    if kwargs.get("error") and assistant_response is None and kwargs.get("final_text") is None:
        return handle_api_error(
            session_id=session_id,
            error=kwargs.get("error"),
            model=model,
            turn_id=turn_id,
            context_length=_context_length_from(kwargs),
            reason=kwargs.get("reason"),
        )
    if kwargs.get("final_text") is not None and assistant_response is None:
        return handle_stream_end(
            session_id=session_id,
            turn_id=turn_id,
            final_text=kwargs.get("final_text"),
            finished=kwargs.get("finished", True),
            error=kwargs.get("error"),
            model=model,
            context_length=_context_length_from(kwargs),
        )
    if not session_id:
        _log_hook("", "missed", "post_llm_call without session_id")
        return {"missed": True}
    text = assistant_response
    if text is None:
        text = kwargs.get("response") or kwargs.get("response_text") or ""
    context_length = _context_length_from(kwargs)
    if conversation_history is not None:
        total, buckets = gauge_math.measure_messages(conversation_history)
        # The transcript is the whole prompt. Replace, and do not mirror it again.
        record_kwargs = dict(
            model=model,
            direct=True,
            breakdown=buckets,
            context_length=context_length,
            confirm_window=True if context_length else False,
        )
        chars = total
        absolute = True
    else:
        record_kwargs = dict(
            model=model,
            context_length=context_length,
            confirm_window=True if context_length else False,
        )
        chars = len(text or "")
        absolute = False
    _BUFFERS.pop((session_id, turn_id or "stream"), None)
    added = _commit_turn(session_id, turn_id, chars, absolute=absolute, **record_kwargs)
    _record_turn_tools(session_id, turn_id, conversation_history, kwargs)
    _after_turn(session_id)
    return {"recorded": True, "chars": chars, "added": added}


def _record_turn_tools(session_id, turn_id, conversation_history, kwargs):
    """Store named tool-result lengths for this turn. Never invents a name."""
    if not session_id:
        return {"attribution": "turn-only", "stored": 0}
    messages = conversation_history
    if messages is None:
        messages = kwargs.get("messages")
    request = kwargs.get("request")
    if messages is None and isinstance(request, dict):
        messages = request.get("messages")
    if isinstance(messages, list):
        extracted = gauge_math.extract_turn_tools(messages)
    else:
        extracted = gauge_math.extract_turn_tools(
            None,
            tool_calls=kwargs.get("tool_calls"),
            tool_results=kwargs.get("tool_results"),
        )
    tools_found = extracted.get("tools") or []
    if extracted.get("attribution") != "tool-level" or not tools_found:
        return {"attribution": "turn-only", "stored": 0}
    _store_turn_tools(session_id, turn_id or "turn", tools_found)
    return {"attribution": "tool-level", "stored": len(tools_found)}


def _store_turn_tools(session_id, turn_id, tool_rows):
    """Replace this turn's rows so a replay of the same payload does not double."""
    conn = _db()
    conn.execute(
        "DELETE FROM turn_tools WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    )
    conn.executemany(
        """INSERT INTO turn_tools (session_id, turn_id, tool_name, result_chars)
           VALUES (?, ?, ?, ?)""",
        [
            (session_id, turn_id, row["tool_name"], int(row["result_chars"]))
            for row in tool_rows
            if row.get("tool_name")
        ],
    )
    conn.commit()


def _session_tools(conn, session_id, top):
    rows = conn.execute(
        """SELECT tool_name, SUM(result_chars) AS chars, COUNT(*) AS calls
           FROM turn_tools
           WHERE session_id = ?
           GROUP BY tool_name
           ORDER BY chars DESC, tool_name ASC
           LIMIT ?""",
        (session_id, top),
    ).fetchall()
    by_tool = [
        {
            "tool_name": name,
            "result_chars": int(chars or 0),
            "calls": int(calls or 0),
        }
        for name, chars, calls in rows
    ]
    if by_tool:
        return by_tool, "tool-level"
    return [], "turn-only"


def _turn_top_tool(conn, session_id, turn_id):
    rows = conn.execute(
        """SELECT tool_name, SUM(result_chars) AS chars
           FROM turn_tools
           WHERE session_id = ? AND turn_id = ?
           GROUP BY tool_name
           ORDER BY chars DESC, tool_name ASC
           LIMIT 1""",
        (session_id, turn_id),
    ).fetchone()
    if not rows:
        return None
    return rows[0], int(rows[1] or 0)


def handle_compaction(session_id=None, old_session_id=None, reason=None, kind=None, **kwargs):
    """Invalidate the anchor that compaction just made stale."""
    reason = reason or kwargs.get("reason") or kind or "compaction"
    targets = []
    if old_session_id:
        targets.append(old_session_id)
    # In-place compaction leaves old_session_id empty and reuses session_id.
    if session_id and not old_session_id and session_id not in targets:
        targets.append(session_id)
    if not targets and kwargs.get("session_id"):
        targets.append(kwargs["session_id"])
    snapshots = []
    audit = _sibling("audit")
    for sid in targets:
        snapshots.append(invalidate_anchor(sid, reason=reason))
        if audit.event_kind(reason) == audit.KIND_COMPACTION:
            _notify_summarized(sid)
    return {"invalidated": targets, "snapshots": snapshots}


def _notify_summarized(session_id):
    """One notice when Hermes has just summarized a conversation."""
    if not session_id:
        return None
    try:
        lang = _active_language()
        who = _session_label(session_id, lang)
        message = _alert_copy(lang)["summarized"].format(who=who)
        route = gauge_math.alert_settings(load_config())["route"]
        return emit_hermes_notification(message, session_id=session_id, route=route)
    except Exception:
        return None


def handle_session_reset(session_id=None, old_session_id=None, new_session_id=None,
                         reason=None, **kwargs):
    """A /new or session swap must not keep showing the previous fill."""
    new_id = new_session_id or session_id
    old_id = old_session_id or kwargs.get("old_session_id")
    if old_id:
        invalidate_anchor(old_id, reason=reason or "new")
    elif new_id:
        invalidate_anchor(new_id, reason=reason or "new")
    if new_id and new_id != old_id:
        conn = _db()
        conn.execute("DELETE FROM usage WHERE session_id = ?", (new_id,))
        conn.execute("DELETE FROM agents WHERE session_id = ?", (new_id,))
        conn.execute("DELETE FROM session_windows WHERE session_id = ?", (new_id,))
        conn.execute("DELETE FROM turn_tools WHERE session_id = ?", (new_id,))
        conn.commit()
    if old_id:
        conn = _db()
        conn.execute("DELETE FROM turn_tools WHERE session_id = ?", (old_id,))
        conn.commit()
    return {"cleared": True, "session_id": new_id, "old_session_id": old_id}


def handle_subagent_stop(parent_session_id=None, child_session_id=None, model=None,
                         context_length=None, child_summary=None, **kwargs):
    """Fold a child agent into the parent subject. Status uses the smaller window."""
    if not parent_session_id or not child_session_id:
        return {"attached": False}
    if parent_session_id == child_session_id:
        return {"attached": False}
    conn = _db()
    parent = _as_usage(_usage_row(conn, parent_session_id))
    child = _as_usage(_usage_row(conn, child_session_id))
    if parent is not None and not conn.execute(
        "SELECT 1 FROM agents WHERE session_id = ? AND agent_id = ?",
        (parent_session_id, "session"),
    ).fetchone():
        conn.execute(
            """INSERT INTO agents
               (session_id, agent_id, model, chars_in, detected_window, direct_chars)
               VALUES (?, 'session', ?, ?, ?, ?)""",
            (
                parent_session_id,
                parent["model"],
                parent["chars_in"],
                parent["detected_window"],
                parent["direct_chars"],
            ),
        )
    child_chars = child["chars_in"] if child else len(child_summary or "")
    child_model = (child["model"] if child and child["model"] else None) or model
    child_window = child["detected_window"] if child else None
    if context_length and not child_window:
        child_window = int(context_length)
    child_direct = child["direct_chars"] if child else None
    conn.execute(
        """INSERT INTO agents
           (session_id, agent_id, model, chars_in, detected_window, direct_chars)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id, agent_id) DO UPDATE SET
               model = COALESCE(excluded.model, model),
               chars_in = excluded.chars_in,
               detected_window = COALESCE(excluded.detected_window, detected_window),
               direct_chars = COALESCE(excluded.direct_chars, direct_chars)""",
        (parent_session_id, child_session_id, child_model, child_chars, child_window, child_direct),
    )
    conn.commit()
    return {"attached": True, "session_id": parent_session_id, "agent_id": child_session_id}


def gauge_audit(*args, session_id=None, limit=10, format=None, lang=None, write=False, **kwargs):
    """Setup audit, or the v0.6 peaks report when a report format is asked for.

    ``gauge_audit(session_id)`` is the context-setup audit (overhead, memory
    bank, degradation, dull threshold). ``/gauge audit`` passes ``format``
    and receives the peaks report (compactions, composition, names).
    """
    for value in args:
        if isinstance(value, str) and value and not session_id:
            session_id = value
        elif isinstance(value, dict):
            if not session_id and value.get("session_id"):
                session_id = value.get("session_id")
            if format is None and "format" in value:
                format = value.get("format")
            if lang is None and "lang" in value:
                lang = value.get("lang")
            if "limit" in value:
                limit = value.get("limit")
            if "write" in value:
                write = value.get("write")
            kwargs = {**value, **kwargs}
    if not session_id:
        session_id = kwargs.get("session_id")
    peaks = format is not None or lang is not None or bool(write)
    if peaks:
        return _peaks_audit(
            session_id=session_id,
            limit=10 if limit is None else limit,
            format=format or "text",
            lang=lang or "en",
            write=write,
        )
    kwargs.pop("session_id", None)
    return _setup_audit(session_id=session_id)


def hermes_home():
    """Hermes root. Tests point ``_HERMES_HOME`` at a temporary directory."""
    if _HERMES_HOME is not None:
        return Path(_HERMES_HOME)
    return Path.home() / ".hermes"


def _memory_limits(config_path):
    """Char budgets from a Hermes ``config.yaml`` ``memory:`` block.

    Inline comments are stripped. A missing file or key yields ``{}`` so the
    caller can fall back to the root config.
    """
    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    found = {}
    in_memory = False
    memory_indent = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1].strip()
        if not in_memory:
            if indent == 0 and key == "memory" and val == "":
                in_memory = True
                memory_indent = indent
            continue
        if indent <= memory_indent:
            break
        if key in ("memory_char_limit", "user_char_limit") and val:
            try:
                found[key] = int(float(val))
            except ValueError:
                continue
    return found


def _active_memory_files(directory):
    """Live notes in one memories directory. Locks and backups are not active."""
    if not directory.is_dir():
        return []
    files = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".lock") or ".bak" in name:
            continue
        files.append(path)
    return files


def _memory_parts(path, text):
    """Split a Hermes memory file on ``§``. Other files are one entry."""
    if path.name in ("MEMORY.md", "USER.md") and "§" in text:
        return [part.strip() for part in text.split("§") if part.strip()]
    stripped = text.strip()
    return [stripped] if stripped else []


def _memory_preview(text):
    preview = " ".join(text.split())
    if len(preview) > 80:
        return preview[:77] + "..."
    return preview


def _collect_memory_store(name, directory, limits):
    """One store: sizes, budget fill, and the entries used for duplicates."""
    directory = Path(directory)
    files = _active_memory_files(directory)
    total_bytes = 0
    total_chars = 0
    memory_chars = None
    user_chars = None
    entries = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            size = path.stat().st_size
        except OSError:
            continue
        total_bytes += size
        total_chars += len(text)
        if path.name == "MEMORY.md":
            memory_chars = len(text)
        elif path.name == "USER.md":
            user_chars = len(text)
        for index, part in enumerate(_memory_parts(path, text), start=1):
            entries.append({
                "store": name,
                "file": path.name,
                "index": index,
                "chars": len(part),
                "preview": _memory_preview(part),
                "text": part,
                "id": f"{name}/{path.name}#{index}",
            })
    memory_budget = limits.get("memory_char_limit")
    user_budget = limits.get("user_char_limit")

    def _fill(chars, budget):
        if chars is None or not budget:
            return None
        return round(chars / budget, 4)

    store = {
        "name": name,
        "path": str(directory),
        "files": len(files),
        "bytes": total_bytes,
        "chars": total_chars,
        "entries": len(entries),
        "memory_chars": memory_chars,
        "memory_budget": memory_budget,
        "memory_fill": _fill(memory_chars, memory_budget),
        "user_chars": user_chars,
        "user_budget": user_budget,
        "user_fill": _fill(user_chars, user_budget),
    }
    return store, entries


def _memory_duplicates(entries):
    """Pairs whose smaller token set is more than 80% shared."""
    tokenized = []
    for entry in entries:
        tokens = {
            token.lower()
            for token in _MEMORY_TOKEN.findall(entry["text"])
            if len(token) > 1
        }
        tokenized.append((entry, tokens))
    found = []
    for left_index, (left, left_tokens) in enumerate(tokenized):
        if len(left_tokens) < _MEMORY_MIN_TOKENS:
            continue
        for right, right_tokens in tokenized[left_index + 1:]:
            if len(right_tokens) < _MEMORY_MIN_TOKENS:
                continue
            shared = len(left_tokens & right_tokens)
            smaller = min(len(left_tokens), len(right_tokens))
            similarity = shared / smaller
            if similarity > _MEMORY_DUP_ABOVE:
                found.append({
                    "left": left["id"],
                    "right": right["id"],
                    "similarity": round(similarity, 2),
                    "shared_tokens": shared,
                })
    found.sort(key=lambda item: (-item["similarity"], item["left"], item["right"]))
    return found


def audit_memory_bank(home):
    """Size, budget fill, longest entries, and near-duplicates of the memory bank.

    Scans ``<home>/memories`` and ``<home>/profiles/*/memories``. Returns
    ``(metrics, findings)``. Findings are alerts above 90% of a known char
    budget, and warnings for near-duplicate entries.
    """
    home = Path(home)
    root_limits = _memory_limits(home / "config.yaml")
    stores = []
    entries = []
    store, store_entries = _collect_memory_store("default", home / "memories", root_limits)
    stores.append(store)
    entries.extend(store_entries)
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in sorted(path for path in profiles.iterdir() if path.is_dir()):
            directory = child / "memories"
            if not directory.is_dir():
                continue
            limits = dict(root_limits)
            limits.update(_memory_limits(child / "config.yaml"))
            store, store_entries = _collect_memory_store(child.name, directory, limits)
            stores.append(store)
            entries.extend(store_entries)
    duplicates = _memory_duplicates(entries)
    top = sorted(entries, key=lambda item: (-item["chars"], item["id"]))[:5]
    fills = []
    for store in stores:
        for key in ("memory_fill", "user_fill"):
            if store[key] is not None:
                fills.append(store[key])
    alert = any(fill > _MEMORY_BUDGET_ALERT for fill in fills)
    findings = []
    for store in stores:
        for chars_key, budget_key, fill_key, label in (
            ("memory_chars", "memory_budget", "memory_fill", "MEMORY.md"),
            ("user_chars", "user_budget", "user_fill", "USER.md"),
        ):
            fill = store[fill_key]
            if fill is not None and fill > _MEMORY_BUDGET_ALERT:
                findings.append({
                    "severity": "alert",
                    "area": "memory",
                    "detail": _say(
                        f"Memory {store['name']}/{label} is at {round(fill * 100, 1)}% "
                        f"of its budget ({store[chars_key]}/{store[budget_key]} characters).",
                        f"Mémoire {store['name']}/{label} à {round(fill * 100, 1)}% "
                        f"du budget ({store[chars_key]}/{store[budget_key]} caractères).",
                    ),
                })
    for dup in duplicates[:10]:
        findings.append({
            "severity": "warn",
            "area": "memory",
            "detail": _say(
                f"Near-identical memories ({round(dup['similarity'] * 100)}% of "
                f"tokens shared): {dup['left']} and {dup['right']}.",
                f"Mémoires quasi identiques ({round(dup['similarity'] * 100)}% de "
                f"tokens partagés) : {dup['left']} et {dup['right']}.",
            ),
        })
    metrics = {
        "files": sum(store["files"] for store in stores),
        "bytes": sum(store["bytes"] for store in stores),
        "chars": sum(store["chars"] for store in stores),
        "entries": len(entries),
        "stores": stores,
        "top_entries": [
            {
                "store": item["store"],
                "file": item["file"],
                "index": item["index"],
                "chars": item["chars"],
                "preview": item["preview"],
            }
            for item in top
        ],
        "duplicates": duplicates[:20],
        "duplicate_count": len(duplicates),
        "max_fill_ratio": max(fills) if fills else None,
        "alert": alert,
    }
    return metrics, findings


def _audit_reading(session_id):
    """Compose a reading without flushing buffers or pushing an alert."""
    if not session_id:
        return {"error": "no session"}
    conn = _db()
    fields, usage = _reading(conn, session_id, notify=False)
    return _finish(conn, session_id, fields, usage, notify=False)


def _setup_audit(*args, session_id=None, **kwargs):
    """Audit the context setup: overhead, model sweet spot, config and files.

    Answers "is this context healthy or misconfigured?" — oversized
    system/tools/skills overhead, a fallback window, an anchor the hook lost,
    a degradation factor that makes the WATCH threshold arrive too late, and
    heavyweight files (memories, skills, plugins) on disk.
    Does not flush stream buffers and does not push an alert.
    """
    for value in args:
        if isinstance(value, str) and value and not session_id:
            session_id = value
        elif isinstance(value, dict):
            if not session_id and value.get("session_id"):
                session_id = value.get("session_id")
            kwargs = {**value, **kwargs}
    if not session_id:
        session_id = kwargs.get("session_id")

    home = hermes_home()
    findings = []

    def flag(severity, area, message):
        findings.append({"severity": severity, "area": area, "detail": message})

    config = load_config()
    watch, full = _thresholds(config)
    # --- live reading -----------------------------------------------------
    reading = _audit_reading(session_id)
    audit = {
        "session_id": session_id,
        "reading": {k: reading.get(k) for k in (
            "fill_display", "status", "nominal_window", "effective_window",
            "degrade_factor", "window_source", "estimated_tokens", "turns",
            "anchor_valid",
        )} if "error" not in reading else reading,
        "policy_verdict": reading.get("policy_verdict") if isinstance(reading, dict) else None,
        "findings": findings,
    }
    if not isinstance(audit["policy_verdict"], dict):
        audit["policy_verdict"] = gauge_math.policy_verdict(
            None, None, config, _active_language()
        )
    audit["thresholds"] = {"watch": watch, "full": full}
    audit["config_path"] = str(Path(__file__).with_name("config.yaml"))
    audit["effective_factors"] = (config or {}).get("effective_factors") or {}
    audit["effective_models"] = (config or {}).get("effective_models") or {}

    model = reading.get("model") if isinstance(reading, dict) else None
    chosen = model
    stored = None
    if session_id and "error" not in reading:
        _factor, chosen, stored = _applied_degrade(
            _db(), session_id, model, config, agents=reading.get("agents")
        )
    audit["active_factors"] = gauge_math.active_factors(chosen, config, stored=stored)
    nominal = reading.get("nominal_window") if isinstance(reading, dict) else None
    degrade = reading.get("degrade_factor") if isinstance(reading, dict) else None
    if degrade is None:
        degrade = gauge_math.degradation_factor(model, config)
    audit["degrade_factor"] = degrade
    effective = reading.get("effective_window") if isinstance(reading, dict) else None
    if effective:
        audit["effective_window"] = effective
    elif nominal:
        audit["effective_window"] = max(int(float(nominal) * float(degrade)), 1)
        effective = audit["effective_window"]
    turns = int(reading.get("turns") or 0) if isinstance(reading, dict) else 0
    if reading.get("window_source") == "fallback":
        audit["fallback_turns"] = turns
        audit["fallback_turn_limit"] = FALLBACK_TURNS_BEFORE_WARN
        if turns > FALLBACK_TURNS_BEFORE_WARN:
            fallback = (config or {}).get("fallback_window") or gauge_math.DEFAULT_FALLBACK_WINDOW
            model_label = model or "unknown"
            flag(
                "warn",
                "window",
                _say(
                    f"Session on the fallback window ({fallback}) for {turns} turns "
                    f"(limit {FALLBACK_TURNS_BEFORE_WARN}). "
                    f"Fix: gauge_set_window(model={model_label!r}, window_tokens=<actual>, "
                    f"session_id={session_id!r}) or set models.{model_label} in config.yaml.",
                    f"Session sur la fenêtre fallback ({fallback}) depuis {turns} tours "
                    f"(seuil {FALLBACK_TURNS_BEFORE_WARN}). "
                    f"Fixer : gauge_set_window(model={model_label!r}, window_tokens=<réel>, "
                    f"session_id={session_id!r}) ou renseigner models.{model_label} dans config.yaml.",
                ),
            )
        else:
            flag("warn", "window", _say(
                "Window is on 'fallback' (default, not measured): the estimate may be "
                "wrong. Let the hook confirm it, or set effective_factors/models in config.yaml.",
                "Fenêtre en 'fallback' (défaut, pas mesuré) — "
                "l'estimation peut être fausse. Laisser le hook confirmer ou fixer "
                "effective_factors/models dans config.yaml.",
            ))
    if reading.get("status") == "UNKNOWN":
        flag("warn", "window", _say(
            "Invalid anchor: the hook did not confirm the window for this session "
            "(compaction or a missed turn).",
            "Ancre invalide : le hook n'a pas confirmé la "
            "fenêtre pour cette session (compression ou missed turn).",
        ))

    # --- thresholds vs degradation ---------------------------------------
    watch, full = _thresholds(config)
    if nominal and effective and degrade < 1.0:
        fill = reading.get("estimated_tokens") or 0
        ratio_effective = fill / effective if effective else 0
        ratio_nominal = fill / nominal if nominal else 0
        audit["fill_effective_pct"] = round(ratio_effective * 100, 1)
        audit["fill_nominal_pct"] = round(ratio_nominal * 100, 1)
        if ratio_effective >= watch and ratio_nominal < watch:
            flag("alert", "sweet-spot", _say(
                 f"The model ({model}) already has {round(ratio_effective * 100, 1)}% of its "
                 f"reliable window filled (factor {degrade}) while the nominal gauge "
                 f"shows only {round(ratio_nominal * 100, 1)}%. The nominal gauge is "
                 f"optimistic: read the WATCH/FULL status on effective_window.",
                 f"Le modèle ({model}) a déjà {round(ratio_effective * 100, 1)}% de sa "
                 f"fenêtre fiable remplie (facteur {degrade}) alors que la jauge "
                 f"nominale n'affiche que {round(ratio_nominal * 100, 1)}% — la jauge "
                 f"nominale est optimiste : le statut WATCH/FULL doit être lu "
                 f"sur effective_window."))

    # --- overhead breakdown ----------------------------------------------
    breakdown = reading.get("breakdown") if isinstance(reading, dict) else None
    if breakdown:
        total = sum(int(v or 0) for v in breakdown.values()) or 1
        overhead = sum(int(breakdown.get(k) or 0) for k in ("system_prompt", "tools", "skills"))
        audit["overhead_tokens"] = overhead
        audit["overhead_pct"] = round(overhead / total * 100, 1)
        audit["breakdown_pct"] = {k: round(int(v or 0) / total * 100, 1) for k, v in breakdown.items()}
        if overhead / total > 0.5:
            flag("alert", "overhead", _say(
                 f"{audit['overhead_pct']}% of the context is fixed overhead "
                 f"(system prompt + tools + skills): wasted room on every turn.",
                 f"{audit['overhead_pct']}% du contexte est du fixed overhead "
                 f"(system prompt + outils + skills) — de l'air perdu à chaque tour."))
        elif overhead / total > 0.3:
            flag("warn", "overhead", _say(
                 f"{audit['overhead_pct']}% fixed overhead: check the loaded skills "
                 f"and the number of tools/plugins.",
                 f"{audit['overhead_pct']}% d'overhead fixe : vérifier les skills "
                 f"chargées et le nombre d'outils/plugins."))

    # Thresholds have to make sense for this model, not in the abstract.
    # WATCH below the fixed overhead never leaves OK. FULL below the
    # session's conversation never leaves room for a normal exchange.
    conversation_tokens = 0
    if isinstance(breakdown, dict):
        conversation_tokens = int(breakdown.get("conversation") or 0)
    audit["conversation_tokens"] = conversation_tokens
    if effective:
        watch_tokens = int(round(float(watch) * float(effective)))
        full_tokens = int(round(float(full) * float(effective)))
        audit["watch_tokens"] = watch_tokens
        audit["full_tokens"] = full_tokens
        overhead_tokens = audit.get("overhead_tokens")
        if overhead_tokens is not None and watch_tokens < overhead_tokens:
            flag(
                "alert",
                "thresholds",
                _say(
                    f"WATCH threshold ({watch}) × effective window ({effective}) = "
                    f"{watch_tokens} < measured fixed overhead ({overhead_tokens}). "
                    f"The WATCH threshold cannot be reached for this model.",
                    f"Seuil WATCH ({watch}) × fenêtre effective ({effective}) = "
                    f"{watch_tokens} < overhead fixe mesuré ({overhead_tokens}). "
                    f"Le seuil WATCH est inatteignable pour ce modèle.",
                ),
            )
        if conversation_tokens and full_tokens < conversation_tokens:
            flag(
                "alert",
                "thresholds",
                _say(
                    f"FULL threshold ({full}) × effective window ({effective}) = "
                    f"{full_tokens} < typical conversation of the session "
                    f"({conversation_tokens} tokens). "
                    f"The FULL threshold cannot be reached for this model.",
                    f"Seuil FULL ({full}) × fenêtre effective ({effective}) = "
                    f"{full_tokens} < conversation typique de la session "
                    f"({conversation_tokens} tokens). "
                    f"Le seuil FULL est inatteignable pour ce modèle.",
                ),
            )
        verdict = audit.get("policy_verdict") or {}
        dull_at = verdict.get("dull_at_applied")
        overhead_tokens = audit.get("overhead_tokens")
        if dull_at is not None and overhead_tokens is not None:
            dull_tokens = int(round(float(dull_at) * float(effective)))
            audit["dull_tokens"] = dull_tokens
            if dull_tokens < overhead_tokens:
                flag(
                    "alert",
                    "policy",
                    _say(
                        f"Dull threshold ({dull_at}) × effective window ({effective}) = "
                        f"{dull_tokens} < measured fixed overhead ({overhead_tokens}). "
                        f"The threshold cannot be reached for this model.",
                        f"Seuil débile ({dull_at}) × fenêtre effective ({effective}) = "
                        f"{dull_tokens} < overhead fixe mesuré ({overhead_tokens}). "
                        f"Le seuil est inatteignable pour ce modèle.",
                    ),
                )

    # --- files on disk -----------------------------------------------------
    def dir_stats(path, pattern="*"):
        files = sorted(Path(path).rglob(pattern)) if Path(path).exists() else []
        size = sum(f.stat().st_size for f in files if f.is_file())
        return len(files), size

    if home.exists():
        files_audit = {}
        for label, rel in (
            ("AGENT.md", "AGENT.md"), ("SOUL.md", "SOUL.md"),
            ("config.yaml", "config.yaml"),
        ):
            p = home / rel
            if p.exists():
                files_audit[label] = p.stat().st_size
                if label == "config.yaml":
                    code = sum(
                        1 for line in p.read_text(errors="ignore").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")
                    )
                    files_audit[label + "_code_lines"] = code
                    if code > 400:
                        flag("alert", "config", _say(
                             f"config.yaml holds {code} lines of effective settings, "
                             f"beyond the stock file: check what was added and whether "
                             f"it belongs there.",
                             f"config.yaml contient {code} lignes de réglages "
                             f"effectifs — au-delà de la doc d'usine, vérifier ce "
                             f"qui a été ajouté et si ça vit au bon endroit."))
                    elif p.stat().st_size > 80_000:
                        files_audit[label + "_note"] = _say(
                            "large file, but mostly inline documentation (comments): healthy.",
                            "gros fichier mais surtout de la doc inline (commentaires) — sain.",
                        )
        mem_dir = home / "memories"
        n, size = dir_stats(mem_dir)
        if size > 120_000:
            flag("alert", "memory", _say(
                 f"Heavy memory bank: {size // 1024} KB across {n} files. "
                 f"Every session inherits part of it. Purge or consolidate.",
                 f"Banque mémoire lourde : {size // 1024} KB sur {n} fichiers — "
                 f"chaque session en hérite une partie. Purger/consolider."))
        elif size > 60_000:
            flag("warn", "memory", _say(
                 f"Memory is {size // 1024} KB across {n} files: consolidate "
                 f"before it gets heavy.",
                 f"Mémoire {size // 1024} KB / {n} fichiers : "
                 f"à consolider avant que ça pèse."))
        files_audit["memories"] = {"files": n, "bytes": size}
        skills_root = home / "skills"
        n, size = dir_stats(skills_root)
        files_audit["skills"] = {"files": n, "bytes": size}
        if size > 400_000:
            flag("warn", "skills", _say(
                 f"skills/ weighs {size // 1024} KB: only the relevant skills are "
                 f"injected, but the index keeps growing.",
                 f"skills/ pèse {size // 1024} KB — seules les "
                 f"skills pertinentes sont injectées, mais l'index grossit."))
        big = []
        for f in sorted(skills_root.rglob("SKILL.md")):
            s = f.stat().st_size
            if s > 30_000:
                big.append((s, str(f.relative_to(skills_root))))
        if big:
            flag("warn", "skills", "SKILL.md > 30 KB : " +
                 ", ".join(f"{p} ({s // 1024} KB)" for s, p in sorted(big, reverse=True)[:5]))
        plugins_root = home / "plugins"
        n, size = dir_stats(plugins_root)
        files_audit["plugins"] = {"files": n, "bytes": size}
        audit["files"] = files_audit

    memory_metrics, memory_findings = audit_memory_bank(home)
    audit["memory"] = memory_metrics
    findings.extend(memory_findings)

    # --- hermes doctor (static setup checks) — delegated, not re-implemented ---
    # Boundary: hermes doctor owns static setup (dependencies, credentials,
    # gateways, profiles, advisories). This tool owns runtime context
    # economics (fill, effective window, overhead, memory-store pressure).
    # Here we only EMBED doctor's warnings so the caller has one entry point.
    try:
        import os as _os
        import subprocess
        proc = subprocess.run(
            ["hermes", "doctor"], capture_output=True, text=True, timeout=120,
            env={**_os.environ, "HERMES_DOCTOR_QUIET": "1"},
        )
        warnings = [
            line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip().startswith(("⚠", "!")) or " ⚠ " in line
        ]
        audit["doctor"] = {
            "ran": True,
            "passed": proc.returncode == 0,
            "warnings": warnings[:20],
        }
        if not proc.returncode == 0:
            flag("warn", "doctor", _say(
                 f"hermes doctor reports {len(warnings)} warning(s). Run "
                 f"`hermes doctor` for the detail (dependencies, tokens, "
                 f"gateways). gauge_audit does not duplicate them.",
                 f"hermes doctor signale {len(warnings)} avertissement(s) — "
                 f"lancer `hermes doctor` pour le détail (dépendances, tokens, "
                 f"gateways). gauge_audit ne les duplique pas."))
    except Exception as exc:  # doctor absent or slow: audit stays self-contained
        audit["doctor"] = {"ran": False, "error": str(exc)[:120]}

    audit["status"] = "ALERT" if any(f["severity"] == "alert" for f in findings) else (
        "WARN" if findings else "HEALTHY")
    return audit

def gauge_policy(*args, **_kwargs):
    """The active policy, where each value came from, and what it will not do.

    Read-only. Does not edit ``config.yaml``, does not push, and does not
    compact or rotate a session.
    """
    resolved = gauge_math.resolve_policy(load_config(), _active_language())
    return {
        "preset": resolved["preset"],
        "preset_applied": resolved["preset_applied"],
        "preset_known": resolved["preset_known"],
        "active": {
            "mode": resolved["mode"],
            "dull_at": resolved["dull_at"],
            "on_dull": resolved["on_dull"],
            "cooldown_minutes": resolved["cooldown_minutes"],
        },
        "sources": resolved["sources"],
        "effect": resolved["effect"],
        "will_not": _say(
            "The plugin never compacts a session and never opens a new one, "
            "whatever the mode. It observes, recommends, and pushes a "
            "notification. Compaction and the session cycle belong to Hermes core.",
            "Le plugin ne compacte jamais une session et n'en ouvre jamais "
            "une nouvelle, quel que soit le mode. Il observe, recommande, et "
            "pousse une notification. La compaction et le cycle de session "
            "appartiennent à Hermes core.",
        ),
    }


def gauge_blame(*args, session_id=None, top=8, **_):
    """Deterministic attribution: which turns, and which tools, added context.

    Turn cost is the delta between consecutive recorded transcripts.
    ``by_tool`` sums ``tool_result`` lengths stored from the hook payload.
    Names that were not in the payload are not invented: ``attribution``
    is ``turn-only`` until a named tool has been stored.
    """
    for a in args:
        if isinstance(a, str) and a and not session_id:
            session_id = a
        elif isinstance(a, dict):
            if not session_id:
                session_id = a.get("session_id")
            if isinstance(a.get("top"), int):
                top = a["top"]
    try:
        top = max(int(top or 8), 1)
    except (TypeError, ValueError):
        top = 8
    if not session_id:
        return {"error": "No session — pass session_id."}
    conn = _db()
    by_tool, attribution = _session_tools(conn, session_id, top)
    rows = conn.execute(
        "SELECT turn_id, chars FROM recorded_turns WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    if not rows:
        return {
            "session_id": session_id,
            "turns": 0,
            "by_tool": by_tool,
            "attribution": attribution,
            "note": "No recorded turns yet.",
        }
    deltas = []
    previous = 0
    for turn_id, chars in rows:
        chars = int(chars or 0)
        deltas.append({
            "turn_id": turn_id,
            "cumulative_chars": chars,
            "added_chars": max(chars - previous, 0),
        })
        previous = chars
    ordered = sorted(deltas, key=lambda d: d["added_chars"], reverse=True)
    # A trailing 0-char turn (post-compaction record) must not reset the
    # totals: the context size is the highest cumulative ever recorded.
    total_added = max((d["cumulative_chars"] for d in deltas), default=0)
    real_turns = [d for d in deltas if d["added_chars"] > 0 and d["cumulative_chars"] > 0]
    avg = total_added // max(len(real_turns), 1)
    findings = []
    for d in ordered[:top]:
        if d["added_chars"] > 3 * avg and d["added_chars"] > 20_000:
            ratio = round(d["added_chars"] / max(avg, 1))
            detail = _say(
                f"Turn {d['turn_id'][-8:]} added "
                f"{d['added_chars'] // 1024} KB, {ratio}x the average "
                f"turn cost ({avg // 1024} KB): main culprit",
                f"Tour {d['turn_id'][-8:]} a ajouté "
                f"{d['added_chars'] // 1024} KB, soit "
                f"{ratio}x le coût moyen d'un tour "
                f"({avg // 1024} KB) — coupable principal",
            )
            culprit = _turn_top_tool(conn, session_id, d["turn_id"])
            finding = {
                "severity": "alert",
                "turn_id": d["turn_id"],
                "added_chars": d["added_chars"],
            }
            if culprit:
                detail += _say(
                    f", via {culprit[0]} ({culprit[1]} characters).",
                    f" : {culprit[0]} ({culprit[1]} caractères).",
                )
                finding["tool_name"] = culprit[0]
            else:
                detail += "."
            finding["detail"] = detail
            findings.append(finding)
    if by_tool and not any(by_tool[0]["tool_name"] in item.get("detail", "") for item in findings):
        top_tool = by_tool[0]
        findings.append({
            "severity": "warn",
            "tool_name": top_tool["tool_name"],
            "result_chars": top_tool["result_chars"],
            "detail": _say(
                f"Main culprit: {top_tool['tool_name']} "
                f"({top_tool['result_chars']} characters over {top_tool['calls']} call(s)).",
                f"Coupable principal : {top_tool['tool_name']} "
                f"({top_tool['result_chars']} caractères sur {top_tool['calls']} appel(s)).",
            ),
        })
    heavy = [d for d in deltas if d["added_chars"] > avg * 2]
    if attribution == "tool-level":
        note = _say(
            "Every turn replays the whole conversation: added_chars is the actual "
            "cost of that turn. by_tool sums the tool_result lengths read from "
            "the payload: counting only, no invented names.",
            "Chaque tour rejoue toute la conversation : added_chars est le "
            "coût réel de ce tour. by_tool somme les longueurs de tool_result "
            "lues dans la payload — comptage uniquement, aucun nom inventé.",
        )
    else:
        note = _say(
            "Every turn replays the whole conversation: added_chars is the actual "
            "cost of that turn. Attribution is turn-only: the payload carried "
            "no tool name.",
            "Chaque tour rejoue toute la conversation : added_chars est le "
            "coût réel de ce tour. attribution turn-only : la payload n'a pas "
            "fourni de nom d'outil.",
        )
    return {
        "session_id": session_id,
        "turns": len(deltas),
        "total_context_chars": total_added,
        "avg_turn_chars": avg,
        "heavy_turns": len(heavy),
        "top_adds": [
            {"turn_id": d["turn_id"], "added_kb": d["added_chars"] // 1024}
            for d in ordered[:top]
        ],
        "by_tool": by_tool,
        "attribution": attribution,
        "findings": findings,
        "note": note,
    }
