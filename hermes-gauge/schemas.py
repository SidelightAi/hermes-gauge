"""Tool schemas — what the LLM sees."""

GAUGE_CHECK = {
    "name": "gauge_check",
    "description": (
        "Check how full the current session's context window is. "
        "Returns an estimated fill (0-100%, prefixed with ≈ when it is not a "
        "provider token count), the window size, and window_source "
        "(detected, configured, fallback, or unresolved). When the max is "
        "unknown or the usage anchor was invalidated by compaction, fill_display "
        "is an em dash and no percentage is invented. Also returns a breakdown "
        "(system_prompt, tools, skills, conversation), status OK / WATCH / FULL, "
        "and a plain recommendation. On a subject with several agents the fill "
        "sums their contributions and the status uses the smallest window. "
        "Call it when a session has been running long, and whenever the "
        "automatic hook may have missed a streamed or failed turn — pass "
        "conversation_history in that case so the gauge can recompute."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "Optional model name. Does not invent a window from an alias.",
            },
            "conversation_history": {
                "type": "array",
                "description": "Optional transcript. Used to recompute the gauge when the post_llm_call hook did not record the turn.",
            },
            "recalculate": {
                "type": "boolean",
                "description": "Force an on-demand recompute from conversation_history or content.",
            },
        },
        "required": [],
    },
}

GAUGE_REPORT = {
    "name": "gauge_report",
    "description": (
        "Report context usage across all tracked sessions. The first line is "
        "the session count and the weakest link (highest fill among active "
        "sessions). Sessions carry their Hermes title, last activity, and "
        "an active, frozen or archived state. "
        "format text (default) adds a plain report, md a markdown document "
        "for chat attachments, json the stable data object. Use it to decide "
        "which sessions are near the limit or should be summarized and retired."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max sessions to return, most full first (default 20).",
            },
            "format": {
                "type": "string",
                "enum": ["text", "md", "json"],
                "description": "text (default), md, or json.",
            },
            "write": {
                "type": "boolean",
                "description": (
                    "Write the full report to a .md file and return its path "
                    "as a MEDIA: line, so the chat sends it as an attachment "
                    "instead of pasting a long table. Use it for any report "
                    "or comparison the user will read."
                ),
            },
        },
        "required": [],
    },
}

GAUGE_RENDER = {
    "name": "gauge_render",
    "description": (
        "Render the current global gauge as a standalone SVG string: a "
        "segmented arc of pills on #032A27, amber #E8B54A, terracotta "
        "#C45C48 when the fullest session is FULL. The picture is the "
        "weakest link across tracked sessions, not one file per session. "
        "Set write true to save it at "
        "~/.hermes/plugin-data/hermes-gauge/gauge.svg."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "write": {
                "type": "boolean",
                "description": "Write the single gauge.svg next to the plugin database.",
            },
        },
        "required": [],
    },
}

GAUGE_SET_WINDOW = {
    "name": "gauge_set_window",
    "description": (
        "Override the context-window size (in tokens) for the current session "
        "only, or clear that session's override. The value never leaks onto "
        "another session. Omit window_tokens to reset. The response includes "
        "window_source configured."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "Model name this session is running.",
            },
            "window_tokens": {
                "type": "integer",
                "description": "Context window size in tokens. Omit to reset this session's override.",
            },
            "session_id": {
                "type": "string",
                "description": "Session the override applies to. Hermes injects this; do not copy it onto other sessions.",
            },
        },
        "required": ["model"],
    },
}

GAUGE_ACTION = {
    "name": "gauge_action",
    "description": (
        "Carry out the user's answer to a Hermes Gauge alert on one session. "
        "Call it when they reply to a WATCH or FULL notice. "
        "action is compact, new, compact+new, or dismiss "
        "(Telegram replies: 1 = compact+new, 2 = compact, 3 = new, rien = dismiss). "
        "dismiss records the choice and quiets that status until it changes. "
        "The other actions ask Hermes to run /compact and /new on that session. "
        "This tool does not summarize the transcript itself. "
        "session_id is the session named in the alert."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["compact", "new", "compact+new", "dismiss"],
                "description": "compact, new, compact+new, or dismiss. Also accepts 1, 2, 3, or rien.",
            },
            "session_id": {
                "type": "string",
                "description": "Session the alert named. Hermes injects this; keep it on that session.",
            },
        },
        "required": ["action", "session_id"],
    },
}
