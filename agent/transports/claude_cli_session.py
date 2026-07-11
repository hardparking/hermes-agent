"""Session adapter for the claude-cli stream-json runtime.

Owns one persistent `claude` subprocess per Hermes session. Drives user
turns over stdin, consumes stream-json events, projects Claude Code's
assistant/tool activity into Hermes' OpenAI-format messages list, bridges
permission control_requests into Hermes' approval flow, and translates
cancellation.

Lifecycle:
    session = ClaudeCliSession(cwd=..., model="claude-opus-4-8",
                               system_prompt="...", mcp_config_file=...)
    result = session.run_turn(user_input="hello")   # blocks until result event
    # result.final_text          → assistant text
    # result.projected_messages  → {role, content, tool_calls, tool_call_id} rows
    # result.interrupted / error / should_retire
    session.close()

Claude Code owns the conversation context inside the subprocess (including
its own compaction), so Hermes does NOT resend history — each run_turn writes
only the new user message. If the process is retired (timeout, crash, error),
the next turn respawns with `--resume <session_id>` so context survives.

The structure deliberately mirrors CodexAppServerSession; the wire protocol
and flag set mirror OpenClaw's claude-cli live-session backend.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from agent.transports.claude_cli_transport import (
    HERMES_MCP_TOOL_PREFIX,
    ClaudeCliProcess,
    build_claude_live_args,
)

logger = logging.getLogger(__name__)

_STDERR_TAIL_LINES = 12

# Quiet-window defaults. While a native tool (Bash, Write, ...) is executing,
# the CLI emits nothing — a long build being quiet is not a wedged process,
# so the no-output window stretches while tool calls are outstanding.
_DEFAULT_NO_OUTPUT_TIMEOUT = 180.0
_TOOL_OUTSTANDING_NO_OUTPUT_TIMEOUT = 900.0

# After send_interrupt(), how long to wait for the CLI to wind the turn down
# gracefully before killing the process.
_INTERRUPT_GRACE_SECONDS = 5.0

# Substrings in stderr/result text that mean the CLI's login is broken and
# the user has to re-auth, mirroring codex's OAuth classification.
_AUTH_FAILURE_HINTS = (
    "not logged in",
    "please run /login",
    "please log in",
    "invalid api key",
    "oauth token has expired",
    "oauth token expired",
    "authentication_error",
    "credit balance is too low",
    "401",
)

# result.subtype values that indicate the session id is unusable and a
# resume would fail the same way — force a fresh session instead.
_SESSION_INVALID_HINTS = (
    "no conversation found",
    "session not found",
)


@dataclass
class ClaudeTurnResult:
    """Result of one user→assistant turn through the claude CLI."""

    final_text: str = ""
    projected_messages: list[dict] = field(default_factory=list)
    tool_iterations: int = 0
    interrupted: bool = False
    error: Optional[str] = None
    session_id: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: Optional[float] = None
    num_turns: int = 0
    # The subprocess should be torn down and respawned (with --resume) on
    # the next turn instead of reused.
    should_retire: bool = False
    # The captured session id itself is unusable (CLI couldn't resume it) —
    # the next spawn must start fresh rather than --resume.
    session_invalid: bool = False


def _coerce_turn_input_text(user_input: Any) -> str:
    """Collapse rich content into the text sent on stdin. Mirrors the codex
    adapter: keep text parts, mark images with a placeholder."""
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        parts: list[str] = []
        for item in user_input:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item_type in {"image", "image_url", "input_image"}:
                parts.append("[image attached]")
        text = "\n\n".join(p for p in parts if p).strip()
        return text or "What do you see in this image?"
    return "" if user_input is None else str(user_input)


def _render_result_content(content: Any) -> str:
    """Render a tool_result block's content (string or content-block list)
    into the plain string Hermes stores on {role: tool} messages."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def _hermes_tool_name(raw_name: str) -> str:
    """Strip the MCP prefix from Hermes' own tools so the transcript and
    tool-progress breadcrumbs show `web_search`, not
    `mcp__hermes-tools__web_search`. Native/other-MCP names pass through."""
    if raw_name.startswith(HERMES_MCP_TOOL_PREFIX):
        return raw_name[len(HERMES_MCP_TOOL_PREFIX):]
    return raw_name


def _classify_auth_failure(*parts: str) -> Optional[str]:
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return None
    for needle in _AUTH_FAILURE_HINTS:
        if needle in haystack:
            return (
                "Claude Code authentication failed — the `claude` CLI's login "
                "looks expired or invalid. Run `claude` interactively and "
                "`/login` (or set CLAUDE_CODE_OAUTH_TOKEN), then retry."
            )
    return None


class ClaudeCliSession:
    """One persistent claude CLI conversation, lifetime owned by AIAgent.

    Not thread-safe — one caller drives it at a time, matching
    run_conversation(). request_interrupt() may be called from another
    thread (it only sets an Event).
    """

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        command: str = "claude",
        model: Optional[str] = None,
        effort: Optional[str] = None,
        permission_mode: str = "default",
        system_prompt: Optional[str] = None,
        mcp_config_file: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
        scratch_dir: Optional[str] = None,
        permission_callback: Optional[Callable[[str, dict], bool]] = None,
        on_stream_delta: Optional[Callable[[str], None]] = None,
        on_thinking_delta: Optional[Callable[[str], None]] = None,
        on_tool_started: Optional[Callable[[str, dict], None]] = None,
        process_factory: Optional[Callable[..., ClaudeCliProcess]] = None,
        initial_session_id: Optional[str] = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._command = command
        self._model = model
        self._effort = effort
        self._permission_mode = permission_mode
        self._system_prompt = system_prompt or ""
        self._mcp_config_file = mcp_config_file
        self._extra_args = list(extra_args or [])
        self._scratch_dir = scratch_dir
        self._permission_callback = permission_callback
        self._on_stream_delta = on_stream_delta
        self._on_thinking_delta = on_thinking_delta
        self._on_tool_started = on_tool_started
        self._process_factory = process_factory or ClaudeCliProcess

        self._proc: Optional[ClaudeCliProcess] = None
        # Carrying an id across session objects (model switch, gateway agent
        # re-hydration) makes the first spawn a --resume instead of fresh.
        self._session_id: Optional[str] = initial_session_id or None
        self._system_prompt_file: Optional[str] = None
        self._interrupt_event = threading.Event()
        self._closed = False

    # ---------- lifecycle ----------

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def ensure_started(self) -> None:
        """Spawn the subprocess if needed. Fresh sessions get a minted
        --session-id; respawns after a retire use --resume so Claude Code
        restores the conversation context. Idempotent."""
        if self._proc is not None and self._proc.is_alive():
            return
        resume_id: Optional[str] = None
        fresh_id: Optional[str] = None
        if self._session_id:
            resume_id = self._session_id
        else:
            fresh_id = str(uuid.uuid4())

        args = build_claude_live_args(
            model=self._model,
            effort=self._effort,
            permission_mode=self._permission_mode,
            system_prompt_file=self._ensure_system_prompt_file(),
            mcp_config_file=self._mcp_config_file,
            session_id=fresh_id,
            resume_session_id=resume_id,
            extra_args=self._extra_args,
        )
        self._proc = self._process_factory(
            command=self._command, args=args, cwd=self._cwd
        )
        if fresh_id:
            self._session_id = fresh_id
        logger.info(
            "claude-cli session %s: sid=%s model=%s cwd=%s",
            "resumed" if resume_id else "started",
            (resume_id or fresh_id or "")[:8],
            self._model or "default",
            self._cwd,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.retire()
        if self._system_prompt_file:
            try:
                os.unlink(self._system_prompt_file)
            except OSError:
                pass
            self._system_prompt_file = None

    def retire(self) -> None:
        """Tear down the subprocess but keep the session id, so the next
        ensure_started() resumes the conversation in a fresh process."""
        if self._proc is not None:
            try:
                self._proc.close()
            except Exception:  # pragma: no cover - best-effort
                pass
            self._proc = None

    def __enter__(self) -> "ClaudeCliSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- interrupt ----------

    def request_interrupt(self) -> None:
        """Idempotent; thread-safe. The active run_turn loop notices, asks
        the CLI to abort, and unwinds."""
        self._interrupt_event.set()

    # ---------- per-turn ----------

    def run_turn(
        self,
        user_input: Any,
        *,
        turn_timeout: float = 600.0,
        event_poll_timeout: float = 0.25,
        no_output_timeout: float = _DEFAULT_NO_OUTPUT_TIMEOUT,
        interrupt_check: Optional[Callable[[], bool]] = None,
    ) -> ClaudeTurnResult:
        """Write one user message and consume events until the terminal
        `result` line. Returns a ClaudeTurnResult; never raises for turn-level
        failures (they land in result.error)."""
        result = ClaudeTurnResult(session_id=self._session_id)
        try:
            self.ensure_started()
        except Exception as exc:
            result.error = f"claude CLI startup failed: {exc}"
            result.should_retire = True
            return result
        assert self._proc is not None

        self._interrupt_event.clear()
        text = _coerce_turn_input_text(user_input)
        try:
            self._proc.send_user_message(text)
        except RuntimeError as exc:
            result.error = self._format_error("failed to write turn input", exc)
            result.should_retire = True
            return result

        deadline = time.monotonic() + turn_timeout
        last_output_at = time.monotonic()
        # tool_use ids seen without a matching tool_result yet. While
        # non-empty, the quiet window stretches (a running Bash command is
        # silent by design).
        outstanding_tools: set[str] = set()
        interrupt_sent_at: Optional[float] = None
        turn_complete = False

        while time.monotonic() < deadline:
            wants_interrupt = self._interrupt_event.is_set() or (
                interrupt_check is not None and interrupt_check()
            )
            if wants_interrupt and interrupt_sent_at is None:
                interrupt_sent_at = time.monotonic()
                result.interrupted = True
                try:
                    self._proc.send_interrupt(f"hermes-int-{uuid.uuid4().hex[:8]}")
                except RuntimeError:
                    break  # stdin gone — process is already dying
            if (
                interrupt_sent_at is not None
                and time.monotonic() - interrupt_sent_at > _INTERRUPT_GRACE_SECONDS
            ):
                # CLI didn't wind down in time — hard-stop it. The session id
                # survives, so the next turn resumes.
                result.should_retire = True
                break

            event = self._proc.take_event(timeout=event_poll_timeout)
            if event is None:
                now = time.monotonic()
                quiet_window = (
                    _TOOL_OUTSTANDING_NO_OUTPUT_TIMEOUT
                    if outstanding_tools
                    else no_output_timeout
                )
                if not self._proc.is_alive():
                    self._fail_on_exit(result)
                    return result
                if now - last_output_at > quiet_window:
                    result.error = self._format_error(
                        f"claude CLI produced no output for {quiet_window:.0f}s"
                    )
                    result.should_retire = True
                    return result
                continue

            last_output_at = time.monotonic()
            etype = event.get("type")

            if etype == "_process_exited":
                self._fail_on_exit(result)
                return result

            sid = event.get("session_id")
            if isinstance(sid, str) and sid.strip():
                self._session_id = sid.strip()
                result.session_id = self._session_id

            if etype == "stream_event":
                self._handle_stream_event(event.get("event") or {})
            elif etype == "assistant":
                self._project_assistant(event, result, outstanding_tools)
            elif etype == "user":
                self._project_tool_results(event, result, outstanding_tools)
            elif etype == "control_request":
                self._handle_control_request(event)
            elif etype == "result":
                self._apply_result(event, result)
                turn_complete = True
                break
            # "system" (init) and control_response acks need no handling.

        if not turn_complete and not result.error:
            if result.interrupted:
                # Interrupt raced turn completion; accept what we have.
                pass
            else:
                result.error = self._format_error(
                    f"turn timed out after {turn_timeout:.0f}s"
                )
                result.interrupted = True
                result.should_retire = True

        if result.should_retire:
            self.retire()
        return result

    # ---------- event handling ----------

    def _handle_stream_event(self, ev: dict) -> None:
        """Partial deltas — display streaming only; projection uses the
        complete assistant/user events."""
        if ev.get("type") != "content_block_delta":
            return
        delta = ev.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            chunk = delta.get("text")
            if chunk and self._on_stream_delta is not None:
                try:
                    self._on_stream_delta(chunk)
                except Exception:
                    logger.debug("stream delta callback raised", exc_info=True)
        elif dtype == "thinking_delta":
            chunk = delta.get("thinking")
            if chunk and self._on_thinking_delta is not None:
                try:
                    self._on_thinking_delta(chunk)
                except Exception:
                    logger.debug("thinking delta callback raised", exc_info=True)

    def _project_assistant(
        self,
        event: dict,
        result: ClaudeTurnResult,
        outstanding_tools: set[str],
    ) -> None:
        """Project a complete assistant message into OpenAI format: text
        content plus tool_calls entries for tool_use blocks."""
        message = event.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                if isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            elif btype in {"tool_use", "server_tool_use", "mcp_tool_use"}:
                tool_id = block.get("id") or f"claude_call_{uuid.uuid4().hex[:8]}"
                name = _hermes_tool_name(str(block.get("name") or "unknown"))
                args = block.get("input")
                if not isinstance(args, (dict, list)):
                    args = {} if args is None else {"input": args}
                tool_calls.append(
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
                outstanding_tools.add(tool_id)
                if self._on_tool_started is not None:
                    try:
                        self._on_tool_started(
                            name, args if isinstance(args, dict) else {}
                        )
                    except Exception:
                        logger.debug("tool-started callback raised", exc_info=True)

        text = "".join(text_parts)
        if not text and not tool_calls:
            return
        projected: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            projected["tool_calls"] = tool_calls
            result.tool_iterations += 1
        result.projected_messages.append(projected)
        if text:
            # Claude may emit several assistant messages in one turn
            # (commentary between tool calls, then the answer). The last
            # text-bearing one is the canonical final response.
            result.final_text = text

    def _project_tool_results(
        self,
        event: dict,
        result: ClaudeTurnResult,
        outstanding_tools: set[str],
    ) -> None:
        """Project echoed tool_result blocks into {role: tool} rows. Plain
        replayed user messages (--replay-user-messages echoes our own input)
        carry no tool_result blocks and are skipped."""
        message = event.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id") or ""
            outstanding_tools.discard(tool_id)
            rendered = _render_result_content(block.get("content"))
            if block.get("is_error"):
                rendered = rendered or "tool reported an error"
            result.projected_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": rendered,
                }
            )

    def _handle_control_request(self, event: dict) -> None:
        """Answer permission asks (`can_use_tool`) via the Hermes policy
        callback; fail closed on anything unrecognized."""
        assert self._proc is not None
        request_id = event.get("request_id")
        request = event.get("request") or {}
        subtype = request.get("subtype")
        if subtype != "can_use_tool":
            logger.warning("claude-cli: unknown control_request %r", subtype)
            try:
                self._proc.send_control_response(
                    request_id,
                    allow=False,
                    deny_message=f"Unsupported control request: {subtype}",
                )
            except RuntimeError:
                pass
            return

        tool_name = str(request.get("tool_name") or "")
        tool_input = request.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        allow = False
        if self._permission_callback is not None:
            try:
                allow = bool(self._permission_callback(tool_name, tool_input))
            except Exception:
                logger.exception("claude-cli permission callback raised")
                allow = False
        try:
            self._proc.send_control_response(
                request_id,
                allow=allow,
                updated_input=tool_input,
                tool_use_id=request.get("tool_use_id"),
                deny_message=(
                    f"Hermes denied {tool_name}: no interactive approval "
                    "available in this context."
                ),
            )
        except RuntimeError:
            pass

    def _apply_result(self, event: dict, result: ClaudeTurnResult) -> None:
        subtype = str(event.get("subtype") or "")
        usage = event.get("usage")
        if isinstance(usage, dict):
            result.usage = usage
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            result.total_cost_usd = float(cost)
        num_turns = event.get("num_turns")
        if isinstance(num_turns, int):
            result.num_turns = num_turns

        text = event.get("result")
        if isinstance(text, str) and text.strip():
            result.final_text = text

        is_error = bool(event.get("is_error")) or (
            subtype != "" and subtype != "success" and not subtype.startswith("success")
        )
        if is_error:
            detail = ""
            for key in ("result", "error", "message"):
                val = event.get(key)
                if isinstance(val, str) and val.strip():
                    detail = val.strip()
                    break
            haystack = f"{subtype} {detail}".lower()
            hint = _classify_auth_failure(subtype, detail)
            if hint is not None:
                result.error = hint
            else:
                result.error = self._format_error(
                    f"claude CLI turn failed ({subtype or 'error'})", detail
                )
            result.should_retire = True
            if any(h in haystack for h in _SESSION_INVALID_HINTS):
                result.session_invalid = True
                self._session_id = None

    # ---------- failure paths ----------

    def _fail_on_exit(self, result: ClaudeTurnResult) -> None:
        assert self._proc is not None
        # Drain anything the reader parsed before exit — a result event may
        # already be queued (e.g. the CLI printed its error and quit).
        while True:
            event = self._proc.take_event(timeout=0)
            if event is None:
                break
            if event.get("type") == "result":
                self._apply_result(event, result)
                if result.error is None and not result.final_text:
                    result.error = self._format_error(
                        "claude CLI exited after an empty result"
                    )
                result.should_retire = True
                self.retire()
                return
        if result.interrupted:
            result.should_retire = True
            self.retire()
            return
        stderr_blob = "\n".join(self._proc.stderr_tail(60))
        hint = _classify_auth_failure(stderr_blob)
        rc = self._proc.returncode()
        result.error = hint or self._format_error(
            f"claude CLI subprocess exited unexpectedly (code {rc})"
        )
        result.should_retire = True
        self.retire()

    def _format_error(self, prefix: str, exc: Any = "") -> str:
        exc_str = str(exc) if exc not in ("", None) else ""
        base = f"{prefix}: {exc_str}" if exc_str else prefix
        if self._proc is None:
            return base
        try:
            tail = self._proc.stderr_tail(_STDERR_TAIL_LINES)
        except Exception:  # pragma: no cover
            return base
        joined = "\n".join(line.rstrip() for line in tail if line).strip()
        if not joined:
            return base
        redacted = redact_sensitive_text(joined, force=True)
        return f"{base}\nclaude stderr (last {len(tail)} lines):\n{redacted}"

    # ---------- internals ----------

    def _ensure_system_prompt_file(self) -> Optional[str]:
        """Write the Hermes system prompt to a file for
        --append-system-prompt-file (argv has ARG_MAX limits; the prompt can
        be tens of KB). Reused across respawns within this session."""
        if not self._system_prompt:
            return None
        if self._system_prompt_file and os.path.exists(self._system_prompt_file):
            return self._system_prompt_file
        import tempfile

        fd, path = tempfile.mkstemp(
            prefix="hermes-claude-sysprompt-",
            suffix=".md",
            dir=self._scratch_dir or None,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self._system_prompt)
        self._system_prompt_file = path
        return path
