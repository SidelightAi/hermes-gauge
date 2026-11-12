"""Pure context-gauge math. Stdlib only — no database and no Hermes imports.

The headline number is an estimate, not a tokenizer count:

    estimated_tokens = (chars // 4) * 2

The ``* 2`` treats a stored reply as roughly mirrored by the rest of the
prompt. When the caller already measured the whole transcript, pass
``direct=True`` and the ``* 2`` is not applied. Either way the fill is capped
to 0–100%.
"""

import re
import time

CHARS_PER_TOKEN = 4
FILL_UNKNOWN = "\u2014"
DEFAULT_WATCH = 0.60
DEFAULT_FULL = 0.85
DEFAULT_FALLBACK_WINDOW = 200_000
WINDOW_LABEL_WEAKEST = "window: smallest model of the subject"
BREAKDOWN_KEYS = ("system_prompt", "tools", "skills", "conversation")


def chars_per_token_from(config):
    """``chars_per_token`` from a config dict. Missing or invalid → 4."""
    value = (config or {}).get("chars_per_token") if isinstance(config, dict) else None
    if isinstance(value, bool):
        return CHARS_PER_TOKEN
    try:
        per = int(value)
    except (TypeError, ValueError):
        return CHARS_PER_TOKEN
    if per < 1 or per > 8:
        return CHARS_PER_TOKEN
    return per


def estimate_tokens(chars, direct=False, chars_per_token=None):
    """Chars to tokens. Negative input counts as zero. Never negative.

    ``chars_per_token`` defaults to 4, which keeps ``(chars // 4) * 2``.
    """
    try:
        count = int(chars or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    per = chars_per_token if isinstance(chars_per_token, int) and chars_per_token >= 1 else CHARS_PER_TOKEN
    tokens = count // per
    if not direct:
        tokens *= 2
    return tokens


def effective_window(nominal, reserve):
    """Usable window after the reply reserve is deducted.

    A reserve of 0 leaves the nominal window unchanged, which is what the
    historical 200_000 comparison depends on.
    """
    if nominal is None:
        return None
    nominal = int(nominal)
    try:
        hold = int(reserve or 0)
    except (TypeError, ValueError):
        hold = 0
    if hold <= 0:
        return nominal
    return max(nominal - hold, 1)


# Long-context degradation: some models advertise a huge context window but
# degrade (memory holes, silent compression) well before it. The factor is
# the share of the nominal window that stays reliable; the gauge computes
# WATCH/FULL against the effective window, not the marketing number.
# One table, shared with dashboard/effective_window.py. First match wins.
# Flash is tested before mini and before pro, so "gemini" is not read as
# "mini" and gemini-3-flash stays at 0.25. A stored factor still wins.
_FAMILIES = (
    (
        "flash",
        0.25,
        re.compile(r"flashx|flash|lite|air|nano|gemini-.*-lite", re.IGNORECASE),
    ),
    (
        "mini",
        0.40,
        re.compile(r"4o-mini|o4-mini|(?<![A-Za-z])mini|glm-5\.2", re.IGNORECASE),
    ),
    (
        "haiku",
        0.50,
        re.compile(r"haiku", re.IGNORECASE),
    ),
    (
        "pro",
        1.0,
        re.compile(
            r"sonnet|opus|gpt-5|gpt-6|gemini-2\.5-pro|gemini-3|"
            r"glm-5\.3-prime|deepseek-r|qwen.*max",
            re.IGNORECASE,
        ),
    ),
)
UNKNOWN_FAMILY = "unknown"
UNKNOWN_FACTOR = 0.60


def model_family(model):
    """Return ``(family, factor)``. A name that matches nothing keeps 0.60."""
    text = model if isinstance(model, str) else ""
    for family, factor, pattern in _FAMILIES:
        if text and pattern.search(text):
            return family, factor
    return UNKNOWN_FAMILY, UNKNOWN_FACTOR


def scaled_window(usable, factor):
    """Apply a factor with integer hundredths so 0.40 and 0.60 do not drift.

    ``None`` stays ``None``. A usable window below 1 token becomes 1.
    """
    if usable is None:
        return None
    try:
        size = int(usable)
    except (TypeError, ValueError):
        return None
    if size < 1:
        size = 1
    try:
        hundredths = int(round(float(factor) * 100))
    except (TypeError, ValueError):
        hundredths = int(round(UNKNOWN_FACTOR * 100))
    if hundredths < 1:
        hundredths = 1
    return max((size * hundredths + 50) // 100, 1)


def clamp_factor(factor):
    """Factor in ``[0.05, 1]``, or None when the value is not a number."""
    try:
        value = float(factor)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    if value < 0.05:
        return 0.05
    if value > 1.0:
        return 1.0
    return value


def degradation_factor(model, config=None):
    """Reliable share of the nominal window for a model id.

    Priority: exact ``effective_models`` id, then ``effective_factors``
    substring (first match), then the shared family table. A name that
    matches no family keeps 0.60.
    """
    name = str(model or "").strip().lower()
    config = config or {}
    exact = config.get("effective_models") or {}
    if name and isinstance(exact, dict):
        for key, factor in exact.items():
            if str(key).strip().lower() == name:
                clamped = clamp_factor(factor)
                if clamped is not None:
                    return clamped
    overrides = config.get("effective_factors") or {}
    if name and isinstance(overrides, dict):
        try:
            for pattern, factor in overrides.items():
                if str(pattern).lower() in name:
                    clamped = clamp_factor(factor)
                    if clamped is not None:
                        return clamped
        except AttributeError:
            pass
    return model_family(model)[1]


def active_factors(model, config=None, stored=None):
    """Families and overrides that apply to ``model``, for a live check.

    ``stored`` is a database factor (session or model). It wins when set.
    Every built-in family is listed; ``matched`` names the family that hit.
    """
    config = config or {}
    name = str(model or "").strip().lower()
    family, family_factor = model_family(model)
    tiers = []
    for fam, fam_factor, _pattern in _FAMILIES:
        tiers.append({
            "factor": fam_factor,
            "family": fam,
            "keywords": [fam],
            "matched": [fam] if fam == family else [],
        })
    tiers.append({
        "factor": UNKNOWN_FACTOR,
        "family": UNKNOWN_FAMILY,
        "keywords": [],
        "matched": [UNKNOWN_FAMILY] if family == UNKNOWN_FAMILY else [],
    })
    source = "none"
    matched = None
    factor = family_factor
    stored_factor = clamp_factor(stored) if stored is not None else None
    if stored is not None and stored_factor is not None:
        source = "stored"
        matched = str(model or "") or None
        factor = stored_factor
    if source == "none" and name:
        exact = config.get("effective_models") or {}
        if isinstance(exact, dict):
            for key, value in exact.items():
                if str(key).strip().lower() == name:
                    clamped = clamp_factor(value)
                    if clamped is not None:
                        source = "effective_models"
                        matched = str(key)
                        factor = clamped
                        break
    if source == "none" and name:
        overrides = config.get("effective_factors") or {}
        if isinstance(overrides, dict):
            try:
                for pattern, value in overrides.items():
                    if str(pattern).lower() in name:
                        clamped = clamp_factor(value)
                        if clamped is not None:
                            source = "effective_factors"
                            matched = str(pattern)
                            factor = clamped
                            break
            except AttributeError:
                pass
    if source == "none":
        source = "family"
        matched = family
        factor = family_factor
    return {
        "model": str(model or ""),
        "factor": factor,
        "source": source,
        "matched": matched,
        "tiers": tiers,
    }


def clamp_ratio(estimated, window):
    """Fill ratio in ``[0, 1]``, or None when the window is unknown."""
    if window is None or window <= 0:
        return None
    if estimated is None or estimated < 0:
        estimated = 0
    ratio = estimated / window
    if ratio < 0:
        return 0.0
    if ratio > 1:
        return 1.0
    return ratio


def fill_percent(ratio):
    if ratio is None:
        return None
    percent = round(ratio * 100, 1)
    if percent < 0:
        return 0.0
    if percent > 100:
        return 100.0
    return percent


def band(ratio, watch=DEFAULT_WATCH, full=DEFAULT_FULL):
    if ratio is None:
        return "UNKNOWN"
    if ratio >= full:
        return "FULL"
    if ratio >= watch:
        return "WATCH"
    return "OK"


ROUTE_SESSION = "session"
# Where a push goes. ``session`` is the thread the session lives in (the
# historical behavior), ``home`` the home channel of the platform the session
# lives on, ``home:<platform>`` the home channel of a named platform, and
# ``custom:<platform>:<chat_id>`` one explicit chat (``hermes send`` target).
_ROUTE_PLATFORM = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ROUTE_CHAT = re.compile(r"^[A-Za-z0-9_.@:+#!-]{1,128}$")


def parse_route(value):
    """Parse ``alerts.route``. Returns a dict, or None when the value is not a route."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("", ROUTE_SESSION):
        return {"kind": "session", "platform": None, "target": None, "value": ROUTE_SESSION}
    if lowered == "home":
        return {"kind": "home", "platform": None, "target": None, "value": "home"}
    if lowered.startswith("home:"):
        platform = lowered[5:].strip()
        if not _ROUTE_PLATFORM.match(platform):
            return None
        return {"kind": "home", "platform": platform, "target": platform, "value": f"home:{platform}"}
    if lowered.startswith("custom:"):
        platform, _sep, chat = text[7:].partition(":")
        platform = platform.strip().lower()
        chat = chat.strip()
        if not _ROUTE_PLATFORM.match(platform) or not _ROUTE_CHAT.match(chat):
            return None
        if chat.startswith(":") or chat.endswith(":") or "::" in chat:
            return None
        return {
            "kind": "custom",
            "platform": platform,
            "target": f"{platform}:{chat}",
            "value": f"custom:{platform}:{chat}",
        }
    return None


def _alert_defaults():
    return {
        "enabled": True,
        "on": ["WATCH", "FULL"],
        "cooldown_minutes": 30,
        "route": ROUTE_SESSION,
    }


# "The exchange gets dull": share of the EFFECTIVE window (already reduced
# by the degradation factor) past which a model answers worse. Substring
# keys match the model id; ``default`` is the fallback. Clamped to 0.1–1.
# Restored from 50a5c6a / 88f4c2b. The plugin recommends; it never runs these.
DULL_AT_MIN = 0.1
DULL_AT_MAX = 1.0
DULL_AT_DEFAULTS = {"default": 0.70, "flash": 0.55, "mini": 0.60}
ON_DULL_DEFAULT = ["compact", "new_session"]
POLICY_COOLDOWN_DEFAULT = 60
_DULL_ACTION_WORDS = {
    "en": {"compact": "compact", "new_session": "new session"},
    "fr": {"compact": "compacter", "new_session": "nouvelle session"},
}


def _policy_defaults():
    return {
        "mode": "audit",
        "dull_at": dict(DULL_AT_DEFAULTS),
        "on_dull": list(ON_DULL_DEFAULT),
        "cooldown_minutes": POLICY_COOLDOWN_DEFAULT,
    }


def default_config():
    return {
        "thresholds": {"watch": DEFAULT_WATCH, "full": DEFAULT_FULL},
        "output_reserve": 0,
        "fallback_window": DEFAULT_FALLBACK_WINDOW,
        "models": {},
        "effective_models": {},
        "effective_factors": {},
        "chars_per_token": CHARS_PER_TOKEN,
        "language": "auto",
        "frozen_after_hours": 72,
        "gauge": {"alerts": _alert_defaults()},
        "policy": _policy_defaults(),
    }


def _split_inline_list(inner):
    """Split one flat ``[a, b]`` body. Quotes may contain commas; lists do not nest."""
    parts = []
    buf = []
    quote = ""
    for char in inner:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            continue
        if char == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def _coerce(value):
    if value in ("{}",):
        return {}
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_coerce(part.strip()) for part in _split_inline_list(inner)]
    if value.lower() in ("null", "~", ""):
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value


def parse_config(text):
    """Parse the small YAML subset used by ``config.yaml``.

    Maps, scalars, comments, ``{}``, booleans, and one inline list
    (``[WATCH, FULL]``). No anchors and no nested lists — the file is
    hand-written and PyYAML is not a plugin dependency.
    """
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce(val)
    return root


def _merge_alerts(base, parsed):
    """Alerts block. ``gauge.alerts`` is the shipped layout; a top-level
    ``alerts`` block is accepted as an alias and loses to ``gauge.alerts``."""
    alerts = _alert_defaults()
    for source in (base, parsed or {}):
        if not isinstance(source, dict):
            continue
        gauge = source.get("gauge")
        blocks = (
            source.get("alerts"),
            gauge.get("alerts") if isinstance(gauge, dict) else None,
        )
        for incoming in blocks:
            if not isinstance(incoming, dict):
                continue
            for key, value in incoming.items():
                if key == "on":
                    items = value if isinstance(value, (list, tuple)) else [value]
                    alerts["on"] = [
                        str(item).strip().upper()
                        for item in items
                        if str(item).strip()
                    ]
                else:
                    alerts[key] = value
    return alerts


def _merge_policy_raw(base, parsed):
    """Keep the raw ``policy`` map. Missing file → shipped defaults.

    Preset resolution happens later, so a partial map is stored as written
    and ``resolve_policy`` fills what the file left out.
    """
    raw = _policy_defaults()
    for source in (base, parsed or {}):
        if not isinstance(source, dict):
            continue
        incoming = source.get("policy")
        if isinstance(incoming, dict):
            raw = incoming
    return raw if isinstance(raw, dict) else _policy_defaults()


def merge_config(base, parsed):
    merged = default_config()
    merged.update({
        k: v for k, v in base.items()
        if k not in ("thresholds", "models", "gauge", "alerts", "policy")
    })
    thresholds = dict(default_config()["thresholds"])
    thresholds.update(base.get("thresholds") or {})
    thresholds.update((parsed or {}).get("thresholds") or {})
    models = dict(base.get("models") or {})
    models.update((parsed or {}).get("models") or {})
    for key, value in (parsed or {}).items():
        if key in ("thresholds", "models", "gauge", "alerts", "policy"):
            continue
        merged[key] = value
    merged["thresholds"] = thresholds
    merged["models"] = models
    merged["gauge"] = {"alerts": _merge_alerts(base, parsed)}
    merged["policy"] = _merge_policy_raw(base, parsed)
    return merged


def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def alert_settings(config):
    """Normalized ``gauge.alerts`` block. Missing keys keep the shipped defaults."""
    gauge = (config or {}).get("gauge") if isinstance(config, dict) else None
    alerts = gauge.get("alerts") if isinstance(gauge, dict) else None
    alerts = alerts if isinstance(alerts, dict) else {}
    raw_on = alerts.get("on", ["WATCH", "FULL"])
    if isinstance(raw_on, str):
        raw_on = [part.strip() for part in raw_on.split(",")]
    if not isinstance(raw_on, (list, tuple)):
        raw_on = ["WATCH", "FULL"]
    on = []
    for item in raw_on:
        name = str(item).strip().upper()
        if name and name not in on:
            on.append(name)
    try:
        cooldown = float(alerts.get("cooldown_minutes", 30))
    except (TypeError, ValueError):
        cooldown = 30.0
    if cooldown < 0:
        cooldown = 0.0
    route = parse_route(alerts.get("route", ROUTE_SESSION))
    return {
        "enabled": _as_bool(alerts.get("enabled", True), default=True),
        "on": on,
        "cooldown_minutes": cooldown,
        "route": route["value"] if route else ROUTE_SESSION,
    }


def percent_label(fill_percent):
    """Compact percent text: ``92``, ``84.9``, or None when the fill is unknown."""
    if fill_percent is None:
        return None
    try:
        number = round(float(fill_percent), 1)
    except (TypeError, ValueError):
        return None
    if number < 0:
        number = 0.0
    if number > 100:
        number = 100.0
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def clamp_dull(value):
    """Dull threshold in ``[0.1, 1]``, or None when the value is not a number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number < DULL_AT_MIN:
        return DULL_AT_MIN
    if number > DULL_AT_MAX:
        return DULL_AT_MAX
    return number


def _policy_map(config):
    raw = (config or {}).get("policy") if isinstance(config, dict) else None
    return raw if isinstance(raw, dict) else {}


def _coerce_mode(value):
    mode = str(value or "").strip().lower()
    if mode in ("audit", "act"):
        return mode
    return "audit"


def _coerce_on_dull(value):
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return list(ON_DULL_DEFAULT)
    items = []
    for item in value:
        name = str(item).strip()
        if name and name not in items:
            items.append(name)
    return items or list(ON_DULL_DEFAULT)


def _coerce_cooldown(value, default=POLICY_COOLDOWN_DEFAULT):
    try:
        cooldown = float(value)
    except (TypeError, ValueError):
        return float(default)
    if cooldown < 0:
        return 0.0
    return cooldown


# Named bundles. ``standard`` matches the shipped defaults. ``prudent``
# warns earlier and still only recommends — it never switches to act.
BUILTIN_PRESETS = {
    "standard": {
        "mode": "audit",
        "dull_at": dict(DULL_AT_DEFAULTS),
        "on_dull": list(ON_DULL_DEFAULT),
        "cooldown_minutes": POLICY_COOLDOWN_DEFAULT,
    },
    "prudent": {
        "mode": "audit",
        "dull_at": {"default": 0.55, "flash": 0.40, "mini": 0.45},
        "on_dull": list(ON_DULL_DEFAULT),
        "cooldown_minutes": 30,
    },
}
_POLICY_KEYS = ("mode", "dull_at", "on_dull", "cooldown_minutes")


def _copy_preset(body):
    if not isinstance(body, dict):
        return {}
    copied = {}
    if "mode" in body:
        copied["mode"] = body["mode"]
    if isinstance(body.get("dull_at"), dict):
        copied["dull_at"] = {
            str(key).strip(): value
            for key, value in body["dull_at"].items()
            if str(key).strip()
        }
    if "on_dull" in body:
        copied["on_dull"] = body["on_dull"]
    if "cooldown_minutes" in body:
        copied["cooldown_minutes"] = body["cooldown_minutes"]
    return copied


def _preset_catalog(raw):
    """Built-in presets, then the YAML ``presets:`` map overlaid per key."""
    catalog = {name: _copy_preset(body) for name, body in BUILTIN_PRESETS.items()}
    custom = raw.get("presets") if isinstance(raw.get("presets"), dict) else {}
    for name, body in custom.items():
        key = str(name).strip().lower()
        if not key or not isinstance(body, dict):
            continue
        merged = _copy_preset(catalog.get(key) or {})
        overlay = _copy_preset(body)
        if "mode" in overlay:
            merged["mode"] = overlay["mode"]
        if "dull_at" in overlay:
            dull = dict(merged.get("dull_at") or {})
            dull.update(overlay["dull_at"])
            merged["dull_at"] = dull
        if "on_dull" in overlay:
            merged["on_dull"] = overlay["on_dull"]
        if "cooldown_minutes" in overlay:
            merged["cooldown_minutes"] = overlay["cooldown_minutes"]
        catalog[key] = merged
    return catalog


def _apply_policy_layer(state, sources, body, source):
    if "mode" in body:
        state["mode"] = _coerce_mode(body.get("mode"))
        sources["mode"] = source
    dull_in = body.get("dull_at")
    if isinstance(dull_in, dict):
        for key, value in dull_in.items():
            name = str(key).strip()
            if not name:
                continue
            clamped = clamp_dull(value)
            if clamped is None:
                continue
            state["dull_at"][name] = clamped
            sources[f"dull_at.{name}"] = source
    if "on_dull" in body:
        state["on_dull"] = _coerce_on_dull(body.get("on_dull"))
        sources["on_dull"] = source
    if "cooldown_minutes" in body:
        state["cooldown_minutes"] = _coerce_cooldown(body.get("cooldown_minutes"))
        sources["cooldown_minutes"] = source


def _is_french(lang):
    return str(lang or "").strip().lower().startswith("fr")


def _policy_effect(mode, applied, known, requested, lang="en"):
    french = _is_french(lang)
    if mode == "act":
        text = (
            "Mode act : au franchissement du seuil débile, le plugin pousse "
            "une notification « action recommandée » (compacter ou ouvrir une "
            "nouvelle session). Il ne l'exécute pas."
            if french else
            "Act mode: when the dull threshold is crossed, the plugin pushes a "
            "\"recommended action\" notification (compact or open a new "
            "session). It does not run it."
        )
    else:
        text = (
            "Mode audit : le plugin observe et recommande seulement. "
            "Au franchissement du seuil débile, une notification de "
            "recommandation part une fois, puis le cooldown la retient."
            if french else
            "Audit mode: the plugin only observes and recommends. When the "
            "dull threshold is crossed, one recommendation notification goes "
            "out, then the cooldown holds it back."
        )
    text += (
        " Il ne compacte jamais une session et n'en ouvre jamais une "
        "nouvelle, dans aucun mode."
        if french else
        " It never compacts a session and never opens a new one, in any mode."
    )
    if not known:
        text += (
            f" Le preset « {requested} » est inconnu : "
            f"« {applied} » s'applique à la place."
            if french else
            f" The preset \"{requested}\" is unknown: "
            f"\"{applied}\" applies instead."
        )
    return text


def resolve_policy(config, lang="en"):
    """Active policy: defaults, then the named preset, then explicit keys.

    ``mode`` other than ``audit`` or ``act`` stays ``audit``. Dull
    thresholds are clamped to 0.1–1. An unknown preset falls back to
    ``standard``. Nothing in this object is an action the plugin will run.
    ``sources`` names where each value came from: ``default``,
    ``preset:<name>``, or ``config``.
    """
    raw = _policy_map(config)
    requested = str(raw.get("preset") or "standard").strip().lower() or "standard"
    catalog = _preset_catalog(raw)
    known = requested in catalog
    applied = requested if known else "standard"
    sources = {}
    state = {
        "mode": "audit",
        "dull_at": dict(DULL_AT_DEFAULTS),
        "on_dull": list(ON_DULL_DEFAULT),
        "cooldown_minutes": float(POLICY_COOLDOWN_DEFAULT),
    }
    sources["mode"] = "default"
    for key in state["dull_at"]:
        sources[f"dull_at.{key}"] = "default"
    sources["on_dull"] = "default"
    sources["cooldown_minutes"] = "default"
    _apply_policy_layer(state, sources, catalog.get(applied) or {}, f"preset:{applied}")
    explicit = {key: raw[key] for key in _POLICY_KEYS if key in raw}
    _apply_policy_layer(state, sources, explicit, "config")
    return {
        "mode": state["mode"],
        "dull_at": state["dull_at"],
        "on_dull": state["on_dull"],
        "cooldown_minutes": state["cooldown_minutes"],
        "preset": requested,
        "preset_applied": applied,
        "preset_known": known,
        "sources": sources,
        "effect": _policy_effect(state["mode"], applied, known, requested, lang),
    }


def matched_dull(model, dull_map):
    """``(threshold, label)``. Longest substring wins; ``default`` is last."""
    mapping = dull_map if isinstance(dull_map, dict) else {}
    name = str(model or "").strip().lower()
    best_key = None
    best_value = None
    for key, value in mapping.items():
        token = str(key).strip().lower()
        if not token or token == "default" or token not in name:
            continue
        if best_key is None or len(token) > len(best_key):
            best_key = token
            best_value = value
    if best_key is not None:
        clamped = clamp_dull(best_value)
        if clamped is not None:
            return clamped, best_key
    fallback = clamp_dull(mapping.get("default", DULL_AT_DEFAULTS["default"]))
    if fallback is None:
        fallback = DULL_AT_DEFAULTS["default"]
    return fallback, "default"


def _dull_actions_phrase(on_dull, lang="en"):
    french = _is_french(lang)
    table = _DULL_ACTION_WORDS["fr" if french else "en"]
    words = [table.get(item, item) for item in on_dull or []]
    if not words:
        words = [table["compact"]]
    phrase = words[0]
    for word in words[1:]:
        phrase += (", puis " if french else ", then ") + word
    return phrase


def policy_push_message(session_id, verdict, policy, lang="en"):
    """One notification. Wording depends on mode; neither mode executes it.

    ``audit`` repeats the recommendation. ``act`` labels the same push as
    an explicit recommended action. Hermes core is the only thing that
    can compact or open a session.
    """
    french = _is_french(lang)
    if verdict.get("mode") == "act":
        actions = list((policy or {}).get("on_dull") or ON_DULL_DEFAULT)
        if french:
            names = {"compact": "compacte", "new_session": "nouvelle session"}
            suffix, generic = " recommandée", "action recommandée"
        else:
            names = {"compact": "compact", "new_session": "new session"}
            suffix, generic = " recommended", "action recommended"
        if actions == ["compact", "new_session"]:
            asked = " / ".join((names["compact"], names["new_session"])) + suffix
        else:
            parts = [names.get(item, item) for item in actions]
            asked = (" / ".join(parts) + suffix) if parts else generic
        if french:
            return (
                f"Hermes Gauge — action recommandée — session {session_id} : "
                f"{asked}. {verdict.get('recommendation')}"
            )
        return (
            f"Hermes Gauge — recommended action — session {session_id}: "
            f"{asked}. {verdict.get('recommendation')}"
        )
    if french:
        return f"Hermes Gauge — session {session_id} : {verdict.get('recommendation')}"
    return f"Hermes Gauge — session {session_id}: {verdict.get('recommendation')}"


def policy_verdict(model, fill_percent, config, lang="en"):
    """What to tell the user. Never an instruction the plugin will run.

    ``fill_percent`` is already the fill of the effective window
    (``compose`` scores WATCH/FULL against that window).
    """
    french = _is_french(lang)
    policy = resolve_policy(config, lang)
    threshold, label = matched_dull(model, policy["dull_at"])
    dull_txt = percent_label(round(threshold * 100, 1)) or "0"
    if label == "default":
        model_bit = ""
    else:
        model_bit = f" pour ce modèle {label}" if french else f" for this model {label}"
    actions = _dull_actions_phrase(policy["on_dull"], lang)
    if fill_percent is None:
        return {
            "mode": policy["mode"],
            "dull_at_applied": threshold,
            "fill_effective_pct": None,
            "dull_reached": False,
            "recommendation": (
                f"Fenêtre non confirmée, seuil débile {dull_txt} %{model_bit} : "
                "pas évalué."
                if french else
                f"Window not confirmed, dull threshold {dull_txt}%{model_bit}: "
                "not evaluated."
            ),
        }
    try:
        fill_value = float(fill_percent)
    except (TypeError, ValueError):
        fill_value = 0.0
    if fill_value < 0:
        fill_value = 0.0
    if fill_value > 100:
        fill_value = 100.0
    reached = (fill_value / 100.0) >= threshold
    fill_txt = percent_label(round(fill_value, 1)) or "0"
    if reached:
        recommendation = (
            f"{fill_txt} % effectif, seuil débile {dull_txt} %{model_bit} : "
            f"recommandé = {actions}."
            if french else
            f"{fill_txt}% effective, dull threshold {dull_txt}%{model_bit}: "
            f"recommended = {actions}."
        )
    else:
        recommendation = (
            f"{fill_txt} % effectif, seuil débile {dull_txt} %{model_bit} : "
            f"pas encore atteint (recommandé = {actions})."
            if french else
            f"{fill_txt}% effective, dull threshold {dull_txt}%{model_bit}: "
            f"not reached yet (recommended = {actions})."
        )
    return {
        "mode": policy["mode"],
        "dull_at_applied": threshold,
        "fill_effective_pct": fill_value,
        "dull_reached": reached,
        "recommendation": recommendation,
    }


def resolve_nominal(model, session_override, detected, model_override, config, unresolved=False):
    """Pick a nominal window and where it came from.

    Detected ``context_length`` beats a config-file preset. An exact model id
    in config is ``configured``; a substring or alias is never consulted.
    A session marked unresolved does not silently take the fallback number —
    that invented window is what made Hermes #139492 a false alarm.
    """
    if session_override:
        return int(session_override), "configured"
    if detected:
        return int(detected), "detected"
    if model_override:
        return int(model_override), "configured"
    models = (config or {}).get("models") or {}
    if model:
        wanted = str(model).strip().lower()
        for name, window in models.items():
            if str(name).strip().lower() == wanted and window:
                return int(window), "configured"
    if unresolved:
        return None, "unresolved"
    fallback = (config or {}).get("fallback_window")
    if fallback:
        return int(fallback), "fallback"
    return None, "unresolved"


def weakest_link(agents):
    """Status window for a subject with several models.

    ``agents`` is a list of ``{"window": int | None, "source": str}``.
    The subject fill uses the sum of contributions elsewhere; the window
    itself is the smallest known context, or unresolved if any link has
    no window yet.
    """
    if not agents or len(agents) < 2:
        return None
    if any(agent.get("window") is None for agent in agents):
        return {
            "window": None,
            "source": "unresolved",
            "label": WINDOW_LABEL_WEAKEST,
        }
    weakest = min(agents, key=lambda agent: agent["window"])
    return {
        "window": weakest["window"],
        "source": weakest.get("source") or "fallback",
        "label": WINDOW_LABEL_WEAKEST,
    }


def breakdown_tokens(char_parts, total, direct=False, chars_per_token=None):
    """Token split that adds up to ``total``. Missing detail stays in conversation."""
    parts = char_parts or {}
    raw = {
        key: estimate_tokens(parts.get(key, 0), direct=direct, chars_per_token=chars_per_token)
        for key in BREAKDOWN_KEYS
    }
    if sum(raw.values()) == 0:
        raw["conversation"] = total or 0
        return raw
    gap = (total or 0) - sum(raw.values())
    raw["conversation"] = max(raw["conversation"] + gap, 0)
    return raw


def message_text(message):
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return str(message)
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                chunks.append(str(block.get("text") or block.get("content") or ""))
        text = "".join(chunks)
    elif content is None:
        text = ""
    else:
        text = str(content)
    if message.get("tool_calls"):
        text += str(message.get("tool_calls"))
    return text


def measure_messages(messages):
    """Split a transcript into system prompt / tools / skills / conversation chars."""
    buckets = {key: 0 for key in BREAKDOWN_KEYS}
    for message in messages or []:
        text = message_text(message)
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "").lower()
            source = str(message.get("source") or message.get("category") or "").lower()
        else:
            role = ""
            source = ""
        if source in ("skill", "skills") or role in ("skill", "skills"):
            bucket = "skills"
        elif role in ("tool", "function") or (
            isinstance(message, dict) and message.get("tool_call_id") and role != "assistant"
        ):
            bucket = "tools"
        elif role in ("system", "developer"):
            bucket = "system_prompt"
        elif isinstance(message, dict) and message.get("tool_calls") and role == "assistant":
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            buckets["conversation"] += len(content)
            buckets["tools"] += len(str(message.get("tool_calls")))
            continue
        else:
            bucket = "conversation"
        buckets[bucket] += len(text)
    return sum(buckets.values()), buckets


def _message_role(message):
    if not isinstance(message, dict):
        return ""
    return str(message.get("role") or message.get("type") or "").lower()


def _content_blocks(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _result_chars(content):
    """Length of a tool_result body. No token estimate, no model."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, str):
                total += len(item)
            elif isinstance(item, dict):
                if item.get("text") is not None:
                    total += _result_chars(item.get("text"))
                elif item.get("content") is not None:
                    total += _result_chars(item.get("content"))
                else:
                    total += len(str(item))
            else:
                total += len(str(item))
        return total
    if isinstance(content, dict):
        if content.get("content") is not None:
            return _result_chars(content.get("content"))
        if content.get("text") is not None:
            return _result_chars(content.get("text"))
        return len(str(content))
    return len(str(content))


def _call_name(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("name", "tool_name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    function = obj.get("function")
    if isinstance(function, dict):
        value = function.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _call_id(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("id", "tool_call_id", "tool_use_id"):
        value = obj.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _tool_result_carrier(message):
    """A user turn boundary is a real user message, not a tool_result wrapper."""
    role = _message_role(message)
    if role in ("tool", "function"):
        return True
    blocks = _content_blocks(message)
    if not blocks:
        return False
    return all(str(block.get("type") or "") == "tool_result" for block in blocks)


def _messages_this_turn(messages):
    """Messages after the last plain user message. That slice is this turn."""
    start = 0
    for index, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and _message_role(message) == "user"
            and not _tool_result_carrier(message)
        ):
            start = index + 1
    return messages[start:]


def extract_turn_tools(messages=None, tool_calls=None, tool_results=None):
    """Named tools and result lengths from one turn's payload.

    Counts and string lengths only. A missing name is not guessed: the
    result is ``attribution: turn-only`` and ``tools`` stays empty.
    ``tool-level`` means every returned name was read off the payload.
    Restored from 7f3b9c5 / 111eb15.
    """
    calls = []
    results = []
    saw_payload = False

    def note_call(obj):
        nonlocal saw_payload
        saw_payload = True
        if not isinstance(obj, dict):
            return
        cid = _call_id(obj)
        name = _call_name(obj)
        if cid:
            for existing in calls:
                if existing["id"] == cid:
                    if name and not existing["name"]:
                        existing["name"] = name
                    return
        calls.append({"id": cid, "name": name})

    def note_result(obj, chars):
        nonlocal saw_payload
        saw_payload = True
        cid = _call_id(obj) if isinstance(obj, dict) else None
        name = _call_name(obj) if isinstance(obj, dict) else None
        size = max(int(chars or 0), 0)
        if cid:
            for existing in results:
                if existing["id"] == cid:
                    if name and not existing["name"]:
                        existing["name"] = name
                    if size > existing["chars"]:
                        existing["chars"] = size
                    return
        results.append({"id": cid, "name": name, "chars": size})

    if isinstance(tool_calls, list):
        for call in tool_calls:
            note_call(call)
    if isinstance(tool_results, list):
        for result in tool_results:
            if isinstance(result, dict):
                note_result(result, _result_chars(result.get("content")))
            else:
                note_result(None, _result_chars(result))
    if isinstance(messages, list):
        saw_payload = True
        for message in _messages_this_turn(messages):
            if not isinstance(message, dict):
                continue
            role = _message_role(message)
            if role in ("tool", "function"):
                note_result(message, _result_chars(message.get("content")))
                continue
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    note_call(call)
            for block in _content_blocks(message):
                kind = str(block.get("type") or "")
                if kind == "tool_use":
                    note_call(block)
                elif kind == "tool_result":
                    note_result(block, _result_chars(block.get("content")))

    if not saw_payload:
        return {"attribution": "turn-only", "tools": []}

    names_by_id = {
        call["id"]: call["name"]
        for call in calls
        if call.get("id") and call.get("name")
    }
    tools = []
    consumed = set()
    if results:
        for result in results:
            name = result["name"]
            if not name and result["id"]:
                name = names_by_id.get(result["id"])
            if not name:
                continue
            if result["id"]:
                consumed.add(result["id"])
            tools.append({
                "tool_name": name,
                "result_chars": result["chars"],
            })
        for call in calls:
            if not call.get("name"):
                continue
            if call.get("id") and call["id"] in consumed:
                continue
            if call.get("id"):
                tools.append({"tool_name": call["name"], "result_chars": 0})
    else:
        for call in calls:
            if call.get("name"):
                tools.append({"tool_name": call["name"], "result_chars": 0})
    if not tools:
        return {"attribution": "turn-only", "tools": []}
    return {"attribution": "tool-level", "tools": tools}


STATE_ORDER = {"active": 0, "frozen": 1, "archived": 2}


def session_label(session):
    """The readable name when there is one, else the id."""
    return session.get("name") or session.get("session_id")


def state_counts(sessions):
    """``{"active": n, "frozen": n, "archived": n}`` for sessions that carry a state."""
    counts = {state: 0 for state in STATE_ORDER}
    for session in sessions or []:
        state = session.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def sort_by_state(sessions):
    """Active first, then frozen, then archived. Order inside a state is kept."""
    return sorted(sessions or [], key=lambda s: STATE_ORDER.get(s.get("state"), 0))


def weakest_session(sessions):
    """The session with the highest known fill. Ties break toward FULL, then WATCH.

    Sessions that someone is still working in come first: a frozen or archived
    session cannot be the link that breaks now. When none is active, every
    session competes.
    """
    ranked = []
    for session in sessions or []:
        fill = session.get("fill_percent")
        if isinstance(fill, bool) or not isinstance(fill, (int, float)):
            continue
        ranked.append(session)
    live = [s for s in ranked if s.get("state") in (None, "active")]
    if live:
        ranked = live
    if not ranked:
        return None
    band_rank = {"FULL": 2, "WATCH": 1, "OK": 0}
    return max(
        ranked,
        key=lambda session: (
            float(session["fill_percent"]),
            band_rank.get(session.get("status"), -1),
        ),
    )


def summary_line(sessions):
    """One line: how many sessions, and which one is closest to full."""
    count = len(sessions or [])
    noun = "session" if count == 1 else "sessions"
    counts = state_counts(sessions)
    if counts["frozen"] or counts["archived"]:
        parts = [f"{counts[state]} {state}" for state in STATE_ORDER if counts[state]]
        head = f"{count} {noun} ({', '.join(parts)})"
    else:
        head = f"{count} active {noun}"
    link = weakest_session(sessions)
    if link is None:
        return f"{head} — weakest link: none"
    label = percent_label(link.get("fill_percent"))
    return (
        f"{head} — weakest link: "
        f"{session_label(link)} {label}% {link.get('status')}"
    )


def _stamp(generated_at):
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(generated_at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _activity_stamp(value):
    if value is None:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _has_state(sessions):
    return any("state" in s or "last_activity" in s for s in sessions or [])


def _md_cell(value):
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")


def _fill_cell(session):
    if session.get("fill_percent") is None:
        return FILL_UNKNOWN
    return f"{percent_label(session.get('fill_percent'))}%"


def render_report_text(sessions, generated_at):
    """Plain report. The summary line is first, then one row per session."""
    lines = [summary_line(sessions), ""]
    with_state = _has_state(sessions)
    for session in sort_by_state(sessions):
        row = (
            f"{session_label(session)}  {_fill_cell(session)}  "
            f"{session.get('status')}  turns {session.get('turns')}  "
            f"{session.get('model')}"
        )
        if with_state:
            row += (
                f"  last {_activity_stamp(session.get('last_activity'))}"
                f"  {session.get('state') or 'active'}"
            )
        lines.append(row)
    lines.extend(["", f"Generated {_stamp(generated_at)}"])
    return "\n".join(lines)


def render_report_markdown(sessions, generated_at):
    """Markdown report for a chat attachment. The weakest link is underlined."""
    link = weakest_session(sessions)
    summary = summary_line(sessions)
    if link is not None:
        plain = (
            f"{session_label(link)} {percent_label(link.get('fill_percent'))}% "
            f"{link.get('status')}"
        )
        summary = summary.replace(plain, f"<u>**{plain}**</u>", 1)
    with_state = _has_state(sessions)
    header = ["Session", "Fill", "Status", "Turns", "Model"]
    if with_state:
        header += ["Last activity", "State"]
    lines = [
        "# Hermes Gauge",
        "",
        summary,
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    link_id = link.get("session_id") if link is not None else None
    for session in sort_by_state(sessions):
        sid = _md_cell(session_label(session))
        if session.get("session_id") == link_id:
            sid = f"<u>**{sid}**</u>"
        cells = [
            sid,
            _md_cell(_fill_cell(session)),
            _md_cell(session.get("status")),
            _md_cell(session.get("turns")),
            _md_cell(session.get("model")),
        ]
        if with_state:
            cells += [
                _md_cell(_activity_stamp(session.get("last_activity"))),
                _md_cell(session.get("state") or "active"),
            ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", f"_Generated {_stamp(generated_at)}_", ""])
    return "\n".join(lines)


# Nine pills along the bottom arc, left to right. Coordinates match the
# segmented arc in assets/icon.svg.
_PILLS = (
    (13.38, 61.34, 65.00),
    (19.28, 70.39, 48.75),
    (27.48, 77.42, 32.50),
    (37.31, 81.87, 16.25),
    (48.00, 83.40, 0.00),
    (58.69, 81.87, -16.25),
    (68.52, 77.42, -32.50),
    (76.72, 70.39, -48.75),
    (82.62, 61.34, -65.00),
)
PILL_COUNT = len(_PILLS)
COLOR_GROUND = "#032A27"
COLOR_LIT = "#E8B54A"
COLOR_FULL = "#C45C48"
COLOR_DIM = "#4A6157"


def lit_pills(percent, total=PILL_COUNT):
    """How many pills are on. Any positive fill lights at least one; 100% lights all."""
    if percent is None:
        return 0
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if value >= 100:
        return total
    count = int(round(value / 100.0 * total))
    if count < 1:
        return 1
    if count > total:
        return total
    return count


def _xml(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _pill(x, y, angle, color, glow=False):
    opacity = ' fill-opacity="0.7"' if glow else ""
    return (
        f'<rect x="-3.02" y="-4.70" width="6.05" height="9.40" rx="3.02" ry="3.02" '
        f'transform="translate({x:.2f} {y:.2f}) rotate({angle:.2f})" '
        f'fill="{color}"{opacity}/>'
    )


def render_svg(fill_percent, status, session_id=None):
    """Standalone gauge: deep-green ground, amber pills, terracotta when FULL.

    No external asset. Unlit pills stay sage. The arc is the indicator.
    """
    lit = lit_pills(fill_percent)
    active = COLOR_FULL if status == "FULL" else COLOR_LIT
    label = percent_label(fill_percent)
    if label is None:
        title = "Hermes Gauge"
    elif session_id:
        title = f"Hermes Gauge — {session_id} {label}% {status}"
    else:
        title = f"Hermes Gauge — {label}% {status}"
    glow = []
    solid = []
    for index, (x, y, angle) in enumerate(_PILLS):
        on = index < lit
        color = active if on else COLOR_DIM
        solid.append(_pill(x, y, angle, color))
        if on:
            glow.append(_pill(x, y, angle, color, glow=True))
    glow_markup = "\n".join(glow)
    solid_markup = "\n".join(solid)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" '
        'viewBox="0 0 96 96" role="img">\n'
        f"<title>{_xml(title)}</title>\n"
        "<defs>\n"
        '  <filter id="gauge-glow" x="-60%" y="-60%" width="220%" height="220%" '
        'color-interpolation-filters="sRGB">\n'
        '    <feGaussianBlur stdDeviation="1.2"/>\n'
        "  </filter>\n"
        "</defs>\n"
        f'<rect x="0.08" y="0.08" width="95.84" height="95.84" rx="28" fill="{COLOR_GROUND}"/>\n'
        '<circle cx="48.00" cy="45.20" r="32.60" fill="none" stroke="#8AA392" '
        'stroke-width="1.15" opacity="0.85"/>\n'
        f'<g filter="url(#gauge-glow)">\n{glow_markup}\n</g>\n'
        f"<g>\n{solid_markup}\n</g>\n"
        "</svg>\n"
    )


def compose(estimated, nominal, reserve, source, anchor_valid, watch, full,
            approximate=True, degrade=1.0):
    """Build the public fill fields. Unknown anchor or unknown max → em dash.

    ``degrade`` is the reliable share of the nominal window (1.0 = no
    degradation). WATCH/FULL and the remaining tokens are computed against
    ``effective = usable * degrade``.
    """
    usable = effective_window(nominal, reserve)
    try:
        degrade = max(min(float(degrade or 1.0), 1.0), 0.05)
    except (TypeError, ValueError):
        degrade = 1.0
    effective = scaled_window(usable, degrade)
    unknown = (not anchor_valid) or source == "unresolved" or effective is None
    if unknown:
        return {
            "window_tokens": None,
            "nominal_window": nominal,
            "effective_window": None,
            "degrade_factor": degrade,
            "output_reserve": int(reserve or 0),
            # A window that is known stays named when only the count is waiting
            # (after a summary). Only a window nobody could resolve is "unresolved".
            "window_source": "unresolved" if (source == "unresolved" or effective is None) else source,
            "estimated_tokens": estimated,
            "fill_percent": None,
            "fill_display": FILL_UNKNOWN,
            "remaining_tokens": None,
            "status": "UNKNOWN",
            "approximate": True,
            "anchor_valid": bool(anchor_valid) and source != "unresolved" and effective is not None,
        }
    ratio = clamp_ratio(estimated, effective)
    percent = fill_percent(ratio)
    remaining = (effective or 0) - (estimated or 0)
    if remaining < 0:
        remaining = 0
    status = band(ratio, watch, full)
    if approximate:
        display = f"≈{percent}%"
    else:
        display = f"{percent}%"
    return {
        "window_tokens": effective,
        "nominal_window": nominal,
        "effective_window": effective,
        "degrade_factor": degrade,
        "output_reserve": int(reserve or 0),
        "window_source": source,
        "estimated_tokens": estimated if estimated is not None else 0,
        "fill_percent": percent,
        "fill_display": display,
        "remaining_tokens": remaining,
        "status": status,
        "approximate": bool(approximate),
        "anchor_valid": True,
    }
