"""Preferences: one ``config.yaml``, read by every surface, written through one validator.

Telegram (``/gauge config``), the Desktop popover, and the dashboard backend all
come through here. Nothing keeps a private copy of a setting. Standard library
only — the file is read with the small parser in ``gauge_math``.

Public keys use dots (``alerts.route``). The file keeps the layout it always had:
``alerts.*`` lives under ``gauge: alerts:``. A top-level ``alerts:`` block is read
as an alias and written back where the key already is.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
from pathlib import Path


def _sibling(name):
    """Load a module that sits next to this file, as a package member or by path."""
    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    key = f"hermes_gauge_{name}"
    cached = sys.modules.get(key)
    path = Path(__file__).with_name(f"{name}.py")
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


gauge_math = _sibling("gauge_math")

CONFIG_PATH = Path(__file__).with_name("config.yaml")

# Order is the order users see. ``path`` is the canonical place in the file.
FIELDS = (
    {
        "key": "thresholds.watch",
        "path": ("thresholds", "watch"),
        "type": "ratio",
        "default": gauge_math.DEFAULT_WATCH,
        "min": 0.05,
        "max": 0.99,
        "label": "WATCH from",
        "help": "Fill level where the gauge turns WATCH, as a share of the usable window.",
    },
    {
        "key": "thresholds.full",
        "path": ("thresholds", "full"),
        "type": "ratio",
        "default": gauge_math.DEFAULT_FULL,
        "min": 0.05,
        "max": 0.99,
        "label": "FULL from",
        "help": "Fill level where the gauge turns FULL. Must be above the WATCH level.",
    },
    {
        "key": "alerts.enabled",
        "path": ("gauge", "alerts", "enabled"),
        "alias": ("alerts", "enabled"),
        "type": "bool",
        "default": True,
        "label": "Push alerts",
        "help": "Send a short message when a session crosses into WATCH or FULL.",
    },
    {
        "key": "alerts.on",
        "path": ("gauge", "alerts", "on"),
        "alias": ("alerts", "on"),
        "type": "statuses",
        "default": ["WATCH", "FULL"],
        "choices": ["WATCH", "FULL"],
        "label": "Alert on",
        "help": "Which statuses push: WATCH, FULL, or both.",
    },
    {
        "key": "alerts.cooldown_minutes",
        "path": ("gauge", "alerts", "cooldown_minutes"),
        "alias": ("alerts", "cooldown_minutes"),
        "type": "number",
        "default": 30,
        "min": 0,
        "max": 10080,
        "label": "Cooldown (min)",
        "help": "Quiet time per status per session. 0 turns the cooldown off.",
    },
    {
        "key": "alerts.route",
        "path": ("gauge", "alerts", "route"),
        "alias": ("alerts", "route"),
        "type": "route",
        "default": "session",
        "label": "Send alerts to",
        "help": (
            "session = this conversation. home = the home channel of that "
            "session's platform. custom:<platform>:<chat_id> = one chat."
        ),
    },
    {
        "key": "output_reserve",
        "path": ("output_reserve",),
        "type": "int",
        "default": 0,
        "min": 0,
        "max": 10_000_000,
        "label": "Reply reserve (tokens)",
        "help": "Tokens held back for the model's reply, deducted from the window.",
    },
    {
        "key": "chars_per_token",
        "path": ("chars_per_token",),
        "type": "int",
        "default": gauge_math.CHARS_PER_TOKEN,
        "min": 1,
        "max": 8,
        "label": "Characters per token",
        "help": "How many characters make one token. 4 is the usual figure for English.",
    },
    {
        "key": "language",
        "path": ("language",),
        "type": "choice",
        "default": "auto",
        "choices": ["auto", "en", "fr"],
        "label": "Language",
        "help": "auto follows the Hermes locale when one is set, otherwise English. en or fr is fixed.",
    },
    {
        "key": "frozen_after_hours",
        "path": ("frozen_after_hours",),
        "type": "number",
        "default": 72,
        "min": 1,
        "max": 8760,
        "label": "Idle after (hours)",
        "help": "A conversation with no activity for this long is idle. It stays in the report when it is almost full.",
    },
)

FIELD_BY_KEY = {field["key"]: field for field in FIELDS}
BASIC_KEYS = (
    "language", "thresholds.watch", "thresholds.full",
    "alerts.enabled", "alerts.on", "alerts.route",
)
_ALIASES = {
    "gauge.alerts.enabled": "alerts.enabled",
    "gauge.alerts.on": "alerts.on",
    "gauge.alerts.cooldown_minutes": "alerts.cooldown_minutes",
    "gauge.alerts.route": "alerts.route",
}


def config_path(path=None):
    return Path(path) if path is not None else CONFIG_PATH


def shipped_config_path():
    """The file shipped inside the plugin folder. Defaults only; an update replaces it."""
    return CONFIG_PATH


def hermes_home():
    """The Hermes home of the active profile.

    Inside Hermes this is ``hermes_constants.get_hermes_home()``, which follows
    the active profile on every call. The environment fallback is only for a run
    outside Hermes, where ``hermes_constants`` cannot be imported (the standalone
    demo, the tests).
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        home = os.environ.get("HERMES_HOME")
        return Path(home).expanduser() if home else Path.home() / ".hermes"
    return Path(get_hermes_home())


def user_config_path():
    """Preferences that survive a plugin update, under the Hermes home."""
    return hermes_home() / "plugin-data" / "hermes-gauge" / "config.yaml"


def _differs_from_default(parsed):
    for field in FIELDS:
        raw = _lookup(parsed, field)
        if raw is _MISSING:
            continue
        value, error = validate(field["key"], raw)
        if error is None and value != field["default"]:
            return True
    return False


def resolve_config_path(override=None):
    """The file to read and write.

    An explicit path (the tests, a one-off) wins. Otherwise the user file under
    ``plugin-data`` wins. The first time that file is missing, a shipped file
    that is no longer the defaults is copied across so an update does not wipe it.
    """
    if override is not None:
        return Path(override)
    target = user_config_path()
    if target.exists():
        return target
    shipped = shipped_config_path()
    if shipped.is_file():
        try:
            parsed = gauge_math.parse_config(shipped.read_text(encoding="utf-8"))
        except OSError:
            parsed = {}
        if _differs_from_default(parsed):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def resolve_language(choice, explicit=None):
    """``en`` or ``fr``. An explicit word wins, then the preference, then the locale."""
    word = str(explicit or "").strip().lower()
    if word.startswith("fr"):
        return "fr"
    if word.startswith("en"):
        return "en"
    picked = str(choice or "auto").strip().lower()
    if picked.startswith("fr"):
        return "fr"
    if picked.startswith("en"):
        return "en"
    for key in ("HERMES_LANG", "HERMES_LANGUAGE", "HERMES_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"):
        hint = str(os.environ.get(key) or "").strip().lower()
        if hint.startswith("fr"):
            return "fr"
        if hint.startswith("en"):
            return "en"
    return "en"


# --- validation -----------------------------------------------------------


class _Invalid(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _to_number(raw):
    """``(number, was_percent)``. Accepts ``0.6``, ``0,6``, ``60``, ``60%``."""
    if isinstance(raw, bool):
        raise _Invalid("not_a_number", "Expected a number.")
    if isinstance(raw, (int, float)):
        return float(raw), False
    text = str(raw).strip().replace(",", ".")
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        return float(text), percent
    except ValueError:
        raise _Invalid("not_a_number", f"{str(raw).strip()!r} is not a number.") from None


def _range(field, number):
    low, high = field.get("min"), field.get("max")
    if (low is not None and number < low) or (high is not None and number > high):
        raise _Invalid("out_of_range", f"{field['key']} must be between {low} and {high}.")


def _check_ratio(field, raw):
    number, percent = _to_number(raw)
    if percent or 1 < number <= 100:
        number = number / 100.0
    number = round(number, 4)
    _range(field, number)
    return number


def _check_number(field, raw):
    number, _percent = _to_number(raw)
    _range(field, number)
    return int(number) if number == int(number) else round(number, 2)


def _check_int(field, raw):
    number, _percent = _to_number(raw)
    if number != int(number):
        raise _Invalid("not_an_integer", f"{field['key']} must be a whole number.")
    _range(field, int(number))
    return int(number)


def _check_bool(field, raw):
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise _Invalid("not_a_boolean", f"{field['key']} must be on or off.")


def _check_statuses(field, raw):
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = [part for part in re.split(r"[,\s]+", text) if part]
    if len(items) == 1 and items[0].strip().lower() in ("none", "off", "-"):
        return []
    chosen = []
    for item in items:
        name = item.strip().strip("'\"").upper()
        if name not in field["choices"]:
            raise _Invalid(
                "invalid_choice",
                f"{field['key']} accepts {', '.join(field['choices'])} (or none), not {item.strip()!r}.",
            )
        if name not in chosen:
            chosen.append(name)
    return chosen


def _check_choice(field, raw):
    text = str(raw).strip().lower()
    if text not in field["choices"]:
        listed = ", ".join(field["choices"])
        raise _Invalid("invalid_choice", f"{field['label']} is {listed}.")
    return text


def _check_route(field, raw):
    route = gauge_math.parse_route(raw)
    if route is None:
        raise _Invalid(
            "invalid_route",
            "alerts.route is session, home, home:<platform>, or custom:<platform>:<chat_id>.",
        )
    return route["value"]


_CHECKERS = {
    "ratio": _check_ratio,
    "number": _check_number,
    "int": _check_int,
    "bool": _check_bool,
    "statuses": _check_statuses,
    "choice": _check_choice,
    "route": _check_route,
}


def canonical_key(key):
    text = str(key or "").strip().lower()
    text = _ALIASES.get(text, text)
    return text if text in FIELD_BY_KEY else None


def validate(key, raw):
    """``(value, None)`` or ``(None, {"code", "message"})`` for one key."""
    canonical = canonical_key(key)
    if canonical is None:
        return None, {"code": "unknown_key", "message": f"Unknown setting {str(key).strip()!r}."}
    field = FIELD_BY_KEY[canonical]
    try:
        return _CHECKERS[field["type"]](field, raw), None
    except _Invalid as exc:
        return None, {"code": exc.code, "message": exc.message}


# --- reading --------------------------------------------------------------


def read_parsed(path=None):
    """The file as the plugin sees it, before defaults. Missing file → ``{}``."""
    target = config_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    return gauge_math.parse_config(text)


def effective(path=None):
    """Merged config, the same dict ``tools.load_config()`` builds."""
    return gauge_math.merge_config(gauge_math.default_config(), read_parsed(path))


def _lookup(parsed, field):
    """Raw value of a field in a parsed file, or ``_MISSING``."""
    for route in (field["path"], field.get("alias")):
        if not route:
            continue
        node = parsed
        for part in route:
            if not isinstance(node, dict) or part not in node:
                node = _MISSING
                break
            node = node[part]
        if node is not _MISSING:
            return node
    return _MISSING


_MISSING = object()


def _nest(flat):
    nested = {}
    for key, value in flat.items():
        head, _dot, tail = key.partition(".")
        if tail:
            nested.setdefault(head, {})[tail] = value
        else:
            nested[head] = value
    return nested


def view(path=None):
    """Active settings with where each came from, plus the field metadata for a form."""
    parsed = read_parsed(path)
    flat = {}
    sources = {}
    fields = []
    invalid = {}
    for field in FIELDS:
        raw = _lookup(parsed, field)
        value, error = (
            (field["default"], None) if raw is _MISSING else validate(field["key"], raw)
        )
        source = "file"
        if raw is _MISSING:
            source = "default"
        elif error is not None:
            value, source = field["default"], "default"
            invalid[field["key"]] = error
        if isinstance(value, list):
            value = list(value)
        flat[field["key"]] = value
        sources[field["key"]] = source
    # A pair that the file cannot honor (watch not below full) falls back together.
    watch, full = flat["thresholds.watch"], flat["thresholds.full"]
    if watch >= full:
        for key in ("thresholds.watch", "thresholds.full"):
            flat[key] = FIELD_BY_KEY[key]["default"]
            sources[key] = "default"
            invalid[key] = {
                "code": "watch_not_below_full",
                "message": "thresholds.watch must be below thresholds.full.",
            }
    for field in FIELDS:
        entry = {
            "key": field["key"],
            "type": field["type"],
            "label": field["label"],
            "help": field["help"],
            "default": field["default"],
            "value": flat[field["key"]],
            "source": sources[field["key"]],
        }
        for extra in ("min", "max", "choices"):
            if extra in field:
                entry[extra] = field[extra]
        if field["key"] in invalid:
            entry["invalid_in_file"] = invalid[field["key"]]
        entry["group"] = "basic" if field["key"] in BASIC_KEYS else "advanced"
        fields.append(entry)
    return {
        "ok": True,
        "values": _nest(flat),
        "flat": flat,
        "sources": sources,
        "fields": fields,
        "path": str(config_path(path)),
        "profile": os.environ.get("HERMES_PROFILE") or "default",
    }


# --- writing --------------------------------------------------------------

_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:(.*)$")


def _literal(field, value):
    kind = field["type"]
    if kind == "bool":
        return "true" if value else "false"
    if kind == "statuses":
        return "[" + ", ".join(value) + "]"
    if kind == "route":
        return value if value in ("session", "home") else f'"{value}"'
    return repr(value) if isinstance(value, float) else str(value)


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _find(lines, keys):
    """Index of the line that holds ``keys`` (a path), mirroring ``parse_config`` nesting."""
    stack = []
    wanted = tuple(keys)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_LINE.match(raw.rstrip("\r\n"))
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = tuple(name for _depth, name in stack) + (key,)
        if path == wanted:
            return index
        if match.group(3).strip() == "":
            stack.append((indent, key))
    return None


def _block_end(lines, index):
    """Last non-blank, non-comment line inside the block opened at ``index``."""
    indent = _indent_of(lines[index])
    last = index
    for pos in range(index + 1, len(lines)):
        stripped = lines[pos].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent_of(lines[pos]) <= indent:
            break
        last = pos
    return last


def _child_indent(lines, index):
    """Indent of the first child under the block at ``index``, else parent + 2."""
    parent = _indent_of(lines[index])
    for pos in range(index + 1, len(lines)):
        stripped = lines[pos].strip()
        if not stripped or stripped.startswith("#"):
            continue
        child = _indent_of(lines[pos])
        return child if child > parent else parent + 2
    return parent + 2


def _set_line(lines, index, literal):
    match = _KEY_LINE.match(lines[index].rstrip("\r\n"))
    rest = match.group(3)
    comment = ""
    body = rest.strip()
    quoted = body[:1] in ("'", '"')
    if not quoted and " #" in body:
        body, _sep, tail = body.partition(" #")
        comment = "  #" + tail
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{match.group(1)}{match.group(2)}: {literal}{comment}{ending}"


def apply_to_text(text, field, value):
    """Return ``text`` with ``field`` set to ``value``. Every other line is untouched."""
    lines = text.splitlines(keepends=True)
    literal = _literal(field, value)
    for route in (field["path"], field.get("alias")):
        if not route:
            continue
        found = _find(lines, route)
        if found is not None:
            _set_line(lines, found, literal)
            return "".join(lines)
    path = field["path"]
    # Deepest block that already exists, then the missing levels below it.
    for depth in range(len(path) - 1, -1, -1):
        parent = _find(lines, path[:depth]) if depth else None
        if depth and parent is None:
            continue
        missing = path[depth:]
        indent = _child_indent(lines, parent) if depth else 0
        if depth:
            at = _block_end(lines, parent) + 1
            if lines[at - 1] and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            at = len(lines)
        block = []
        for offset, name in enumerate(missing[:-1]):
            block.append(" " * (indent + 2 * offset) + f"{name}:\n")
        block.append(" " * (indent + 2 * (len(missing) - 1)) + f"{missing[-1]}: {literal}\n")
        lines[at:at] = block
        return "".join(lines)
    return text


def _write_atomic(target, text):
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.chmod(temp, target.stat().st_mode & 0o777)
    except OSError:
        pass
    os.replace(temp, target)


def set_values(updates, path=None):
    """Validate every pair, then write them all or none. Returns a dict, never raises.

    ``{"ok": bool, "changed": {key: value}, "errors": {key: {code, message}}, **view}``
    """
    errors = {}
    chosen = {}
    for raw_key, raw_value in dict(updates or {}).items():
        canonical = canonical_key(raw_key)
        if canonical is None:
            errors[str(raw_key)] = {
                "code": "unknown_key",
                "message": f"Unknown setting {str(raw_key).strip()!r}.",
            }
            continue
        value, error = validate(canonical, raw_value)
        if error is not None:
            errors[canonical] = error
        else:
            chosen[canonical] = value
    current = view(path)
    if not errors and ("thresholds.watch" in chosen or "thresholds.full" in chosen):
        watch = chosen.get("thresholds.watch", current["flat"]["thresholds.watch"])
        full = chosen.get("thresholds.full", current["flat"]["thresholds.full"])
        if watch >= full:
            key = "thresholds.watch" if "thresholds.watch" in chosen else "thresholds.full"
            errors[key] = {
                "code": "watch_not_below_full",
                "message": "thresholds.watch must be below thresholds.full.",
            }
    if not chosen and not errors:
        errors["_"] = {"code": "nothing_to_set", "message": "No setting was given."}
    if errors:
        return {**current, "ok": False, "changed": {}, "errors": errors}
    target = config_path(path)
    try:
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        for key, value in chosen.items():
            text = apply_to_text(text, FIELD_BY_KEY[key], value)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, text)
    except OSError as exc:
        return {
            **current,
            "ok": False,
            "changed": {},
            "errors": {"_": {"code": "write_failed", "message": f"Could not write {target.name}: {exc.strerror or exc}."}},
        }
    return {**view(path), "ok": True, "changed": chosen, "errors": {}}


_ASSIGN = re.compile(r"""([A-Za-z0-9_.]+)\s*=\s*("[^"]*"|'[^']*'|\[[^\]]*\]|\S+)""")


def parse_assignments(text):
    """``a=b c="d e"`` → ``{"a": "b", "c": "d e"}``. Quotes are stripped."""
    pairs = {}
    for key, raw in _ASSIGN.findall(str(text or "")):
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        pairs[key] = raw
    return pairs


# --- text view (``/gauge config``) -----------------------------------------


def display_value(field_key, value):
    if field_key.startswith("thresholds."):
        number = round(float(value) * 100, 1)
        return f"{int(number) if number == int(number) else number} %"
    if field_key == "alerts.enabled":
        return "on" if value else "off"
    if field_key == "alerts.on":
        return ", ".join(value) if value else "none"
    if field_key == "alerts.cooldown_minutes":
        return f"{value} min"
    if field_key == "output_reserve":
        return f"{value} tokens"
    return str(value)


def render_view_text(snapshot):
    lines = ["Hermes Gauge — preferences", ""]
    width = max(len(entry["key"]) for entry in snapshot["fields"])
    for entry in snapshot["fields"]:
        shown = display_value(entry["key"], entry["value"])
        lines.append(f"{entry['key'].ljust(width)}  {shown}  ({entry['source']})")
    lines.extend([
        "",
        "Change one with: /gauge config set alerts.route=home",
        "alerts.route: session (the thread the session lives in), home, or custom:<platform>:<chat_id>.",
        "Same file for Telegram and Desktop: hermes-gauge/config.yaml.",
    ])
    return "\n".join(lines)


def render_set_text(result):
    if result.get("ok"):
        lines = ["Saved."]
        for key, value in result["changed"].items():
            lines.append(f"{key} = {display_value(key, value)}")
        return "\n".join(lines)
    lines = ["Nothing was changed."]
    for key, error in result["errors"].items():
        label = "" if key == "_" else f"{key}: "
        lines.append(f"{label}{error['message']}")
    return "\n".join(lines)
