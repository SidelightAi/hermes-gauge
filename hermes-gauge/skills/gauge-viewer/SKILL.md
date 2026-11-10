---
name: gauge-viewer
description: Read the context gauge — when and how to check how full the session is, and when to warn the user.
---

# gauge-viewer

Read the context gauge and act on it.

## When to check

- Before starting a long or multi-step task on an already-active session.
- Whenever the user asks "how full is the context", "can we still fit X", or mentions the gauge.
- After very long tool outputs or file dumps in a session.

## Natural commands

Answer in the user's language, French or English. Call a tool first; do not guess the fill.

- `gauge`, `GaugeCheck`, `où j'en suis` (context), "how full", "where am I on context" — call `gauge_check`. Reply in one line: fill, status, session. Example: `release-review — ≈92% FULL`.
- `gauge report`, "rapport jauge" — call `gauge_report`. Lead with the summary line (active sessions and the weakest link), then the per-session detail. Pass `format=md` when the user wants a note to forward, `format=json` when they want the stable object.
- `/gauge` is the same one-line check. `/gauge report`, `/gauge md`, and `/gauge json` are the exports. Add `write` (`/gauge report write`) to get the report as an attached `.md` file instead of a long message.
- `/gauge audit` — peaks, compactions and what the context is made of, per session. `/gauge incidents` — the incident report page (verdict line, then the file). `/gauge config` shows the preferences; `/gauge config set key=value` changes one. These are commands for the user to run, not tools: relay their output, do not recompute it.
- "Est-ce qu'il y a eu des incidents ?", "what went wrong with my context" — point to `/gauge incidents` and read its verdict line. A compaction resets the live counter, so only the incident report counts past incidents; do not answer from `gauge_check` alone.

## How

Call `gauge_check`. Read `status`, `recommendation`, `window_source`, and `fill_display`:

- **OK** (under the configured watch threshold, default 60%) — nothing to do.
- **WATCH** (watch ≤ fill < full, default 60–85%) — tell the user the headroom left in plain words; mention `in_flight` (what is in the window). One warning per crossing, not every check.
- **FULL** (at or above full, default 85%) — propose a compact summary, then a fresh session. Never silently drop context.
- **fill_display is "—"** — the max is unknown or compaction invalidated the anchor. Say so. Do not invent a percentage. A `window_source` of `fallback` is a labeled default, not a measured context length.
- **Several agents** — the fill sums their contributions. The status uses the smallest window. Repeat `window_label` ("window: smallest model of the subject") so nobody thinks the big model is the limit.
- **effective_window / degrade_factor** — the same family table as the desktop chip. First match: flash/lite/air/nano 0.25, mini/4o-mini/glm-5.2 0.40, haiku 0.50, sonnet/opus/gpt-5/gpt-6 and the other pro names 1.0, anything else 0.60. Status, remaining and fill are computed against effective_window; report both numbers. An exact `effective_models` id, an `effective_factors` substring, or a stored factor wins over the family.
- **`gauge_audit`** — audit of the whole context setup: fixed overhead (system/tools/skills), fallback/unknown window, sweet-spot mismatch (fill >= WATCH on effective but OK on nominal), heavyweight files (config.yaml, memories, skills). Use it when the user asks whether the setup is misconfigured.
- **`gauge_blame`** — which turns added the context, and `by_tool` when the hook stored tool names and result lengths from the payload. `attribution: turn-only` means those names were not in the payload; do not guess a tool.

If the hook missed a streamed or failed turn, call `gauge_check` again with `conversation_history` so it can recompute. `gauge_set_window` applies to the current session only.

## Policy

The plugin ships in `audit`. It watches the effective window and recommends. It never compacts a session and never starts a new one, in `audit` or in `act`. `act` only changes the push to an explicit "action recommandée". Compaction and the session cycle belong to Hermes core. Nothing ends a session by itself.

## Alert replies

A WATCH or FULL push is one message. It names the conversation and its fill, says what to do, and carries the dull-threshold recommendation when that is due on the same turn. The message is plain text: no button tap reaches the agent.

When the user answers, call `gauge_action` with that action and the session id: `compact+new` (also `1`), `compact` (`2`), `new` (`3`), or `dismiss` (`rien`). `dismiss` records the choice and quiets that status until it changes. The other three hand `/compact` and `/new` to Hermes. If the tool returns `executed: false`, tell the user the exact command from `commands`. The gauge never compacts a transcript by itself.

Read `policy_verdict` on `gauge_check` and `gauge_audit`: the dull threshold for this model and a short recommendation. `audit` pushes that recommendation once per crossing.

Change sensitivity with one line in the plugin `config.yaml`:

```yaml
policy:
  preset: prudent   # or standard, the default
```

`standard` is the shipped threshold (dull at 70% of the effective window, earlier for flash and mini). `prudent` warns earlier and stays in `audit`. An explicit `mode`, `dull_at`, `on_dull`, or `cooldown_minutes` next to `preset` wins over the bundle.

Call `gauge_policy` before editing the file. It returns the active policy, the source of each value (`default`, `preset:standard`, `preset:prudent`, or `config`), and what the plugin will and will not do. It does not write the file and it does not push.

## Reporting across sessions

Use `gauge_report` to list the fullest tracked sessions and which ones deserve a summary + retire. Read `summary` first: it names the weakest link (highest fill, active sessions first). `format` is `text`, `md`, or `json`; `write` saves the markdown and returns a `MEDIA:` line, which Hermes attaches.

Sessions carry the title and thread from Hermes (`name`), a `last_activity` time and a `state`: `active`, `frozen` (ended or quiet for over 24 hours), or `archived`. Name sessions by `name`, not by id. Only active sessions need a summary now; a frozen one at FULL is history, and a restart of it is the user's call.

## Alerts

WATCH and FULL crossings are pushed once per crossing, as one line. `alerts.route` decides where: `session` (default, the thread itself), `home`, or `custom:<platform>:<chat_id>`. With the default route, a user on several channels gets the alert in each concerned thread. Never compact or end a session because of an alert: propose it, and let the user decide.

## Boundary with `hermes doctor` (anti-duplication)

`hermes doctor` owns STATIC setup: dependencies, credentials, tool backends, gateways, profiles, security advisories. `gauge` + `gauge_audit` own RUNTIME context economics: fill, effective window after degradation, fixed overhead per turn, memory-store pressure, threshold sanity for the current model.

Rules:
- Never add dependency/credential/gateway checks to the gauge plugin — delegate to `hermes doctor`.
- `gauge_audit` embeds doctor's warnings under `doctor` (single entry point) but does not re-check or fix them; advise the user to run `hermes doctor --fix` when relevant.
- New audit ideas first ask: is this static (→ doctor) or runtime/context (→ gauge)?
