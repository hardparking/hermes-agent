"""Claude CLI runtime — hands the entire turn to a persistent `claude`
subprocess speaking stream-json.

Analog of ``agent/codex_runtime.py``'s app-server path: Claude Code owns the
agentic loop (its native Bash/Read/Write/Edit tools, its own context and
compaction), Hermes' extra tool surface reaches it via the hermes-tools MCP
server, and the streamed events are projected back into Hermes' messages
list so transcripts, memory review, and the sessions DB keep working.

Selected when ``agent.api_mode == "claude_cli"`` (the default for the
claude-cli provider; set HERMES_CLAUDE_CLI_RUNTIME=shim to fall back to the
legacy one-shot OpenAI-compat shim in agent/claude_cli_client.py).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    if "/" in model:
        model = model.split("/", 1)[1]
    return model or None


def _resolve_effort() -> str | None:
    raw = os.getenv("HERMES_CLAUDE_CLI_EFFORT", "").strip().lower()
    if raw in _EFFORT_LEVELS:
        return raw
    return None


# Claude Code 2.x --permission-mode values, plus friendlier aliases.
_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
_PERMISSION_MODE_ALIASES = {
    "auto": "bypassPermissions",
    "yolo": "bypassPermissions",
    "bypass": "bypassPermissions",
    "bypasspermissions": "bypassPermissions",
    "acceptedits": "acceptEdits",
    "accept-edits": "acceptEdits",
}


def _resolve_permission_mode() -> str:
    """Pick the --permission-mode for the claude subprocess.

    HERMES_CLAUDE_CLI_PERMISSION_MODE wins when set (headless gateways have no
    one to answer control_request prompts, so "auto"/"bypassPermissions" is
    the usual choice there); otherwise Hermes' own approval-bypass state
    (--yolo, /yolo, approvals.mode: off) maps to bypassPermissions.
    """
    raw = os.getenv("HERMES_CLAUDE_CLI_PERMISSION_MODE", "").strip()
    if raw:
        mode = _PERMISSION_MODE_ALIASES.get(raw.lower(), raw)
        if mode in _PERMISSION_MODES:
            return mode
        logger.warning(
            "claude-cli: ignoring invalid HERMES_CLAUDE_CLI_PERMISSION_MODE=%r "
            "(valid: %s)",
            raw,
            ", ".join(sorted(_PERMISSION_MODES)),
        )
    try:
        from tools.approval import is_approval_bypass_active

        if is_approval_bypass_active():
            return "bypassPermissions"
    except Exception:
        logger.debug(
            "claude-cli: approval-bypass lookup failed; keeping default "
            "permission mode",
            exc_info=True,
        )
    return "default"


def _write_mcp_config(scratch_dir: str | None = None) -> str:
    """Write the per-session MCP config that lets the claude subprocess call
    back into Hermes' tool surface (web search, browser, vision, kanban, …)
    via ``agent.transports.hermes_tools_mcp_server`` over stdio.

    Passed with --strict-mcp-config so the user's own ~/.claude MCP servers
    don't double-load inside a gateway-driven session.
    """
    repo_root = str(Path(__file__).resolve().parents[1])
    config = {
        "mcpServers": {
            "hermes-tools": {
                "command": sys.executable,
                "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
                "env": {
                    "PYTHONPATH": repo_root,
                    "HERMES_QUIET": "1",
                },
            }
        }
    }
    fd, path = tempfile.mkstemp(
        prefix="hermes-claude-mcp-", suffix=".json", dir=scratch_dir or None
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    return path


def _build_permission_callback(agent):
    """Bridge claude's can_use_tool control_requests into Hermes' approval
    flow. Mirrors the codex app-server approval bridge: interactive CLI
    contexts prompt the user; gateway/cron contexts fail closed unless the
    user opted out of approvals (/yolo, approvals.mode: off) — in which case
    --permission-mode bypassPermissions is already set and this callback is
    only a backstop."""
    try:
        from tools.terminal_tool import _get_approval_callback

        approval_callback = _get_approval_callback()
    except Exception:
        approval_callback = None

    def _decide(tool_name: str, tool_input: dict) -> bool:
        if approval_callback is None:
            return False  # fail closed — same policy as codex runtime
        preview = ""
        if tool_name == "Bash":
            preview = str(tool_input.get("command") or "")
        elif tool_input:
            try:
                preview = json.dumps(tool_input, ensure_ascii=False)[:200]
            except Exception:
                preview = str(tool_input)[:200]
        description = f"Claude Code requests {tool_name}"
        try:
            choice = approval_callback(
                preview or tool_name, description, allow_permanent=False
            )
        except Exception:
            logger.exception("claude-cli approval callback raised")
            return False
        return choice in {"once", "session", "always"}

    return _decide


def _record_claude_cli_usage(agent, turn) -> dict[str, Any]:
    """Translate the claude CLI result usage block into Hermes accounting.

    The terminal result event reports input_tokens / output_tokens /
    cache_read_input_tokens / cache_creation_input_tokens for the whole turn
    (all inner API calls included).
    """
    agent.session_api_calls += 1

    usage = getattr(turn, "usage", None)
    if not isinstance(usage, dict) or not usage:
        if agent._session_db and agent.session_id:
            try:
                if not agent._session_db_created:
                    agent._ensure_db_session()
                agent._session_db.update_token_counts(
                    agent.session_id, model=agent.model, api_call_count=1
                )
            except Exception as exc:
                logger.debug(
                    "claude-cli api-call persistence failed (session=%s): %s",
                    agent.session_id, exc,
                )
        return {}

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    def _as_int(value: Any) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    canonical_usage = CanonicalUsage(
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        reasoning_tokens=0,
        raw_usage=usage,
    )
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = canonical_usage.total_tokens
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": 0,
    }

    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        try:
            compressor.update_from_response(usage_dict)
        except Exception:
            logger.debug("claude-cli usage update failed", exc_info=True)

    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical_usage.input_tokens
    agent.session_output_tokens += canonical_usage.output_tokens
    agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
    agent.session_cache_write_tokens += canonical_usage.cache_write_tokens

    cost_result = estimate_usage_cost(
        agent.model,
        canonical_usage,
        provider=agent.provider,
        base_url=agent.base_url,
        api_key=getattr(agent, "api_key", ""),
    )
    if cost_result.amount_usd is not None:
        agent.session_estimated_cost_usd += float(cost_result.amount_usd)
    agent.session_cost_status = cost_result.status
    agent.session_cost_source = cost_result.source

    if agent._session_db and agent.session_id:
        try:
            if not agent._session_db_created:
                agent._ensure_db_session()
            agent._session_db.update_token_counts(
                agent.session_id,
                input_tokens=canonical_usage.input_tokens,
                output_tokens=canonical_usage.output_tokens,
                cache_read_tokens=canonical_usage.cache_read_tokens,
                cache_write_tokens=canonical_usage.cache_write_tokens,
                estimated_cost_usd=float(cost_result.amount_usd)
                if cost_result.amount_usd is not None else None,
                cost_status=cost_result.status,
                cost_source=cost_result.source,
                billing_provider=agent.provider,
                billing_base_url=agent.base_url,
                billing_mode="subscription_included"
                if cost_result.status == "included" else None,
                model=agent.model,
                api_call_count=1,
            )
        except Exception as exc:
            logger.debug(
                "claude-cli token persistence failed (session=%s, tokens=%d): %s",
                agent.session_id, total_tokens, exc,
            )

    return {
        **usage_dict,
        "last_prompt_tokens": prompt_tokens,
        "estimated_cost_usd": float(cost_result.amount_usd)
        if cost_result.amount_usd is not None else None,
        "cost_status": cost_result.status,
        "cost_source": cost_result.source,
    }


def _ensure_claude_cli_session(agent):
    """Get or build the per-agent ClaudeCliSession. Rebuilds (carrying the
    Claude session id forward for --resume) when the model, cwd, or resolved
    permission mode changed mid-session, e.g. a /model switch or /yolo."""
    from agent.transports.claude_cli_session import ClaudeCliSession

    model = _normalize_model(getattr(agent, "model", None))
    from agent.runtime_cwd import resolve_agent_cwd

    cwd = getattr(agent, "session_cwd", None) or str(resolve_agent_cwd())
    permission_mode = _resolve_permission_mode()

    existing = getattr(agent, "_claude_cli_session", None)
    if existing is not None:
        if (
            existing._model == model
            and existing._cwd == cwd
            and existing._permission_mode == permission_mode
        ):
            return existing
        carried_session_id = existing.session_id
        try:
            existing.close()
        except Exception:
            pass
        agent._claude_cli_session = None
    else:
        carried_session_id = getattr(agent, "_claude_cli_session_id", None)

    command = os.getenv("HERMES_CLAUDE_CLI_COMMAND", "").strip() or "claude"
    raw_extra = os.getenv("HERMES_CLAUDE_CLI_ARGS", "").strip()
    extra_args = shlex.split(raw_extra) if raw_extra else []

    def _on_stream_delta(text: str) -> None:
        agent._fire_stream_delta(text)

    def _on_thinking_delta(text: str) -> None:
        agent._fire_reasoning_delta(text)

    def _on_tool_started(name: str, args: dict) -> None:
        progress_callback = getattr(agent, "tool_progress_callback", None)
        if progress_callback is None:
            return
        preview = ""
        if name == "Bash":
            preview = str(args.get("command") or "")
        elif args:
            try:
                preview = json.dumps(args, ensure_ascii=False)[:120]
            except Exception:
                preview = ""
        try:
            progress_callback("tool.started", name, preview, args)
        except Exception:
            logger.debug("claude-cli tool-progress callback raised", exc_info=True)

    session = ClaudeCliSession(
        cwd=cwd,
        command=command,
        model=model,
        effort=_resolve_effort(),
        permission_mode=permission_mode,
        system_prompt=getattr(agent, "_cached_system_prompt", None) or "",
        mcp_config_file=_write_mcp_config(),
        extra_args=extra_args,
        permission_callback=_build_permission_callback(agent),
        on_stream_delta=_on_stream_delta,
        on_thinking_delta=_on_thinking_delta,
        on_tool_started=_on_tool_started,
        initial_session_id=carried_session_id,
    )
    agent._claude_cli_session = session
    return session


# Reseed cap mirrors OpenClaw's default: enough tail context to restore the
# thread without burning the fresh session's first prompt on ancient history.
_RESEED_MAX_CHARS = 12_000


def _maybe_reseed_input(session, messages: List[Dict[str, Any]], user_message: str) -> str:
    """When a fresh claude session starts mid-conversation (gateway agent
    rebuilt after eviction/restart/model switch), Claude Code's internal
    context is empty but Hermes still holds the transcript. Wrap the first
    prompt in a conversation-history envelope so the thread continues
    coherently — OpenClaw's reseed-from-transcript pattern.

    No-op when the session already has a Claude session id (live process or
    --resume respawn: Claude Code restores its own context)."""
    if session.session_id is not None:
        return user_message

    history = [
        m for m in messages
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
    ]
    # The current user message is already appended by run_conversation's
    # prologue — the envelope carries it separately.
    if history and history[-1].get("role") == "user":
        history = history[:-1]
    if not history:
        return user_message

    rendered: list[str] = []
    for m in history:
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        label = "User" if m.get("role") == "user" else "Assistant"
        rendered.append(f"{label}: {content.strip()}")
    if not rendered:
        return user_message

    transcript = "\n\n".join(rendered)
    if len(transcript) > _RESEED_MAX_CHARS:
        transcript = "[...earlier conversation truncated...]\n\n" + transcript[-_RESEED_MAX_CHARS:]

    return (
        "<conversation_history>\n"
        "The following is the conversation so far; your runtime was restarted "
        "and this restores your context. Do not summarize or acknowledge it — "
        "just continue the conversation naturally.\n\n"
        f"{transcript}\n"
        "</conversation_history>\n"
        "<next_user_message>\n"
        f"{user_message}\n"
        "</next_user_message>"
    )


def run_claude_cli_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """Claude CLI runtime path. Hands the entire turn to the persistent
    `claude` subprocess and projects its events back into Hermes' messages
    list. Called from run_conversation() when agent.api_mode == "claude_cli".
    Returns the same dict shape as the chat_completions path.

    NOTE: the user message is ALREADY appended to `messages` by
    run_conversation()'s prologue; Claude Code holds the real conversation
    context internally, so only the new user text is written to the child.
    """
    session = _ensure_claude_cli_session(agent)
    turn_input = _maybe_reseed_input(session, messages, user_message)

    try:
        turn = session.run_turn(
            user_input=turn_input,
            interrupt_check=lambda: bool(agent._interrupt_requested),
        )
    except Exception as exc:
        logger.exception("claude-cli turn failed")
        try:
            session.close()
        except Exception:
            pass
        agent._claude_cli_session = None
        return {
            "final_response": f"Claude CLI turn failed: {exc}",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "error": str(exc),
        }

    # Keep the Claude session id on the agent so a rebuilt session object
    # (model switch, retire) can --resume the same conversation.
    if turn.session_id:
        agent._claude_cli_session_id = turn.session_id
    if turn.session_invalid:
        agent._claude_cli_session_id = None

    if turn.should_retire:
        logger.warning(
            "claude-cli session retired (turn error: %s)", turn.error
        )
        # run_turn already tore the subprocess down; keep the session object
        # so the next turn respawns with --resume.

    # Splice projected messages into the conversation — standard
    # {role, content, tool_calls, tool_call_id} rows, which is what the
    # curator / sessions DB expect. Same persistence contract as the codex
    # app-server path (agent_persisted=True below): we flush here, the
    # gateway skips its own write.
    if turn.projected_messages:
        messages.extend(turn.projected_messages)
        if getattr(agent, "_session_db", None) is not None:
            try:
                agent._flush_messages_to_session_db(messages)
            except Exception:
                logger.debug(
                    "claude-cli projected-message flush failed", exc_info=True
                )

    agent._iters_since_skill = (
        getattr(agent, "_iters_since_skill", 0) + turn.tool_iterations
    )
    usage_result = _record_claude_cli_usage(agent, turn)

    should_review_skills = False
    if (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in agent.valid_tool_names
    ):
        should_review_skills = True
        agent._iters_since_skill = 0

    if not turn.interrupted and turn.error is None:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
                messages=messages,
            )
        except Exception:
            logger.debug("external memory sync raised", exc_info=True)

    if (
        turn.final_text
        and not turn.interrupted
        and (should_review_memory or should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=should_review_memory,
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("background review spawn raised", exc_info=True)

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "error": turn.error,
        "agent_persisted": True,
        "claude_session_id": turn.session_id,
        **usage_result,
    }


__all__ = ["run_claude_cli_turn"]
