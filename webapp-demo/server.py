#!/usr/bin/env python3
"""hermes-gauge demo dashboard.

Stdlib only. Three ways to read the gauge:

- live — the plugin SQLite database, when it exists
- ``?demo=1`` — six fictional sessions, two per band; nothing is written
- fixture — four sessions at 12 / 38 / 63 / 92 %. This is the public demo.
  It is the default when Hermes is not installed, and what ``make demo`` serves.

``GET /health`` is there so a cold start can prove the process is up.

The server listens on 127.0.0.1 only. In its default mode it serves live
per-session data (ids, models, fill), so it leaves the machine only when you ask
for it: ``--host`` names the interface and ``--token`` (or ``GAUGE_TOKEN``) is
then mandatory. Every request must carry the token, as ``Authorization: Bearer``,
``?token=`` or the cookie that ``?token=`` sets for the browser.
"""

import argparse
import hmac
import importlib.util
import ipaddress
import json
import os
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHARS_PER_TOKEN = 4

# Canonical demo sessions — keep in sync with webapp-demo/index.html.
# Default window is 200_000 tokens. Two sessions per band, fullest first.
# (session_id, model, fill_percent, status, turns, estimated_tokens, age_seconds)
DEMO_SESSIONS = (
    ("release-review", "claude-opus", 96.0, "FULL", 48, 192_000, 120),
    ("incident-room", "claude-sonnet", 88.0, "FULL", 31, 176_000, 840),
    ("onboarding", "hermes-3", 74.0, "WATCH", 19, 148_000, 3_600),
    ("api-contract", "gpt-4.1", 63.0, "WATCH", 14, 126_000, 10_800),
    ("quick-question", "hermes-3", 28.0, "OK", 5, 56_000, 93_600),
    ("standup-notes", "hermes-3", 12.0, "OK", 3, 24_000, 172_800),
)

# Public demo. Fullest first. Percents are the contract for ``make demo``.
# (session_id, model, fill_percent, turns, estimated_tokens, age_seconds, window_source, breakdown)
FIXTURE_SESSIONS = (
    ("migration-plan", "claude-opus", 92.0, 40, 184_000, 120, "configured",
     (12_000, 18_000, 6_000, 148_000)),
    ("api-contract", "gpt-4.1", 63.0, 14, 126_000, 3_600, "configured",
     (8_000, 10_000, 4_000, 104_000)),
    ("onboarding", "hermes-3", 38.0, 9, 76_000, 86_400, "configured",
     (6_000, 8_000, 2_000, 60_000)),
    ("standup-notes", "hermes-3", 12.0, 3, 24_000, 172_800, "configured",
     (4_000, 2_000, 0, 18_000)),
)

_MATH = None


def db_path():
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return os.path.join(home, "plugin-data", "hermes-gauge", "data.db")


def port_from_env():
    return int(os.environ.get("GAUGE_PORT", "8787"))


DEFAULT_HOST = "127.0.0.1"
TOKEN_COOKIE = "gauge_token"


def is_loopback(host):
    """True for ``localhost`` and the 127.0.0.0/8 range: addresses only this machine can reach."""
    if str(host).strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_token_off_loopback(host, token):
    """Refuse to serve beyond this machine without a token."""
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"refusing to listen on {host} without a token: pass --token (or set GAUGE_TOKEN)"
        )


def _math():
    global _MATH
    if _MATH is None:
        path = os.path.join(ROOT, "hermes-gauge", "gauge_math.py")
        spec = importlib.util.spec_from_file_location("hermes_gauge_math_server", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MATH = module
    return _MATH


def load_config():
    math = _math()
    config = math.default_config()
    path = os.path.join(ROOT, "hermes-gauge", "config.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            config = math.merge_config(config, math.parse_config(handle.read()))
    except OSError:
        pass
    if os.environ.get("GAUGE_WINDOW"):
        config["fallback_window"] = int(os.environ["GAUGE_WINDOW"])
    return config


def using_fixtures():
    """Public demo when Hermes is absent, or when the fixture flag is set."""
    if os.environ.get("GAUGE_LIVE") == "1":
        return False
    if os.environ.get("GAUGE_FIXTURE") == "1":
        return True
    return not os.path.exists(db_path())


def _band(fill):
    if fill >= 85:
        return "FULL"
    if fill >= 60:
        return "WATCH"
    return "OK"


def clamp_percent(value):
    """Fill stays inside 0–100. Unknown stays unknown."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return 0.0
    if number > 100:
        return 100.0
    return number


def gauge_payload(query="", mode="auto"):
    """Choose demo, fixture, or live. A ``demo=1`` query wins over fixture mode."""
    qs = parse_qs(query)
    if qs.get("demo", ["0"])[0] == "1":
        return demo_state()
    if mode == "fixture" or qs.get("fixture", ["0"])[0] == "1":
        return fixture_state()
    if mode == "live":
        return gauge_state()
    if using_fixtures():
        return fixture_state()
    return gauge_state()


def _percent_text(fill):
    shown = int(fill) if float(fill).is_integer() else fill
    return f"≈{shown}%"


def _breakdown(parts):
    system, tools, skills, conversation = parts
    return {
        "system_prompt": system,
        "tools": tools,
        "skills": skills,
        "conversation": conversation,
    }


def demo_state():
    """Fictional gauge payload. Does not read or write the plugin database."""
    now = time.time()
    sessions = [
        {
            "session_id": sid,
            "model": model,
            "estimated_tokens": estimated,
            "fill_percent": fill,
            "fill_display": _percent_text(fill),
            "status": band,
            "turns": turns,
            "last_seen": now - age,
            "window_tokens": 200_000,
            "window_source": "fallback",
            "window_label": None,
            "approximate": True,
        }
        for sid, model, fill, band, turns, estimated, age in DEMO_SESSIONS
    ]
    return {
        "sessions": sessions,
        "window_tokens": 200_000,
        "total_estimated_tokens": sum(s["estimated_tokens"] for s in sessions),
        "generated_at": now,
        "demo": True,
        "fixture": False,
    }


def fixture_state():
    """Four seeded sessions. Does not read or write the plugin database."""
    now = time.time()
    sessions = []
    for sid, model, fill, turns, estimated, age, source, parts in FIXTURE_SESSIONS:
        sessions.append({
            "session_id": sid,
            "model": model,
            "estimated_tokens": estimated,
            "fill_percent": fill,
            "fill_display": _percent_text(fill),
            "status": _band(fill),
            "turns": turns,
            "last_seen": now - age,
            "window_tokens": 200_000,
            "nominal_window": 200_000,
            "output_reserve": 0,
            "window_source": source,
            "window_label": None,
            "approximate": True,
            "breakdown": _breakdown(parts),
            "agents": [],
        })
    return {
        "sessions": sessions,
        "window_tokens": 200_000,
        "total_estimated_tokens": sum(s["estimated_tokens"] for s in sessions),
        "generated_at": now,
        "demo": False,
        "fixture": True,
    }


def _columns(conn, table):
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _tables(conn):
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    except sqlite3.Error:
        return set()


def _estimate(row, math):
    if row.get("provider_tokens") is not None:
        return max(int(row["provider_tokens"]), 0), False
    if row.get("direct_chars") is not None:
        return math.estimate_tokens(row["direct_chars"], direct=True), True
    return math.estimate_tokens(row.get("chars_in") or 0), True


def _live_fields(row, session_window, agents, config):
    math = _math()
    watch = float(config["thresholds"]["watch"])
    full = float(config["thresholds"]["full"])
    reserve = int(config.get("output_reserve") or 0)
    fallback = int(config.get("fallback_window") or 200_000)
    anchor_valid = bool(row.get("anchor_valid", 1))
    unresolved = bool(row.get("window_unresolved", 0))
    detected = row.get("detected_window") or None

    if len(agents) >= 2:
        described = []
        for agent in agents:
            estimated, _approx = _estimate(agent, math)
            window = agent.get("detected_window") or None
            if window:
                usable = math.effective_window(int(window), reserve)
                source = "detected"
            else:
                usable = None
                source = "unresolved"
            described.append({
                "agent_id": agent.get("agent_id"),
                "model": agent.get("model") or "unknown",
                "estimated_tokens": estimated,
                "window_tokens": usable,
                "window_source": source if usable is not None else "unresolved",
                "window": usable,
                "source": source if usable is not None else "unresolved",
            })
        link = math.weakest_link(described)
        estimated = sum(agent["estimated_tokens"] for agent in described)
        if not anchor_valid or unresolved or link["window"] is None:
            fields = math.compose(estimated, None, reserve, "unresolved", False, watch, full)
        else:
            fields = math.compose(
                estimated, link["window"], 0, link["source"], True, watch, full
            )
        fields["window_label"] = link["label"]
        fields["agents"] = [
            {
                "agent_id": agent["agent_id"],
                "model": agent["model"],
                "estimated_tokens": agent["estimated_tokens"],
                "window_tokens": agent["window_tokens"],
                "window_source": agent["window_source"],
            }
            for agent in described
        ]
    else:
        estimated, approximate = _estimate(row, math)
        if not anchor_valid or (unresolved and not detected and not session_window):
            fields = math.compose(estimated, None, reserve, "unresolved", False, watch, full)
        else:
            if session_window:
                nominal, source = int(session_window), "configured"
            elif detected:
                nominal, source = int(detected), "detected"
            else:
                nominal, source = fallback, "fallback"
            fields = math.compose(
                estimated, nominal, reserve, source, True, watch, full, approximate=approximate
            )
        fields["window_label"] = None
        fields["agents"] = []

    direct = row.get("direct_chars") is not None
    fields["breakdown"] = math.breakdown_tokens(
        {
            "system_prompt": row.get("system_chars") or 0,
            "tools": row.get("tools_chars") or 0,
            "skills": row.get("skills_chars") or 0,
            "conversation": row.get("conversation_chars") or 0,
        },
        fields.get("estimated_tokens") or 0,
        direct=direct,
    )
    return fields


def _empty_live(config):
    return {
        "sessions": [],
        "window_tokens": int(config.get("fallback_window") or 200_000),
        "total_estimated_tokens": 0,
        "generated_at": time.time(),
        "demo": False,
        "fixture": False,
    }


def gauge_state():
    """Read the plugin database. Read-only. Never creates the file."""
    config = load_config()
    path = db_path()
    if not os.path.exists(path):
        return _empty_live(config)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return _empty_live(config)
    try:
        tables = _tables(conn)
        if "usage" not in tables:
            return _empty_live(config)
        cols = _columns(conn, "usage")
        wanted = ["session_id", "chars_in", "turns", "model", "last_seen"]
        optional = [
            "anchor_valid", "window_unresolved", "detected_window", "provider_tokens",
            "direct_chars", "system_chars", "tools_chars", "skills_chars", "conversation_chars",
        ]
        selected = [name for name in wanted + optional if name in cols]
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM usage ORDER BY chars_in DESC LIMIT 50"
        ).fetchall()
        windows = {}
        if "session_windows" in tables:
            for sid, tokens in conn.execute(
                "SELECT session_id, window_tokens FROM session_windows"
            ):
                windows[sid] = tokens
        agents = {}
        if "agents" in tables:
            agent_cols = _columns(conn, "agents")
            agent_select = [
                name for name in (
                    "session_id", "agent_id", "model", "chars_in", "detected_window", "direct_chars"
                )
                if name in agent_cols
            ]
            if "session_id" in agent_select and "chars_in" in agent_select:
                for record in conn.execute(
                    f"SELECT {', '.join(agent_select)} FROM agents"
                ):
                    item = dict(zip(agent_select, record))
                    agents.setdefault(item["session_id"], []).append(item)
    except sqlite3.Error:
        return _empty_live(config)
    finally:
        conn.close()

    sessions = []
    for record in rows:
        row = dict(zip(selected, record))
        fields = _live_fields(
            row,
            windows.get(row["session_id"]),
            agents.get(row["session_id"], []),
            config,
        )
        sessions.append({
            "session_id": row["session_id"],
            "model": row.get("model") or "unknown",
            "estimated_tokens": fields.get("estimated_tokens") or 0,
            "fill_percent": clamp_percent(fields.get("fill_percent")),
            "fill_display": fields.get("fill_display"),
            "status": fields.get("status"),
            "turns": row.get("turns") or 0,
            "last_seen": row.get("last_seen"),
            "window_tokens": fields.get("window_tokens"),
            "nominal_window": fields.get("nominal_window"),
            "output_reserve": fields.get("output_reserve"),
            "window_source": fields.get("window_source"),
            "window_label": fields.get("window_label"),
            "approximate": fields.get("approximate"),
            "breakdown": fields.get("breakdown"),
            "agents": fields.get("agents") or [],
        })
    return {
        "sessions": sessions,
        "window_tokens": int(config.get("fallback_window") or 200_000),
        "total_estimated_tokens": sum(s["estimated_tokens"] for s in sessions),
        "generated_at": time.time(),
        "demo": False,
        "fixture": False,
    }


def dispatch(path):
    """Return ``(status, body_bytes, content_type)`` for one request path."""
    parsed = urlparse(path)
    if parsed.path == "/health":
        body = {"ok": True, "fixture": using_fixtures()}
        return 200, json.dumps(body).encode(), "application/json"
    if parsed.path == "/api/gauge":
        if os.environ.get("GAUGE_FIXTURE") == "1":
            mode = "fixture"
        elif os.environ.get("GAUGE_LIVE") == "1":
            mode = "live"
        else:
            mode = "auto"
        payload = gauge_payload(parsed.query, mode=mode)
        return 200, json.dumps(payload).encode(), "application/json"
    if parsed.path in ("/", "/index.html"):
        with open(os.path.join(HERE, "index.html"), "rb") as handle:
            return 200, handle.read(), "text/html; charset=utf-8"
    return 404, json.dumps({"error": "not found"}).encode(), "application/json"


class Handler(BaseHTTPRequestHandler):
    # None means no token is asked: only ever the case on a loopback address.
    token = None

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _supplied_token(self):
        """The token this request carries, and whether it came in the URL."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip(), False
        query = parse_qs(urlparse(self.path).query).get("token")
        if query:
            return query[0], True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE and value:
                return value, False
        return None, False

    def do_GET(self):
        headers = ()
        if self.token:
            supplied, from_url = self._supplied_token()
            if supplied is None or not hmac.compare_digest(
                supplied.encode(), str(self.token).encode()
            ):
                self._send(401, {"error": "token required"})
                return
            if from_url:
                # The page fetches /api/gauge without the query string, so the
                # browser keeps the token as a cookie scoped to this server.
                headers = ((
                    "Set-Cookie",
                    f"{TOKEN_COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict",
                ),)
        code, body, ctype = dispatch(self.path)
        self._send(code, body, ctype, headers)

    def log_message(self, fmt, *args):
        if os.environ.get("GAUGE_QUIET") == "1":
            return
        super().log_message(fmt, *args)


def _get(url, token=None):
    request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urlopen(request, timeout=2) as response:
        return response.status, response.read()


def probe(base, token=None):
    """Fetch the public endpoints and print each status code."""
    paths = ("/", "/health", "/api/gauge", "/api/gauge?demo=1")
    results = {}
    deadline = time.time() + 5
    last_error = None
    while time.time() < deadline:
        try:
            _get(base + "/health", token)
            break
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
    else:
        raise SystemExit(f"dashboard did not start: {last_error}")
    for path in paths:
        status, body = _get(base + path, token)
        results[path] = (status, body)
        print(f"GET {path} {status}")
    return results


def assert_probe(results):
    for path in ("/", "/health", "/api/gauge", "/api/gauge?demo=1"):
        status, body = results[path]
        if status != 200:
            raise SystemExit(f"{path} returned {status}")
    health = json.loads(results["/health"][1])
    if health.get("ok") is not True:
        raise SystemExit(f"/health body was {health}")
    gauge = json.loads(results["/api/gauge"][1])
    if gauge.get("fixture"):
        fills = [session["fill_percent"] for session in gauge["sessions"]]
        if fills != [92.0, 63.0, 38.0, 12.0]:
            raise SystemExit(f"fixture fills were {fills}")
        for session in gauge["sessions"]:
            percent = session["fill_percent"]
            if percent is None or percent < 0 or percent > 100:
                raise SystemExit(f"fill out of range: {percent}")
    demo = json.loads(results["/api/gauge?demo=1"][1])
    if len(demo["sessions"]) != 6:
        raise SystemExit(f"demo session count was {len(demo['sessions'])}")
    if b"Hermes" not in results["/"][1]:
        raise SystemExit("dashboard HTML did not contain Hermes")


def serve(port, open_browser=False, verify=False, self_check=False, host=DEFAULT_HOST, token=None):
    require_token_off_loopback(host, token)
    # A subclass, so two servers in one process never share a token.
    handler = type("TokenHandler", (Handler,), {"token": token})
    try:
        httpd = HTTPServer((host, port), handler)
    except OSError as exc:
        raise SystemExit(f"port {port} is not available on {host}: {exc}")
    # 0.0.0.0 answers on loopback too; any other interface is probed by its own address.
    probe_host = DEFAULT_HOST if host in ("0.0.0.0", "") else host
    base = f"http://{probe_host}:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        if verify or self_check or open_browser:
            assert_probe(probe(base, token))
        if self_check:
            print(f"hermes-gauge check ok -> {base}")
            return
        if open_browser:
            try:
                webbrowser.open(base + (f"/?token={token}" if token else ""))
            except Exception:
                pass
        print(f"hermes-gauge dashboard -> {base}")
        if not is_loopback(host):
            print(f"listening on {host}: every request needs the token (?token=... or Authorization: Bearer)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="hermes-gauge demo dashboard")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--verify", action="store_true", help="require HTTP 200, then keep serving")
    parser.add_argument("--self-check", action="store_true", help="require HTTP 200, then exit")
    parser.add_argument("--fixture", action="store_true", help="serve the 12/38/63/92 fixture sessions")
    parser.add_argument("--live", action="store_true", help="read the plugin database only")
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help="interface to listen on (default 127.0.0.1); anything else needs --token",
    )
    parser.add_argument(
        "--token", default=os.environ.get("GAUGE_TOKEN"),
        help="shared secret every request must carry (default: $GAUGE_TOKEN)",
    )
    args = parser.parse_args(argv)
    if args.fixture:
        os.environ["GAUGE_FIXTURE"] = "1"
        os.environ.pop("GAUGE_LIVE", None)
    if args.live:
        os.environ["GAUGE_LIVE"] = "1"
        os.environ.pop("GAUGE_FIXTURE", None)
    serve(
        port_from_env(),
        open_browser=args.open,
        verify=args.verify or args.self_check,
        self_check=args.self_check,
        host=args.host,
        token=args.token or None,
    )


if __name__ == "__main__":
    main()
