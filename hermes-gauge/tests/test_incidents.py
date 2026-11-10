"""Incident report: verdict, cards, KPIs, chronology, page, block for an LLM."""

import html
import importlib.util
import re
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_gauge_incidents_under_test", HERE / "incidents.py")
incidents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(incidents)

NOW = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
HOUR = 3600.0
DAY = 86_400.0
WATCH, FULL = 0.60, 0.85


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setattr(incidents, "_localtime", time.gmtime)


def event(reason, at, tokens=150_000, pct=75.0):
    return {
        "session_id": "x", "reason": reason, "created": at,
        "snapshot": {"estimated_tokens": tokens, "fill_percent": pct, "model": "m", "turns": 5,
                     "breakdown": {"system_prompt": 0, "tools": 50_000, "skills": 0, "conversation": 100_000}},
    }


def row(sid, name, *, state="active", status="OK", pct=20.0, events=(), series=(), turns=12,
        window=200_000, last=NOW - 600):
    return {
        "session_id": sid, "name": name, "title": name, "state": state, "last_activity": last,
        "model": "m",
        "reading": {
            "estimated_tokens": int(window * pct / 100), "fill_percent": pct, "fill_display": f"{pct}%",
            "status": status, "window_tokens": window, "window_source": "detected", "turns": turns,
            "model": "m",
            "breakdown": {"system_prompt": 1_000, "tools": 20_000, "skills": 0, "conversation": 40_000},
        },
        "events": list(events), "series": list(series),
    }


def hook(sid, kind, at, detail="Connection error."):
    return {"session_id": sid, "kind": kind, "detail": detail, "created": at}


def collect(rows, hooks=(), **kw):
    kw.setdefault("watch", WATCH)
    kw.setdefault("full", FULL)
    kw.setdefault("now", NOW)
    return incidents.collect(rows, list(hooks), **kw)


def scenario():
    return collect(
        [
            row("a", "Launch plan · fil 7", status="WATCH", pct=70.0, turns=40, events=[
                event("proactive_prune", NOW - 5 * HOUR), event("post_tool", NOW - 4 * HOUR),
                event("post_tool", NOW - 3 * HOUR), event("new_session", NOW - 2 * HOUR),
            ], series=[(NOW - 5 * HOUR + i * 600, 20_000 + i * 6_000) for i in range(30)]),
            row("b", "Vendor contract", state="frozen", status="FULL", pct=92.0, events=[
                event("post_tool", NOW - 3 * DAY)], last=NOW - 3 * DAY),
            row("c", "Calm chat", pct=10.0),
            row("d", "Flaky link", pct=15.0),
        ],
        [
            hook("d", "provider_error", NOW - 2 * HOUR, "Connection error."),
            hook("d", "missed", NOW - 2 * HOUR, "post_llm_call did not run after an incomplete stream"),
            hook("a", "provider_error", NOW - HOUR, "[Errno 32] Broken pipe"),
            hook("", "missed", NOW - 30 * 60, "gauge_check flushed a stream the end hook never closed"),
            hook("a", "other_kind", NOW - 20 * 60, "ignored"),
        ],
    )


# --- numbers -------------------------------------------------------------------


def test_kpis_count_compactions_apart_from_resets_and_errors_apart_from_missed_turns():
    report = scenario()
    assert report["kpis"] == {
        "compactions": 4,        # 3 on a, 1 on b — the /new on a is a reset
        "resets": 1,
        "full_sessions": 0,      # b is FULL but frozen: nobody is working there
        "watch_sessions": 1,     # a
        "idle_full_sessions": 1, # b stays in the amber verdict
        "network_errors": 2,
        "missed_turns": 2,       # one of them has no session
    }
    assert report["unattributed_events"] == 1


def test_only_touched_sessions_get_a_card_worst_first():
    report = scenario()
    # Calm chat has nothing to report.
    # Worst first among sessions someone is working in; the frozen one comes last.
    assert [c["name"] for c in report["cards"]] == ["Launch plan · fil 7", "Flaky link", "Vendor contract"]
    first = report["cards"][0]
    assert (first["compactions"], first["resets"], first["network_errors"], first["missed_turns"]) == (3, 1, 1, 0)
    assert first["status"] == "WATCH" and first["state"] == "active"


def test_a_session_close_to_full_gets_a_card_even_without_any_incident():
    report = collect([row("a", "Almost there", status="FULL", pct=93.0)])
    assert [c["name"] for c in report["cards"]] == ["Almost there"]
    assert report["verdict"]["level"] == "red"
    assert report["kpis"]["compactions"] == 0


def test_timeline_is_chronological_with_real_timestamps_and_keeps_resets_visible():
    report = scenario()
    stamps = [e["at"] for e in report["timeline"]]
    assert stamps == sorted(stamps)
    kinds = [e["kind"] for e in report["timeline"]]
    assert kinds.count("compaction") == 4 and kinds.count("reset") == 1
    assert kinds.count("network_error") == 2 and kinds.count("missed_turn") == 2
    assert "other_kind" not in kinds
    reset = next(e for e in report["timeline"] if e["kind"] == "reset")
    assert reset["reason"] == "new_session" and reset["name"] == "Launch plan · fil 7"
    orphan = next(e for e in report["timeline"] if e["session_id"] is None)
    assert orphan["kind"] == "missed_turn" and orphan["name"] is None
    assert report["period"]["from"] == NOW - 3 * DAY


def test_days_keeps_only_the_recent_window():
    report = collect(
        [row("b", "Vendor contract", events=[event("post_tool", NOW - 3 * DAY), event("post_tool", NOW - HOUR)])],
        [hook("b", "provider_error", NOW - 4 * DAY)],
        days=2,
    )
    assert report["kpis"]["compactions"] == 1
    assert report["kpis"]["network_errors"] == 0
    assert report["period"]["days"] == 2


def test_a_long_log_is_cut_to_the_latest_entries_and_says_so():
    many = [event("post_tool", NOW - i * 60) for i in range(incidents.TIMELINE_MAX + 25)]
    report = collect([row("a", "Busy", events=many)])
    assert len(report["timeline"]) == incidents.TIMELINE_MAX
    assert report["timeline_omitted"] == 25
    assert report["kpis"]["compactions"] == incidents.TIMELINE_MAX + 25  # counted before cutting
    assert report["timeline"][-1]["at"] == NOW - 0
    assert "25 older entries not shown" in incidents.render_html(
        collect([row("a", "Busy", events=many)], lang="en")
    )


# --- verdict ---------------------------------------------------------------------


def test_verdict_levels():
    assert collect([row("a", "Calm")])["verdict"]["level"] == "green"
    assert collect([row("a", "Calm")])["verdict"]["message"].startswith("Non")

    past = collect([row("a", "Was busy", events=[event("post_tool", NOW - DAY)])])
    assert past["verdict"]["level"] == "amber"
    assert past["verdict"]["message"].startswith("Oui, mais rien de critique")
    assert "1 compactage" in past["verdict"]["message"] and "compactage(s)" not in past["verdict"]["message"]

    watching = collect([row("a", "Climbing", status="WATCH", pct=70.0)])
    assert watching["verdict"]["level"] == "amber"
    assert watching["verdict"]["message"].startswith("Pas d'incident enregistré, mais 1 session")

    red = collect([row("a", "Full", status="FULL", pct=95.0, events=[event("post_tool", NOW - HOUR)])])
    assert red["verdict"]["level"] == "red"
    assert red["verdict"]["message"].startswith("Oui — 1 compactage — et 1 session active est presque pleine")


def test_network_errors_alone_make_the_verdict_amber_not_red():
    report = collect([row("a", "Flaky")], [hook("a", "provider_error", NOW - HOUR)])
    assert report["verdict"]["level"] == "amber"
    assert "1 erreur réseau" in report["verdict"]["message"]


# --- advice ----------------------------------------------------------------------


def test_advice_for_an_active_full_session_is_summarize_then_new_then_regive():
    card = collect([row("a", "Full", status="FULL", pct=95.0, events=[event("post_tool", NOW - HOUR)])])["cards"][0]
    assert any("résumé" in line or "Résume" in line for line in card["fix"])
    assert any("/new" in line for line in card["fix"])
    assert any("éléments clés" in line for line in card["fix"])
    assert any("95%" in line for line in card["happened"])


def test_advice_for_a_frozen_session_says_nothing_is_urgent():
    report = scenario()
    card = next(c for c in report["cards"] if c["name"] == "Vendor contract")
    assert card["fix"][0].startswith("Personne ne travaille dans cette session")


def test_advice_for_a_session_summarized_three_times_suggests_splitting():
    report = scenario()
    card = report["cards"][0]
    assert any("Découpe" in line for line in card["fix"])
    other = next(c for c in report["cards"] if c["name"] == "Vendor contract")
    assert not any("Découpe" in line for line in other["fix"])


def test_advice_for_a_network_error_only_says_nothing_is_corrupted():
    card = collect([row("a", "Flaky")], [hook("a", "provider_error", NOW - HOUR)])["cards"][0]
    assert any("Rien n'est corrompu" in line for line in card["implies"])
    assert card["fix"] == ["Si une réponse s'est arrêtée net, redemande-la. Sinon, rien à faire."]


def test_english_advice():
    card = collect([row("a", "Full", status="FULL", pct=95.0)], lang="en-US")["cards"][0]
    assert card["fix"][0].startswith("Ask the agent")
    assert incidents.normalize_lang("EN") == "en" and incidents.normalize_lang("") == "en"
    assert incidents.normalize_lang("fr") == "fr"


# --- page ------------------------------------------------------------------------


def test_page_answers_in_order_and_is_responsive():
    page = incidents.render_html(scenario())
    assert page.startswith("<!DOCTYPE html>")
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in page
    one, two, three = (page.index(x) for x in ("1 · Y a-t-il", "2 · Comment corriger", "3 · État des lieux"))
    assert one < two < three
    assert page.index('class="verdict v-amber"') < page.index('class="card"') < page.index('class="kpis"')
    assert page.index('class="kpis"') < page.index("Chronologie complète") < page.index('id="llm"')
    assert "@media(max-width:600px)" in page
    assert "overflow-wrap:anywhere" in page


def test_page_has_four_kpis_and_a_note_about_resets():
    page = incidents.render_html(scenario())
    assert len(re.findall(r'class="kpi(?: \w+)?"', page)) == 4
    assert "compactages (contexte résumé)" in page and "erreurs réseau" in page
    assert "+ 2 tour(s) non mesuré(s)" in page
    assert "1 remise(s) à zéro (/new) ne sont pas comptées" in html.unescape(page)


def test_page_is_self_contained():
    page = incidents.render_html(scenario())
    assert not re.search(r'(?:src|href)\s*=\s*"(?:https?:)?//', page)
    assert "<link " not in page and "<img" not in page
    assert page.count("<script") == 1  # the copy button, nothing else


def test_everything_user_supplied_is_escaped():
    evil = '<img src=x onerror=alert(1)> & "quotes" </script><script>alert(2)</script>'
    report = collect(
        [row("a", evil, status="FULL", pct=95.0, events=[event("</script><b>", NOW - HOUR)])],
        [hook("a", "provider_error", NOW - HOUR, evil)],
    )
    page = incidents.render_html(report)
    assert "<img src=x" not in page and "alert(2)</script>" not in page
    assert page.count("<script") == 1 and page.count("</script>") == 1
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "</script><b>" not in page


def test_llm_block_sits_in_a_pre_with_a_copy_button_and_a_fallback():
    page = incidents.render_html(scenario())
    assert '<button class="copybtn" id="copy"' in page
    assert "document.execCommand('copy')" in page and "navigator.clipboard" in page
    block = re.search(r'<pre id="llm">(.*?)</pre>', page, re.S).group(1)
    assert "# Rapport Hermes Gauge — à transmettre à un LLM" in block
    assert "## Demande" in block


def test_sparkline_is_drawn_from_dated_turns_and_marks_compactions():
    page = incidents.render_html(scenario())
    assert page.count("<svg") == 1  # only the session with dated turns
    svg = re.search(r"<svg.*?</svg>", page, re.S).group(0)
    assert "<polyline" in svg and 'stroke="#c9a0ff"' in svg  # compaction marks
    assert svg.count("stroke-dasharray") == 2  # WATCH and FULL guides
    assert "Pas encore assez de mesures datées" in page  # the others say so


def test_sparkline_keeps_the_peak_when_it_thins_a_long_series():
    series = [(NOW - 10 * HOUR + i * 30, 20_000) for i in range(400)]
    series[217] = (series[217][0], 190_000)
    spark = collect([row("a", "Long", events=[event("post_tool", NOW - HOUR)], series=series)])["cards"][0]["spark"]
    assert len(spark["points"]) == incidents.SPARK_POINTS
    assert max(p[1] for p in spark["points"]) == 95.0


def test_sparkline_needs_a_known_window_and_two_points():
    one = collect([row("a", "One", events=[event("post_tool", NOW)], series=[(NOW - 60, 40_000)])])["cards"][0]
    assert one["spark"]["points"] == []
    assert incidents.sparkline_svg(one["spark"], {"watch": WATCH, "full": FULL}, "fr") == ""


def test_page_in_english():
    page = incidents.render_html(collect([row("a", "Full", status="FULL", pct=95.0)], lang="en"))
    assert '<html lang="en">' in page
    assert "1 · Were there incidents?" in page and "How to fix" in page
    assert "Hermes Gauge report — to hand to an LLM" in page


def test_empty_report_is_a_calm_page():
    report = collect([])
    page = incidents.render_html(report)
    assert report["verdict"]["level"] == "green"
    text = html.unescape(page)
    assert "Aucune session à corriger" in text and "Rien n'a été enregistré" in text
    assert report["timeline"] == [] and report["cards"] == []


def test_stamps_are_local_time_with_the_offset_named():
    page = incidents.render_html(scenario())
    assert "heure locale de la machine UTC+0000" in page
    assert '<time datetime="2023-11-14T20:13:20Z"><b>14.11 20:13</b></time>' in page


# --- block for an LLM ---------------------------------------------------------------


def test_llm_block_is_autonomous_and_lists_facts_rules_and_the_ask():
    block = scenario()["llm"]
    assert block.index("## Synthèse") < block.index("## Incidents (ordre chronologique)") < block.index("## Données par session")
    assert block.index("## Données par session") < block.index("## Règles connues") < block.index("## Demande")
    assert "- 4 compactages" in block and "- 1 remise à zéro (/new), non comptée comme incident." in block
    assert "- 2 erreurs réseau et 2 tours non mesurés." in block
    assert "- 14.11 17:13 — Launch plan · fil 7 : Compactage (résumé automatique)" in block
    assert "Erreur réseau — [Errno 32] Broken pipe" in block
    assert "(session inconnue)" not in block and "session inconnue : Tour non mesuré" in block
    assert "Launch plan · fil 7 — 70% de remplissage (WATCH), 40 tours, 3 compactages, 1 erreur réseau" in block
    assert "WATCH à 60%, FULL à 85%" in block
    assert "ne compacte, n'archive et ne tue jamais" in block
    assert block.rstrip().endswith("et quelle cadence de suivi.")


def test_llm_block_follows_the_configured_thresholds_and_language():
    report = collect([row("a", "Chat", events=[event("post_tool", NOW - HOUR)])], watch=0.5, full=0.9, lang="en")
    block = report["llm"]
    assert "Thresholds: WATCH at 50%, FULL at 90%" in block
    assert "# Hermes Gauge report — to hand to an LLM" in block
    assert block.rstrip().endswith("and how often to check.")


def test_llm_block_caps_its_chronology():
    many = [event("post_tool", NOW - i * 60) for i in range(incidents.LLM_TIMELINE_MAX + 10)]
    block = collect([row("a", "Busy", events=many)])["llm"]
    assert "- … 10 événements plus anciens omis." in block
    assert block.count(" : Compactage") == incidents.LLM_TIMELINE_MAX
