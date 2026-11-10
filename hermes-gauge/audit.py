"""Context audit: peaks, compactions and what the context is made of.

Pure functions over data the plugin already holds. ``tools.gauge_audit`` gathers
the inputs and calls ``build``; the Desktop panel and ``/gauge audit`` both show
what ``build`` returns. Standard library only, never writes.

A compaction empties the live counter, so the live gauge cannot tell you what a
session went through. Each compaction leaves a snapshot of the state just before
it; those snapshots are the peak history.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path


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


gauge_math = _sibling("gauge_math")
sessions_mod = _sibling("sessions")

KIND_SNAPSHOT = "snapshot"
KIND_RESET = "reset"
KIND_COMPACTION = "compaction"

PARTS = gauge_math.BREAKDOWN_KEYS  # system_prompt, tools, skills, conversation
DISPLAY_ORDER = ("conversation", "tools", "system_prompt", "skills")

LABELS = {
    "en": {
        "title": "Context audit",
        "sessions": "sessions", "session": "session",
        "compactions": "compactions", "compaction": "compaction",
        "resets": "resets", "reset": "reset",
        "highest": "Highest peak", "none": "none",
        "now": "now", "peak": "Peak", "last": "last",
        "composition": "Composition",
        "where": "Where the context goes",
        "parts": {
            "conversation": "conversation", "tools": "tools",
            "system_prompt": "system", "skills": "skills",
        },
        "states": {"active": "active", "frozen": "frozen", "archived": "archived"},
        "tokens": "tokens",
        "no_data": "No session recorded yet.",
        "no_compaction": "no compaction",
        "note": "A compaction resets the live counter; this audit is where past peaks stay.",
        "hdr": ["Session", "State", "Now", "Peak", "Compactions", "Resets", "Conversation", "Tools", "System", "Skills"],
        "undated": "undated",
    },
    "fr": {
        "title": "Audit du contexte",
        "sessions": "sessions", "session": "session",
        "compactions": "compactages", "compaction": "compactage",
        "resets": "remises à zéro", "reset": "remise à zéro",
        "highest": "Plus haut pic", "none": "aucun",
        "now": "maintenant", "peak": "Pic", "last": "dernier",
        "composition": "Composition",
        "where": "Où part le contexte",
        "parts": {
            "conversation": "conversation", "tools": "outils",
            "system_prompt": "système", "skills": "skills",
        },
        "states": {"active": "active", "frozen": "figée", "archived": "archivée"},
        "tokens": "jetons",
        "no_data": "Aucune session enregistrée pour l'instant.",
        "no_compaction": "aucun compactage",
        "note": "Un compactage remet le compteur à zéro ; cet audit garde la trace des pics passés.",
        "hdr": ["Session", "État", "Maintenant", "Pic", "Compactages", "RAZ", "Conversation", "Outils", "Système", "Skills"],
        "undated": "sans date",
    },
}


def labels(lang):
    return LABELS.get(lang) or LABELS["en"]


def event_kind(reason):
    """``snapshot``, ``reset`` or ``compaction``.

    A snapshot is the in-flight copy taken when a session enters WATCH. Nothing
    happened to the session: it feeds the peaks and nothing else. A reset is the
    user starting a fresh session. Anything else Hermes logs is a compaction.
    """
    text = str(reason or "").strip().lower()
    if text == sessions_mod.WATCH_REASON:
        return KIND_SNAPSHOT
    return KIND_RESET if text in sessions_mod.RESET_REASONS else KIND_COMPACTION


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(percent, watch, full):
    if percent is None:
        return "UNKNOWN"
    return gauge_math.band(percent / 100.0, watch, full)


def composition(breakdown):
    """``{part: {"tokens": n, "share": percent}}`` or None when there is nothing to split."""
    if not isinstance(breakdown, dict):
        return None
    tokens = {key: max(int(_number(breakdown.get(key)) or 0), 0) for key in PARTS}
    total = sum(tokens.values())
    if total <= 0:
        return None
    return {
        key: {"tokens": tokens[key], "share": round(100.0 * tokens[key] / total, 1)}
        for key in PARTS
    }


def infer_window(entries):
    """A window read off snapshots that knew both their tokens and their fill.

    Right after a compaction the live window is unknown until Hermes reports it
    again, so a later snapshot can have tokens and no percentage. Another
    snapshot of the same session usually has both; ``tokens / percent`` gives the
    window back. None when no snapshot can say.
    """
    sizes = sorted(
        e["estimated_tokens"] / e["fill_percent"] * 100.0
        for e in entries
        if e.get("estimated_tokens") and e.get("fill_percent") and e["fill_percent"] > 0
    )
    if not sizes:
        return None
    return int(round(sizes[len(sizes) // 2] / 1000.0)) * 1000 or None


def history(events, window, watch, full):
    """Chronological peaks, one per compaction, reset or WATCH snapshot.

    Returns ``(entries, window_used)``. A snapshot that lost its percentage gets
    one from ``window``, or from a window inferred from its siblings; those
    entries carry ``inferred: True``.
    """
    raw = []
    for event in events or []:
        snap = event.get("snapshot") or {}
        tokens = _number(snap.get("estimated_tokens"))
        raw.append({
            "at": _number(event.get("created")),
            "kind": event_kind(event.get("reason")),
            "reason": event.get("reason"),
            "estimated_tokens": None if tokens is None else int(tokens),
            "fill_percent": _number(snap.get("fill_percent")),
            "inferred": False,
            "turns": snap.get("turns"),
            "model": snap.get("model"),
            "composition": composition(snap.get("breakdown")),
        })
    used = window or infer_window(raw)
    for entry in raw:
        if entry["fill_percent"] is None and entry["estimated_tokens"] is not None and used:
            entry["fill_percent"] = gauge_math.fill_percent(
                gauge_math.clamp_ratio(entry["estimated_tokens"], used)
            )
            entry["inferred"] = True
        entry["status"] = _status(entry["fill_percent"], watch, full)
    return raw, used


def _peak(entries, current, series, window, window_known, watch, full):
    """The highest point we know of, and where we learned it."""
    candidates = []
    for entry in entries:
        if entry["estimated_tokens"] is not None:
            candidates.append((
                entry["estimated_tokens"], entry["fill_percent"], entry["at"],
                entry["kind"], entry["inferred"],
            ))
    if current and current.get("estimated_tokens") is not None:
        candidates.append((current["estimated_tokens"], current.get("fill_percent"), None, "now", False))
    for stamp, tokens in series or []:
        percent = None
        if window:
            percent = gauge_math.fill_percent(gauge_math.clamp_ratio(tokens, window))
        candidates.append((tokens, percent, stamp, "turns", percent is not None and not window_known))
    if not candidates:
        return None
    tokens, percent, at, source, inferred = max(
        candidates, key=lambda item: (item[0], item[2] or 0)
    )
    return {
        "estimated_tokens": int(tokens),
        "fill_percent": percent,
        "status": _status(percent, watch, full),
        "at": at,
        "source": source,
        "inferred": bool(inferred),
    }


def _peak_rank(peak):
    """Fill first (windows differ between models), tokens to break a tie."""
    if not peak:
        return (-1.0, -1)
    percent = peak["fill_percent"]
    return (-1.0 if percent is None else float(percent), peak["estimated_tokens"] or 0)


def build(rows, *, watch, full, now=None, limit=10):
    """The audit as a dict.

    ``rows`` is one dict per session: ``session_id``, ``name``, ``title``,
    ``state``, ``last_activity``, ``model``, ``reading`` (the gauge fields, or
    None), ``events`` (``compaction_events`` for the session) and ``series``
    (``turn_series``, optional).
    """
    now = time.time() if now is None else now
    sessions = []
    for row in rows or []:
        reading = row.get("reading")
        window = (reading or {}).get("window_tokens")
        current = None
        if reading:
            current = {
                "estimated_tokens": reading.get("estimated_tokens"),
                "fill_percent": reading.get("fill_percent"),
                "fill_display": reading.get("fill_display"),
                "status": reading.get("status"),
                "window_tokens": window,
                "window_source": reading.get("window_source"),
                "turns": reading.get("turns"),
            }
        entries, used_window = history(row.get("events"), window, watch, full)
        # Snapshots stay in ``entries`` for the peak. They are not events: they
        # are left out of the counts and of the history that the reports list.
        events = [e for e in entries if e["kind"] != KIND_SNAPSHOT]
        compactions = [e for e in events if e["kind"] == KIND_COMPACTION]
        resets = [e for e in events if e["kind"] == KIND_RESET]
        last = compactions[-1] if compactions else None
        sessions.append({
            "session_id": row.get("session_id"),
            "name": row.get("name") or row.get("session_id"),
            "title": row.get("title"),
            "state": row.get("state"),
            "last_activity": row.get("last_activity"),
            "model": row.get("model") or (reading or {}).get("model"),
            "window_tokens": used_window,
            "window_inferred": bool(used_window) and not window,
            "current": current,
            "composition": composition((reading or {}).get("breakdown")),
            "peak": _peak(entries, current, row.get("series"), used_window, bool(window), watch, full),
            "compactions": {
                "count": len(compactions),
                "last_at": last["at"] if last else None,
                "last_reason": last["reason"] if last else None,
            },
            "resets": len(resets),
            "history": events,
        })
    sessions.sort(key=lambda s: (
        gauge_math.STATE_ORDER.get(s["state"], 0),
        tuple(-v for v in _peak_rank(s["peak"])),
    ))
    shown = sessions[: max(int(limit), 0)] if limit is not None else sessions

    live = [s for s in sessions if s["composition"] and s["state"] in (None, "active")]
    pool = live or [s for s in sessions if s["composition"]]
    totals_parts = {key: sum(s["composition"][key]["tokens"] for s in pool) for key in PARTS}
    grand = sum(totals_parts.values())
    top = None
    for s in sessions:
        if s["peak"] and (top is None or _peak_rank(s["peak"]) > _peak_rank(top["peak"])):
            top = s
    return {
        "generated_at": now,
        "session_count": len(sessions),
        "shown": len(shown),
        "sessions": shown,
        "totals": {
            "compactions": sum(s["compactions"]["count"] for s in sessions),
            "resets": sum(s["resets"] for s in sessions),
            "highest_peak": None if top is None else {
                "session_id": top["session_id"],
                "name": top["name"],
                "estimated_tokens": top["peak"]["estimated_tokens"],
                "fill_percent": top["peak"]["fill_percent"],
                "status": top["peak"]["status"],
                "at": top["peak"]["at"],
                "inferred": top["peak"]["inferred"],
            },
            "composition": None if grand <= 0 else {
                key: {
                    "tokens": totals_parts[key],
                    "share": round(100.0 * totals_parts[key] / grand, 1),
                }
                for key in PARTS
            },
            "composition_scope": "active" if live else "all",
        },
    }


# --- text and markdown --------------------------------------------------------


def _when(value, lang):
    if value is None:
        return labels(lang)["undated"]
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return labels(lang)["undated"]


def _pct(value):
    label = gauge_math.percent_label(value)
    return "—" if label is None else f"{label}%"


def _tokens(value):
    if value is None:
        return "—"
    value = int(value)
    return f"{value / 1000:.0f}k" if value >= 10_000 else str(value)


def _peak_text(peak, lang):
    """``≈97% FULL`` (≈ when the window was inferred), or the token count when no fill is known."""
    if peak["fill_percent"] is None:
        return f"{_tokens(peak['estimated_tokens'])} {labels(lang)['tokens']}"
    mark = "≈" if peak.get("inferred") else ""
    return f"{mark}{_pct(peak['fill_percent'])} {peak['status']}"


def _headline(data, lang):
    lab = labels(lang)
    totals = data["totals"]
    count = data["session_count"]
    word = lab["session"] if count == 1 else lab["sessions"]
    comp = totals["compactions"]
    resets = totals["resets"]
    line = (
        f"{lab['title']} — {count} {word} · {comp} "
        f"{lab['compaction'] if comp == 1 else lab['compactions']} · {resets} "
        f"{lab['reset'] if resets == 1 else lab['resets']}"
    )
    peak = totals["highest_peak"]
    if peak is None:
        return line, f"{lab['highest']}: {lab['none']}"
    top = f"{lab['highest']}: {peak['name']} {_peak_text(peak, lang)}"
    if peak["at"]:
        top += f" ({_when(peak['at'], lang)} UTC)"
    return line, top


def _mix(comp, lang):
    lab = labels(lang)
    if not comp:
        return None
    ordered = sorted(DISPLAY_ORDER, key=lambda key: -comp[key]["tokens"])
    return " · ".join(
        f"{lab['parts'][key]} {gauge_math.percent_label(comp[key]['share'])}%"
        for key in ordered if comp[key]["tokens"] > 0
    )


def summary(data, lang="en"):
    """The two headline lines as one string."""
    head, top = _headline(data, lang)
    return f"{head}\n{top}"


def render_text(data, lang="en"):
    lab = labels(lang)
    if not data["session_count"]:
        return lab["no_data"]
    head, top = _headline(data, lang)
    lines = [head, top]
    mix = _mix(data["totals"]["composition"], lang)
    if mix:
        lines.append(f"{lab['where']}: {mix}")
    for s in data["sessions"]:
        lines.append("")
        state = lab["states"].get(s["state"], s["state"] or "")
        now = _pct((s["current"] or {}).get("fill_percent")) if s["current"] else "—"
        lines.append(f"{s['name']} · {state} · {lab['now']} {now}")
        peak = s["peak"]
        if peak:
            where = f" ({_when(peak['at'], lang)} UTC)" if peak["at"] else ""
            lines.append(f"  {lab['peak']} {_peak_text(peak, lang)}{where}")
        comps = s["compactions"]
        if comps["count"]:
            lines.append(
                f"  {comps['count']} "
                f"{lab['compaction'] if comps['count'] == 1 else lab['compactions']}"
                f" ({lab['last']} {_when(comps['last_at'], lang)}"
                f"{', ' + str(comps['last_reason']) if comps['last_reason'] else ''})"
                + (f" · {s['resets']} {lab['reset'] if s['resets'] == 1 else lab['resets']}" if s["resets"] else "")
            )
        else:
            extra = f" · {s['resets']} {lab['reset'] if s['resets'] == 1 else lab['resets']}" if s["resets"] else ""
            lines.append(f"  {lab['no_compaction']}{extra}")
        mix = _mix(s["composition"], lang)
        if mix:
            lines.append(f"  {lab['composition']}: {mix}")
    hidden = data["session_count"] - data["shown"]
    if hidden > 0:
        lines.extend(["", f"+{hidden} …"])
    lines.extend(["", lab["note"]])
    return "\n".join(lines)


def _cell(value):
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")


def render_markdown(data, lang="en"):
    lab = labels(lang)
    head, top = _headline(data, lang)
    lines = ["# Hermes Gauge — " + lab["title"], "", head, "", top, ""]
    mix = _mix(data["totals"]["composition"], lang)
    if mix:
        lines.extend([f"**{lab['where']}** — {mix}", ""])
    if not data["session_count"]:
        return "\n".join(lines + [lab["no_data"], ""])
    lines.append("| " + " | ".join(lab["hdr"]) + " |")
    lines.append("| " + " | ".join("---" for _ in lab["hdr"]) + " |")
    for s in data["sessions"]:
        comp = s["composition"] or {}
        cells = [
            _cell(s["name"]),
            _cell(lab["states"].get(s["state"], s["state"] or "")),
            _cell(_pct((s["current"] or {}).get("fill_percent")) if s["current"] else "—"),
            _cell(_peak_text(s["peak"], lang) if s["peak"] else "—"),
            _cell(s["compactions"]["count"]),
            _cell(s["resets"]),
        ]
        for key in DISPLAY_ORDER:
            part = comp.get(key)
            cells.append(_cell(f"{gauge_math.percent_label(part['share'])}%" if part else "—"))
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", f"_{lab['note']}_", ""])
    return "\n".join(lines)
