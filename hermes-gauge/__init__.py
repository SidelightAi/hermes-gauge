"""hermes-gauge — context gauge plugin. Wiring: schemas → handlers → register(ctx)."""

from pathlib import Path

from . import schemas, tools


def register(ctx):
    # Signature Hermes (hermes_cli/plugins.py):
    # register_tool(name, toolset, schema, handler, ...)
    tools.bind_context(ctx)
    ctx.register_tool("gauge_check", "gauge", schemas.GAUGE_CHECK, tools.gauge_check)
    ctx.register_tool("gauge_report", "gauge", schemas.GAUGE_REPORT, tools.gauge_report)
    ctx.register_tool("gauge_set_window", "gauge", schemas.GAUGE_SET_WINDOW, tools.gauge_set_window)
    ctx.register_tool("gauge_render", "gauge", schemas.GAUGE_RENDER, tools.gauge_render)
    ctx.register_tool("gauge_action", "gauge", schemas.GAUGE_ACTION, tools.gauge_action)
    register_command = getattr(ctx, "register_command", None)
    if register_command is not None:
        register_command(
            "gauge",
            tools.gauge_slash,
            "Show this session's fill, a report of every session, an audit of past peaks, an incident report, or the preferences.",
            "[report|audit|incidents|md|json|config]",
        )
    # post_llm_call only runs after a successful turn. Streaming chunks, a
    # stream that dies, and provider errors are separate Hermes hooks — the
    # same recorder handles all of them so a missed post_llm_call still leaves
    # a trace (and gauge_check can recompute).
    ctx.register_hook("post_llm_call", tools.handle_post_llm_call)
    ctx.register_hook("on_stream_delta", tools.handle_stream_delta)
    ctx.register_hook("on_stream_end", tools.handle_stream_end)
    ctx.register_hook("api_request_error", tools.handle_api_error)
    ctx.register_hook("post_api_request", tools.handle_post_api_request)
    ctx.register_hook("post_compaction", tools.handle_compaction)
    ctx.register_hook("on_session_reset", tools.handle_session_reset)
    ctx.register_hook("subagent_stop", tools.handle_subagent_stop)

    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
