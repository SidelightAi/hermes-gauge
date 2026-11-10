"""Incident report: what went wrong, how to fix it, where things stand.

``collect`` turns the plugin's own records (compaction log, hook log, dated
turns, session titles) into one dict. ``render_html`` turns that dict into a
single self-contained page, readable on a phone. ``llm_block`` writes the same
facts as markdown, ready to paste into any assistant.

Standard library only. Nothing here writes to a database, and nothing here
compacts, archives or kills a session: the plugin measures, alerts, recommends.

Terms used on the page:

- a *compaction* is Hermes summarizing old context because the window filled;
- a *reset* is the user starting a fresh session (``/new``). It empties the
  counter but summarizes nothing, so it is shown in the chronology and left out
  of the compaction count;
- a *network error* is a provider error logged by the plugin (connection
  dropped, broken pipe); a *missed turn* is a turn the plugin could not measure.
"""

from __future__ import annotations

import html
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
audit_mod = _sibling("audit")
sessions_mod = _sibling("sessions")

# Wall-clock stamps are written in the machine's local time. Tests swap this.
_localtime = time.localtime

TIMELINE_MAX = 400          # entries shown in the page
LLM_TIMELINE_MAX = 150      # entries in the paste-able block
CARDS_MAX = 30
SPARK_POINTS = 60

KIND_COMPACTION = "compaction"
KIND_RESET = "reset"
KIND_NETWORK = "network_error"
KIND_MISSED = "missed_turn"
LOG_KINDS = {"provider_error": KIND_NETWORK, "missed": KIND_MISSED}

T = {
    "fr": {
        "html_lang": "fr",
        "title": "Rapport d'incidents — Contexte Hermes",
        "generated": "Généré le",
        "local_time": "heure locale de la machine",
        "period": "Période",
        "period_all": "depuis le premier enregistrement",
        "since": "depuis",
        "to_now": "jusqu'à maintenant",
        "last_days": "{n} derniers jours",
        "sec_verdict": "1 · Y a-t-il eu des incidents ?",
        "sec_fix": "2 · Comment corriger",
        "sec_status": "3 · État des lieux",
        "no_cards": "Aucune session à corriger pour l'instant.",
        "kpi_compactions": "compactages (contexte résumé)",
        "kpi_full": "sessions actives en FULL",
        "kpi_watch": "sessions actives en WATCH",
        "kpi_network": "erreurs réseau",
        "kpi_missed_note": "+ {n} tour(s) non mesuré(s)",
        "resets_note": "{n} remise(s) à zéro (/new) ne sont pas comptées : elles vident le compteur sans rien résumer.",
        "timeline": "Chronologie complète",
        "timeline_empty": "Rien n'a été enregistré sur la période.",
        "timeline_omitted": "{n} entrée(s) plus anciennes non affichées.",
        "llm_summary": "🤖 Bloc à copier pour un LLM (obtenir un plan de correction)",
        "llm_help": "Copie ce bloc et donne-le à n'importe quel assistant : il contient les données nécessaires pour proposer un plan d'hygiène de contexte.",
        "copy": "Copier le bloc",
        "copied": "✓ Copié !",
        "copy_manual": "Sélectionne le texte et copie-le (Ctrl/Cmd+C).",
        "happened": "Ce qui s'est passé",
        "implies": "Ce que ça implique",
        "fix": "Correction",
        "last_activity": "Dernière activité",
        "compactions_chip": "{n} compactage(s)",
        "network_chip": "{n} erreur(s) réseau",
        "missed_chip": "{n} tour(s) non mesuré(s)",
        "turns": "tours",
        "fill": "remplissage",
        "peak": "pic",
        "states": {"active": "active", "frozen": "figée", "archived": "archivée"},
        "status_names": {"OK": "OK", "WATCH": "WATCH", "FULL": "FULL", "UNKNOWN": "inconnu"},
        "spark_none": "Pas encore assez de mesures datées pour tracer la courbe.",
        "spark_label": "Remplissage dans le temps ; traits verticaux : compactages",
        "spark_axis_from": "début",
        "unknown_session": "session inconnue",
        "more_cards": "{n} autre(s) session(s) non détaillée(s).",
        "ev_compaction": "Compactage",
        "ev_reset": "Nouvelle session (/new)",
        "ev_network": "Erreur réseau",
        "ev_missed": "Tour non mesuré",
    },
    "en": {
        "html_lang": "en",
        "title": "Incident report — Hermes context",
        "generated": "Generated",
        "local_time": "machine local time",
        "period": "Period",
        "period_all": "since the first record",
        "since": "since",
        "to_now": "until now",
        "last_days": "last {n} days",
        "sec_verdict": "1 · Were there incidents?",
        "sec_fix": "2 · How to fix",
        "sec_status": "3 · Where things stand",
        "no_cards": "No session needs attention right now.",
        "kpi_compactions": "compactions (context summarized)",
        "kpi_full": "active sessions at FULL",
        "kpi_watch": "active sessions at WATCH",
        "kpi_network": "network errors",
        "kpi_missed_note": "+ {n} turn(s) not measured",
        "resets_note": "{n} reset(s) (/new) are not counted: they empty the counter without summarizing anything.",
        "timeline": "Full chronology",
        "timeline_empty": "Nothing was recorded in this period.",
        "timeline_omitted": "{n} older entries not shown.",
        "llm_summary": "🤖 Block to copy for an LLM (get a fix plan)",
        "llm_help": "Copy this block and give it to any assistant: it holds the data needed to propose a context hygiene plan.",
        "copy": "Copy the block",
        "copied": "✓ Copied!",
        "copy_manual": "Select the text and copy it (Ctrl/Cmd+C).",
        "happened": "What happened",
        "implies": "What it means",
        "fix": "Fix",
        "last_activity": "Last activity",
        "compactions_chip": "{n} compaction(s)",
        "network_chip": "{n} network error(s)",
        "missed_chip": "{n} turn(s) not measured",
        "turns": "turns",
        "fill": "fill",
        "peak": "peak",
        "states": {"active": "active", "frozen": "frozen", "archived": "archived"},
        "status_names": {"OK": "OK", "WATCH": "WATCH", "FULL": "FULL", "UNKNOWN": "unknown"},
        "spark_none": "Not enough dated measurements yet to draw the curve.",
        "spark_label": "Fill over time; vertical lines: compactions",
        "spark_axis_from": "start",
        "unknown_session": "unknown session",
        "more_cards": "{n} more session(s) not detailed.",
        "ev_compaction": "Compaction",
        "ev_reset": "New session (/new)",
        "ev_network": "Network error",
        "ev_missed": "Turn not measured",
    },
}


def tr(lang):
    return T.get(lang) or T["en"]


def normalize_lang(lang, default="en"):
    text = str(lang or "").strip().lower()
    if text.startswith("fr"):
        return "fr"
    if text.startswith("en"):
        return "en"
    return "fr" if str(default or "").strip().lower().startswith("fr") else "en"


def _count(n, one, many):
    number = int(n or 0)
    return f"{number} {one if number == 1 else many}"


_REASON_WORDS = {
    "en": {
        "proactive_prune": "automatic summary",
        "watch": "crossed the watch line",
        "manual": "asked for",
        "compaction": "summary",
        "new": "fresh start",
        "new_session": "fresh start",
        "reset": "fresh start",
        "session_reset": "fresh start",
    },
    "fr": {
        "proactive_prune": "résumé automatique",
        "watch": "passage en vigilance",
        "manual": "demandé",
        "compaction": "résumé",
        "new": "nouveau départ",
        "new_session": "nouveau départ",
        "reset": "nouveau départ",
        "session_reset": "nouveau départ",
    },
}


def reason_words(reason, lang):
    """A plain word for a Hermes reason code. Unknown codes stay hidden."""
    if not reason:
        return ""
    table = _REASON_WORDS.get(lang) or _REASON_WORDS["en"]
    return table.get(str(reason).strip().lower(), "")


# --- time ----------------------------------------------------------------------


def stamp(value, lang="fr", long=False):
    try:
        moment = _localtime(float(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"
    if lang == "en":
        return time.strftime("%Y-%m-%d %H:%M", moment)
    return time.strftime("%d.%m.%y %H:%M" if long else "%d.%m %H:%M", moment)


def iso_utc(value):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def utc_offset(now):
    """``+0100``: the offset of the local stamps shown on the page."""
    try:
        seconds = int(getattr(_localtime(float(now)), "tm_gmtoff", 0) or 0)
    except (TypeError, ValueError, OverflowError, OSError):
        return "+0000"
    minutes = abs(seconds) // 60
    return f"{'+' if seconds >= 0 else '-'}{minutes // 60:02d}{minutes % 60:02d}"


# --- collection ----------------------------------------------------------------


def _clean(text, limit=200):
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _spark(series, window, events):
    """Fill over time for one session, thinned to ``SPARK_POINTS``."""
    if not window or len(series) < 2:
        return {"points": [], "events": [], "window_tokens": window}
    points = [(float(at), round(min(tokens / window, 1.0) * 100.0, 1)) for at, tokens in series]
    if len(points) > SPARK_POINTS:
        # Keep the highest point of each slice so a peak is never smoothed away.
        size = len(points) / SPARK_POINTS
        thinned = []
        for index in range(SPARK_POINTS):
            chunk = points[int(index * size): int((index + 1) * size)] or [points[-1]]
            thinned.append(max(chunk, key=lambda item: item[1]))
        points = thinned
    first, last = points[0][0], points[-1][0]
    marks = [
        {"at": e["at"], "kind": e["kind"]}
        for e in events
        if e["at"] is not None and first <= e["at"] <= last
    ]
    return {
        "points": [[at, pct] for at, pct in points],
        "events": marks,
        "window_tokens": window,
    }


def _advice(card, lang):
    """What happened, what it means, what to do — for one session, in plain words."""
    fr = lang == "fr"
    status = card["status"] if card["state"] == "active" else None
    comps, resets = card["compactions"], card["resets"]
    net, missed = card["network_errors"], card["missed_turns"]
    pct = gauge_math.percent_label(card["fill_percent"])
    happened, implies, fix = [], [], []

    if comps:
        last = stamp(card["last_compaction_at"], lang) if card["last_compaction_at"] else "—"
        happened.append(
            f"{comps} compactage(s), le dernier le {last}." if fr
            else f"{comps} compaction(s), the last one on {last}."
        )
        implies.append(
            "Hermes a résumé l'ancien contenu : des détails précis (chiffres, noms, consignes données plus tôt) peuvent manquer."
            if fr else
            "Hermes summarized the older content: precise details (figures, names, instructions given earlier) may be gone."
        )
    if net or missed:
        parts = []
        if net:
            parts.append(f"{net} erreur(s) réseau" if fr else f"{net} network error(s)")
        if missed:
            parts.append(f"{missed} tour(s) non mesuré(s)" if fr else f"{missed} turn(s) not measured")
        happened.append(" · ".join(parts) + ".")
        implies.append(
            "Un tour interrompu n'est pas compté : la jauge peut afficher un peu moins que la réalité. Rien n'est corrompu."
            if fr else
            "An interrupted turn is not counted: the gauge may read a little below reality. Nothing is corrupted."
        )
    if resets:
        happened.append(
            f"{resets} remise(s) à zéro (/new) : compteur vidé, rien de résumé." if fr
            else f"{resets} reset(s) (/new): counter emptied, nothing summarized."
        )
    if status in ("FULL", "WATCH") and pct is not None:
        happened.append(
            f"Remplissage actuel : {pct}% ({status})." if fr else f"Current fill: {pct}% ({status})."
        )

    if status == "FULL":
        implies.append(
            "La fenêtre est presque saturée : les prochaines réponses risquent d'être tronquées ou le contexte d'être résumé à nouveau."
            if fr else
            "The window is almost full: the next answers may be cut short, or the context summarized again."
        )
        fix += [
            "Demande à l'agent : « Résume les décisions, les points ouverts et les prochaines étapes en 15 lignes. »" if fr
            else "Ask the agent: “Summarize the decisions, open points and next steps in 15 lines.”",
            "Ouvre une session neuve avec /new et colle ce résumé." if fr
            else "Start a fresh session with /new and paste that summary.",
            "Redonne les éléments clés (fichiers, chiffres, contraintes) plutôt que de supposer qu'ils ont survécu." if fr
            else "Re-give the key elements (files, figures, constraints) instead of assuming they survived.",
        ]
    elif status == "WATCH":
        implies.append(
            "La session approche de la limite : encore de la marge, mais pas pour un gros document."
            if fr else
            "The session is nearing the limit: some room left, but not for a large document."
        )
        fix += [
            "Termine l'étape en cours, puis demande un résumé avant de changer de sujet." if fr
            else "Finish the current step, then ask for a summary before changing subject.",
            "Ouvre /new avec ce résumé pour le prochain sujet." if fr
            else "Use /new with that summary for the next subject.",
        ]
    elif comps:
        fix += [
            "Pour tout travail sensible, redonne les éléments clés avant de continuer." if fr
            else "For anything sensitive, re-give the key elements before going on.",
            "Pour un nouveau sujet, ouvre /new : une session par sujet." if fr
            else "For a new subject, open /new: one session per subject.",
        ]
    if comps >= 3:
        fix.append(
            "Cette session a été résumée plusieurs fois : le sujet est trop long. Découpe-le en sessions plus courtes." if fr
            else "This session was summarized several times: the topic is too long. Split it into shorter sessions."
        )
    if (net or missed) and not fix:
        fix.append(
            "Si une réponse s'est arrêtée net, redemande-la. Sinon, rien à faire." if fr
            else "If an answer stopped short, ask again. Otherwise there is nothing to do."
        )
    elif net or missed:
        fix.append(
            "Si une réponse s'est arrêtée net, redemande-la." if fr
            else "If an answer stopped short, ask again."
        )
    if card["state"] in ("frozen", "archived") and fix:
        fix.insert(
            0,
            "Personne ne travaille dans cette session : rien d'urgent. Si tu la reprends :" if fr
            else "Nobody is working in this session, so nothing is urgent. If you resume it:",
        )
    return happened, implies, fix


def _severity(card):
    live = card["state"] == "active"
    rank = {"FULL": 0, "WATCH": 1}.get(card["status"], 2) if live else 3
    return (rank, -card["compactions"], -(card["network_errors"] + card["missed_turns"]), card["name"])


def _timeline(entries):
    entries.sort(key=lambda e: (e["at"] if e["at"] is not None else 0.0))
    return entries


def _verdict(kpis, lang):
    """One sentence and a level: red, amber or green."""
    fr = lang == "fr"
    parts = []
    if kpis["compactions"]:
        parts.append(_count(kpis["compactions"], "compactage", "compactages") if fr
                     else _count(kpis["compactions"], "compaction", "compactions"))
    if kpis["network_errors"]:
        parts.append(_count(kpis["network_errors"], "erreur réseau", "erreurs réseau") if fr
                     else _count(kpis["network_errors"], "network error", "network errors"))
    if kpis["missed_turns"]:
        parts.append(_count(kpis["missed_turns"], "tour non mesuré", "tours non mesurés") if fr
                     else _count(kpis["missed_turns"], "turn not measured", "turns not measured"))
    summary = ", ".join(parts)
    full_n, watch_n = kpis["full_sessions"], kpis["watch_sessions"]
    idle_full = kpis.get("idle_full_sessions") or 0
    if full_n:
        sessions = (_count(full_n, "session active est presque pleine", "sessions actives sont presque pleines") if fr
                    else _count(full_n, "active session is almost full", "active sessions are almost full"))
        if fr:
            lead = f"Oui — {summary} — et " if summary else "Pas d'incident enregistré, mais "
            return "red", f"{lead}{sessions} : les corrections sont juste en dessous."
        lead = f"Yes — {summary} — and " if summary else "No incident recorded, but "
        return "red", f"{lead}{sessions}: the fixes are just below."
    if idle_full and not summary and not watch_n:
        if fr:
            return "amber", (
                f"Pas d'incident enregistré, mais {_count(idle_full, 'conversation inactive est presque pleine', 'conversations inactives sont presque pleines')}."
            )
        return "amber", (
            f"No incident recorded, but {_count(idle_full, 'idle conversation is almost full', 'idle conversations are almost full')}."
        )
    if summary:
        if fr:
            tail = f" {_count(watch_n, 'session active approche', 'sessions actives approchent')} de la limite." if watch_n else ""
            return "amber", (f"Oui, mais rien de critique en ce moment — {summary}. "
                             f"Aucune session active n'est presque pleine.{tail}")
        tail = f" {_count(watch_n, 'active session is', 'active sessions are')} nearing the limit." if watch_n else ""
        return "amber", (f"Yes, but nothing critical right now — {summary}. "
                         f"No active session is almost full.{tail}")
    if watch_n:
        return "amber", (
            f"Pas d'incident enregistré, mais {_count(watch_n, 'session active approche', 'sessions actives approchent')} de la limite." if fr
            else f"No incident recorded, but {_count(watch_n, 'active session is', 'active sessions are')} nearing the limit."
        )
    return "green", ("Non — aucun incident enregistré sur la période." if fr
                     else "No — no incident recorded in this period.")


def collect(rows, hooks, *, watch, full, now=None, lang="fr", days=None):
    """The whole report as a dict.

    ``rows`` is what ``audit.build`` takes (one dict per session, with ``events``
    and ``series``). ``hooks`` is the hook log: dicts with ``session_id``,
    ``kind``, ``detail`` and ``created``. ``days`` keeps only the last N days.
    """
    lang = normalize_lang(lang)
    now = time.time() if now is None else float(now)
    cutoff = now - float(days) * 86400.0 if days else None

    def inside(at):
        return cutoff is None or (at is not None and at >= cutoff)

    scoped = []
    for row in rows or []:
        kept = dict(row)
        kept["events"] = [e for e in (row.get("events") or []) if inside(e.get("created"))]
        scoped.append(kept)
    data = audit_mod.build(scoped, watch=watch, full=full, now=now, limit=None)
    by_id = {s["session_id"]: s for s in data["sessions"]}

    name_of = {sid: s["name"] for sid, s in by_id.items()}
    timeline = []
    net, missed = {}, {}
    unattributed = 0
    for hook in hooks or []:
        kind = LOG_KINDS.get(hook.get("kind"))
        at = hook.get("created")
        if kind is None or not inside(at):
            continue
        sid = hook.get("session_id") or ""
        if sid:
            bucket = net if kind == KIND_NETWORK else missed
            bucket[sid] = bucket.get(sid, 0) + 1
        else:
            unattributed += 1
        timeline.append({
            "at": at, "kind": kind, "session_id": sid or None,
            "name": name_of.get(sid) or (sid if sid else None),
            "reason": None, "detail": _clean(hook.get("detail")),
        })
    for sid, session in by_id.items():
        for entry in session["history"]:
            timeline.append({
                "at": entry["at"], "kind": entry["kind"], "session_id": sid,
                "name": session["name"], "reason": entry["reason"], "detail": "",
            })
    _timeline(timeline)

    events_by_session = {sid: [e for e in s["history"]] for sid, s in by_id.items()}
    cards = []
    for sid, session in by_id.items():
        comps = session["compactions"]["count"]
        n_net, n_missed = net.get(sid, 0), missed.get(sid, 0)
        current = session["current"] or {}
        state = session["state"]
        status = current.get("status") if current else None
        hot = status in ("WATCH", "FULL")
        if not (comps or n_net or n_missed or hot):
            continue
        spark = _spark(
            next((r.get("series") for r in scoped if r.get("session_id") == sid), []) or [],
            session.get("window_tokens"), events_by_session[sid],
        )
        card = {
            "session_id": sid,
            "name": session["name"],
            "state": state,
            "status": status or "UNKNOWN",
            "fill_percent": current.get("fill_percent"),
            "turns": current.get("turns"),
            "last_activity": session["last_activity"],
            "compactions": comps,
            "resets": session["resets"],
            "network_errors": n_net,
            "missed_turns": n_missed,
            "last_compaction_at": session["compactions"]["last_at"],
            "peak": session["peak"],
            "spark": spark,
        }
        card["happened"], card["implies"], card["fix"] = _advice(card, lang)
        cards.append(card)
    cards.sort(key=_severity)

    live = [s for s in by_id.values() if s["state"] == "active" and s["current"]]
    idle = [s for s in by_id.values() if s["state"] != "active" and s["current"]]
    kpis = {
        "compactions": sum(s["compactions"]["count"] for s in by_id.values()),
        "resets": sum(s["resets"] for s in by_id.values()),
        "full_sessions": sum(1 for s in live if s["current"]["status"] == "FULL"),
        "watch_sessions": sum(1 for s in live if s["current"]["status"] == "WATCH"),
        "idle_full_sessions": sum(1 for s in idle if s["current"]["status"] == "FULL"),
        "network_errors": sum(1 for e in timeline if e["kind"] == KIND_NETWORK),
        "missed_turns": sum(1 for e in timeline if e["kind"] == KIND_MISSED),
    }
    level, message = _verdict(kpis, lang)
    stamps = [e["at"] for e in timeline if e["at"] is not None]
    report = {
        "generated_at": now,
        "lang": lang,
        "period": {
            "from": min(stamps) if stamps else None,
            "to": now,
            "days": days,
        },
        "thresholds": {"watch": watch, "full": full},
        "verdict": {"level": level, "message": message},
        "kpis": kpis,
        "cards": cards[:CARDS_MAX],
        "cards_omitted": max(len(cards) - CARDS_MAX, 0),
        "timeline": timeline[-TIMELINE_MAX:],
        "timeline_omitted": max(len(timeline) - TIMELINE_MAX, 0),
        "unattributed_events": unattributed,
        "session_count": len(by_id),
    }
    report["llm"] = llm_block(report, by_id)
    return report


# --- the block for an LLM -------------------------------------------------------


def _event_label(entry, lang):
    t = tr(lang)
    return {
        KIND_COMPACTION: t["ev_compaction"], KIND_RESET: t["ev_reset"],
        KIND_NETWORK: t["ev_network"], KIND_MISSED: t["ev_missed"],
    }[entry["kind"]]


def _event_text(entry, lang):
    label = _event_label(entry, lang)
    if entry["kind"] in (KIND_COMPACTION, KIND_RESET):
        words = reason_words(entry.get("reason"), lang)
        return f"{label} ({words})" if words else label
    return f"{label} — {entry['detail']}" if entry["detail"] else label


def llm_block(report, by_id=None):
    """Autonomous markdown: facts first, rules, then the ask."""
    lang = report["lang"]
    fr = lang == "fr"
    t = tr(lang)
    k = report["kpis"]
    period = report["period"]
    long = bool(period["from"]) and (report["generated_at"] - period["from"]) > 180 * 86400
    start = stamp(period["from"], lang, long) if period["from"] else "—"
    lines = [
        "# Rapport Hermes Gauge — à transmettre à un LLM" if fr
        else "# Hermes Gauge report — to hand to an LLM",
        "",
        (f"Période : du {start} à {stamp(report['generated_at'], lang, long)} "
         f"({t['local_time']} UTC{utc_offset(report['generated_at'])})."
         if fr else
         f"Period: {start} to {stamp(report['generated_at'], lang, long)} "
         f"({t['local_time']}, UTC{utc_offset(report['generated_at'])})."),
        "",
        "## Synthèse" if fr else "## Summary",
        (f"- {_count(k['compactions'], 'compactage', 'compactages')} : la fenêtre a débordé et Hermes a résumé l'ancien contenu "
         "(des détails ont pu être perdus)." if fr else
         f"- {_count(k['compactions'], 'compaction', 'compactions')}: the window overflowed and Hermes summarized the older content "
         "(details may have been lost)."),
        (f"- {_count(k['resets'], 'remise à zéro', 'remises à zéro')} (/new), non comptée comme incident." if fr and int(k['resets'] or 0) == 1
         else f"- {_count(k['resets'], 'remise à zéro', 'remises à zéro')} (/new), non comptées comme incidents." if fr
         else f"- {_count(k['resets'], 'reset', 'resets')} (/new), not counted as incidents."),
        (f"- {_count(k['network_errors'], 'erreur réseau', 'erreurs réseau')} et {_count(k['missed_turns'], 'tour non mesuré', 'tours non mesurés')}." if fr
         else f"- {_count(k['network_errors'], 'network error', 'network errors')} and {_count(k['missed_turns'], 'turn not measured', 'turns not measured')}."),
        (f"- Maintenant : {_count(k['full_sessions'], 'session active', 'sessions actives')} en FULL, {k['watch_sessions']} en WATCH." if fr
         else f"- Now: {_count(k['full_sessions'], 'active session', 'active sessions')} at FULL, {k['watch_sessions']} at WATCH."),
        "",
        "## Incidents (ordre chronologique)" if fr else "## Incidents (chronological)",
    ]
    shown = report["timeline"][-LLM_TIMELINE_MAX:]
    omitted = report["timeline_omitted"] + len(report["timeline"]) - len(shown)
    if omitted:
        lines.append(
            f"- … {_count(omitted, 'événement plus ancien omis', 'événements plus anciens omis')}." if fr
            else f"- … {_count(omitted, 'older event omitted', 'older events omitted')}."
        )
    if not shown:
        lines.append("- (aucun)" if fr else "- (none)")
    for entry in shown:
        where = entry["name"] or t["unknown_session"]
        lines.append(f"- {stamp(entry['at'], lang, long)} — {where} : {_event_text(entry, lang)}")
    lines.extend(["", "## Données par session concernée" if fr else "## Data per session"])
    if not report["cards"]:
        lines.append("- (aucune)" if fr else "- (none)")
    sessions = by_id or {}
    for card in report["cards"]:
        pct = gauge_math.percent_label(card["fill_percent"])
        fill = f"{pct}%" if pct is not None else "—"
        peak = card["peak"]
        peak_text = ""
        if peak and peak["fill_percent"] is not None:
            peak_text = (f", pic {gauge_math.percent_label(peak['fill_percent'])}%" if fr
                         else f", peak {gauge_math.percent_label(peak['fill_percent'])}%")
        mix = audit_mod._mix((sessions.get(card["session_id"]) or {}).get("composition"), lang)
        mix_text = (f", composition : {mix}" if fr else f", composition: {mix}") if mix else ""
        state = t["states"].get(card["state"], card["state"] or "")
        lines.append(
            (f"- {card['name']} — {fill} de remplissage ({card['status']}), {card['turns'] or 0} tours, "
             f"{_count(card['compactions'], 'compactage', 'compactages')}, {_count(card['network_errors'], 'erreur réseau', 'erreurs réseau')}, "
             f"{_count(card['missed_turns'], 'tour non mesuré', 'tours non mesurés')}, session {state}, "
             f"dernière activité {stamp(card['last_activity'], lang, long)}{peak_text}{mix_text}." if fr else
             f"- {card['name']} — {fill} fill ({card['status']}), {card['turns'] or 0} turns, "
             f"{_count(card['compactions'], 'compaction', 'compactions')}, {_count(card['network_errors'], 'network error', 'network errors')}, "
             f"{_count(card['missed_turns'], 'turn not measured', 'turns not measured')}, session {state}, "
             f"last activity {stamp(card['last_activity'], lang, long)}{peak_text}{mix_text}.")
        )
    th = report["thresholds"]
    watch_pct = gauge_math.percent_label(th["watch"] * 100)
    full_pct = gauge_math.percent_label(th["full"] * 100)
    lines.extend([
        "",
        "## Règles connues" if fr else "## Known rules",
        (f"- Seuils : WATCH à {watch_pct}%, FULL à {full_pct}% de la fenêtre du modèle." if fr
         else f"- Thresholds: WATCH at {watch_pct}%, FULL at {full_pct}% of the model window."),
        ("- Un compactage remet le compteur de la session à zéro : ce rapport est la seule vue qui compte les incidents passés." if fr
         else "- A compaction resets the session counter: this report is the only view that counts past incidents."),
        ("- Le plugin ne compacte, n'archive et ne tue jamais une session : il mesure, alerte et recommande." if fr
         else "- The plugin never compacts, archives or kills a session: it measures, alerts and recommends."),
        "",
        "## Demande" if fr else "## Request",
        ("Analyse ces incidents et propose un plan d'hygiène de contexte : quelles sessions clore ou résumer, "
         "quels usages corriger (re-collage de gros documents, sessions trop longues sur un même sujet), "
         "et quelle cadence de suivi." if fr else
         "Analyze these incidents and propose a context hygiene plan: which sessions to close or summarize, "
         "which habits to change (re-pasting large documents, sessions running too long on one subject), "
         "and how often to check."),
    ])
    return "\n".join(lines)


# --- HTML -----------------------------------------------------------------------

_CSS = """
:root{--bg:#0b0f0d;--card:#101713;--line:#1d2a22;--txt:#d9e6dd;--dim:#7d9484;--green:#00e676;--amber:#ffb74d;--red:#ff5252;--violet:#c9a0ff;--mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:20px 14px 80px;overflow-wrap:anywhere}
.wrap{max-width:860px;margin:0 auto}
h1{font:700 20px/1.3 var(--mono);color:var(--green);margin:0 0 4px}
h2.sec{font:700 13px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--green);margin:26px 0 10px}
.meta{color:var(--dim);font:12px/1.5 var(--mono);margin-bottom:18px}
.verdict{border-radius:12px;padding:14px 16px;font-size:15px;border:1px solid}
.v-red{background:#2a1214;border-color:#6b2b2b}
.v-amber{background:#2a2112;border-color:#5c4a1d}
.v-green{background:#0f2218;border-color:#1d5c38}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:0 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}
.kpi b{display:block;font:700 22px var(--mono);color:var(--green)}
.kpi.red b{color:var(--red)}.kpi.viol b{color:var(--violet)}.kpi.amb b{color:var(--amber)}
.kpi span{font:11px var(--mono);color:var(--dim)}
.kpi small{display:block;font:10px var(--mono);color:var(--dim);margin-top:2px}
.note{font:12px/1.5 var(--mono);color:var(--dim);margin:6px 0 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.card h3{margin:0 0 6px;font-size:16px;line-height:1.35}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px}
.chip{font:11px var(--mono);padding:3px 9px;border-radius:6px;border:1px solid}
.c-red{color:var(--red);border-color:#6b2b2b}.c-amber{color:var(--amber);border-color:#5c4a1d}.c-ok{color:var(--green);border-color:#1d5c38}.c-v{color:var(--violet);border-color:#4a3a68}.c-dim{color:var(--dim);border-color:var(--line)}
.row{font:12px/1.5 var(--mono);color:var(--dim)}
.blk{margin-top:10px}
.blk h4{margin:0 0 3px;font:700 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.blk p{margin:0 0 4px;font-size:14px}
.fix{padding:10px 12px;background:#0f1a14;border-left:3px solid var(--green);border-radius:6px}
.fix h4{color:var(--green)}
.fix ol{margin:4px 0 0;padding-left:20px;font-size:14px}
.fix li{margin-bottom:3px}
.spark{margin:8px 0 2px;width:100%}
.spark svg{display:block;width:100%;height:auto}
.spark .cap{font:10px var(--mono);color:var(--dim)}
details{margin-top:18px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
summary{cursor:pointer;font-weight:600;color:var(--green)}
pre{background:#060907;border:1px solid var(--line);border-radius:8px;padding:12px;font:11px/1.5 var(--mono);color:#a9c9b3;white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;margin:0}
.copybtn{margin:10px 0;background:#12351f;color:var(--green);border:1px solid #1d5c38;border-radius:8px;padding:10px 16px;font:600 13px -apple-system,sans-serif;cursor:pointer}
.timeline{list-style:none;margin:10px 0 0;padding:0;font:12px/1.5 var(--mono)}
.timeline li{padding:5px 0;border-bottom:1px dashed #1a2a20;color:var(--dim)}
.timeline li b{color:var(--txt);font-weight:600}
.timeline .k-compaction b.k{color:var(--violet)}.timeline .k-network_error b.k{color:var(--red)}.timeline .k-missed_turn b.k{color:var(--amber)}.timeline .k-reset b.k{color:var(--dim)}
@media(max-width:600px){body{font-size:14px;padding:16px 12px 60px}h1{font-size:17px}.kpi b{font-size:18px}}
""".strip()

_JS = """
(function(){
var b=document.getElementById('copy'),p=document.getElementById('llm');
if(!b||!p)return;
function done(){b.textContent=b.getAttribute('data-done')}
function fallback(){
  var r=document.createRange();r.selectNodeContents(p);
  var s=window.getSelection();s.removeAllRanges();s.addRange(r);
  var ok=false;try{ok=document.execCommand('copy')}catch(e){}
  s.removeAllRanges();
  if(ok){done()}else{b.textContent=b.getAttribute('data-manual');r=document.createRange();r.selectNodeContents(p);s.addRange(r)}
}
b.addEventListener('click',function(){
  var text=p.textContent;
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,fallback)}else{fallback()}
});
})();
""".strip()


def _e(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _chip_class(status, state):
    if state != "active":
        return "c-dim"
    return {"FULL": "c-red", "WATCH": "c-amber", "OK": "c-ok"}.get(status, "c-dim")


def sparkline_svg(spark, thresholds, lang):
    """Inline SVG, fill % against real time. Empty string when there is nothing to draw."""
    points = spark.get("points") or []
    if len(points) < 2:
        return ""
    width, height, pad = 300.0, 56.0, 3.0
    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0) or 1.0

    def x(at):
        return round(pad + (at - t0) / span * (width - 2 * pad), 1)

    def y(pct):
        return round(height - pad - max(0.0, min(pct, 100.0)) / 100.0 * (height - 2 * pad), 1)

    parts = [
        f'<svg viewBox="0 0 {int(width)} {int(height)}" role="img" '
        f'aria-label="{_e(tr(lang)["spark_label"])}" preserveAspectRatio="none">'
    ]
    for key, color in (("watch", "#ffb74d"), ("full", "#ff5252")):
        level = y(thresholds[key] * 100.0)
        parts.append(
            f'<line x1="0" x2="{int(width)}" y1="{level}" y2="{level}" stroke="{color}" '
            'stroke-width="0.8" stroke-dasharray="3 3" opacity="0.55"/>'
        )
    for mark in spark.get("events") or []:
        color = "#c9a0ff" if mark["kind"] == KIND_COMPACTION else "#4a6157"
        px = x(mark["at"])
        parts.append(
            f'<line x1="{px}" x2="{px}" y1="0" y2="{int(height)}" stroke="{color}" stroke-width="1" opacity="0.8"/>'
        )
    path = " ".join(f"{x(at)},{y(pct)}" for at, pct in points)
    parts.append(
        f'<polyline points="{path}" fill="none" stroke="#00e676" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _card_html(card, thresholds, lang):
    t = tr(lang)
    status_name = t["status_names"].get(card["status"], card["status"])
    pct = gauge_math.percent_label(card["fill_percent"])
    chips = []
    if card["compactions"]:
        chips.append(f'<span class="chip c-v">{_e(t["compactions_chip"].format(n=card["compactions"]))}</span>')
    if card["network_errors"]:
        chips.append(f'<span class="chip c-red">{_e(t["network_chip"].format(n=card["network_errors"]))}</span>')
    if card["missed_turns"]:
        chips.append(f'<span class="chip c-amber">{_e(t["missed_chip"].format(n=card["missed_turns"]))}</span>')
    fill = f"{pct}% · {status_name}" if pct is not None else status_name
    chips.append(
        f'<span class="chip {_chip_class(card["status"], card["state"])}">'
        f'{_e(fill)} · {_e(card["turns"] or 0)} {_e(t["turns"])}</span>'
    )
    chips.append(f'<span class="chip c-dim">{_e(t["states"].get(card["state"], card["state"] or ""))}</span>')
    svg = sparkline_svg(card["spark"], thresholds, lang)
    spark = (
        f'<div class="spark">{svg}<div class="cap">{_e(t["spark_label"])}</div></div>'
        if svg else f'<div class="note">{_e(t["spark_none"])}</div>'
    )
    happened = "".join(f"<p>{_e(line)}</p>" for line in card["happened"])
    implies = "".join(f"<p>{_e(line)}</p>" for line in card["implies"])
    steps = card["fix"]
    lead = ""
    if steps and card["state"] != "active":
        lead, steps = f"<p>{_e(steps[0])}</p>", steps[1:]
    items = "".join(f"<li>{_e(step)}</li>" for step in steps)
    return (
        '<div class="card">'
        f'<h3>{_e(card["name"])}</h3>'
        f'<div class="chips">{"".join(chips)}</div>'
        f'<div class="row">{_e(t["last_activity"])} : {_e(stamp(card["last_activity"], lang))}</div>'
        f"{spark}"
        f'<div class="blk"><h4>{_e(t["happened"])}</h4>{happened}</div>'
        + (f'<div class="blk"><h4>{_e(t["implies"])}</h4>{implies}</div>' if implies else "")
        + (f'<div class="blk fix"><h4>{_e(t["fix"])}</h4>{lead}<ol>{items}</ol></div>' if (items or lead) else "")
        + "</div>"
    )


def _timeline_html(report, lang):
    t = tr(lang)
    if not report["timeline"]:
        return f'<p class="note">{_e(t["timeline_empty"])}</p>'
    long = bool(report["period"]["from"]) and (report["generated_at"] - report["period"]["from"]) > 180 * 86400
    items = []
    for entry in reversed(report["timeline"]):
        where = entry["name"] or t["unknown_session"]
        text = _event_text(entry, lang)
        items.append(
            f'<li class="k-{_e(entry["kind"])}">'
            f'<time datetime="{_e(iso_utc(entry["at"]))}"><b>{_e(stamp(entry["at"], lang, long))}</b></time>'
            f' — <b class="k">{_e(text)}</b> — {_e(where)}</li>'
        )
    omitted = (
        f'<p class="note">{_e(t["timeline_omitted"].format(n=report["timeline_omitted"]))}</p>'
        if report["timeline_omitted"] else ""
    )
    return f'<ul class="timeline">{"".join(items)}</ul>{omitted}'


def render_html(report):
    """One self-contained page: no external file, no data fetched, safe to open from disk."""
    lang = report["lang"]
    t = tr(lang)
    k = report["kpis"]
    period = report["period"]
    if period.get("days"):
        span = t["last_days"].format(n=period["days"])
    elif period["from"]:
        span = f'{t["since"]} {stamp(period["from"], lang)} {t["to_now"]}'
    else:
        span = t["period_all"]
    meta = (
        f'{t["generated"]} {stamp(report["generated_at"], lang)} '
        f'({t["local_time"]} UTC{utc_offset(report["generated_at"])}) · '
        f'{t["period"]} : {span} · hermes-gauge'
    )
    verdict = report["verdict"]
    kpis = (
        f'<div class="kpi viol"><b>{k["compactions"]}</b><span>{_e(t["kpi_compactions"])}</span></div>'
        f'<div class="kpi red"><b>{k["full_sessions"]}</b><span>{_e(t["kpi_full"])}</span></div>'
        f'<div class="kpi amb"><b>{k["watch_sessions"]}</b><span>{_e(t["kpi_watch"])}</span></div>'
        f'<div class="kpi"><b>{k["network_errors"]}</b><span>{_e(t["kpi_network"])}</span>'
        + (f'<small>{_e(t["kpi_missed_note"].format(n=k["missed_turns"]))}</small>' if k["missed_turns"] else "")
        + "</div>"
    )
    resets = f'<p class="note">{_e(t["resets_note"].format(n=k["resets"]))}</p>' if k["resets"] else ""
    cards = "".join(_card_html(card, report["thresholds"], lang) for card in report["cards"])
    if not cards:
        cards = f'<p class="note">{_e(t["no_cards"])}</p>'
    if report["cards_omitted"]:
        cards += f'<p class="note">{_e(t["more_cards"].format(n=report["cards_omitted"]))}</p>'
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{t["html_lang"]}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(t['title'])}</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>{_e(t['title'])}</h1>"
        f'<div class="meta">{_e(meta)}</div>'
        f'<h2 class="sec">{_e(t["sec_verdict"])}</h2>'
        f'<div class="verdict v-{_e(verdict["level"])}">{_e(verdict["message"])}</div>'
        f'<h2 class="sec">{_e(t["sec_fix"])}</h2>{cards}'
        f'<h2 class="sec">{_e(t["sec_status"])}</h2>'
        f'<div class="kpis">{kpis}</div>{resets}'
        f'<details open><summary>{_e(t["timeline"])}</summary>{_timeline_html(report, lang)}</details>'
        f'<details><summary>{_e(t["llm_summary"])}</summary>'
        f'<p class="note">{_e(t["llm_help"])}</p>'
        f'<button class="copybtn" id="copy" type="button" data-done="{_e(t["copied"])}" '
        f'data-manual="{_e(t["copy_manual"])}">{_e(t["copy"])}</button>'
        f'<pre id="llm">{_e(report["llm"])}</pre></details>'
        f"<script>{_JS}</script></div></body></html>\n"
    )
