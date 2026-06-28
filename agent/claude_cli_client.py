"""OpenAI-compatible shim that forwards Hermes requests to the `claude` CLI.

Each request formats the full conversation history as a structured prompt,
runs `claude --print --output-format stream-json`, and converts the JSONL
output back into the minimal shape Hermes expects from an OpenAI client.

This is designed as a fallback/alternative path — no API key required, uses
whatever auth the `claude` CLI already has (OAuth, keychain, ANTHROPIC_API_KEY).

Environment variables:
  HERMES_CLAUDE_CLI_COMMAND   Path to the claude binary (default: "claude")
  HERMES_CLAUDE_CLI_EFFORT    Fixed effort level: low/medium/high/xhigh/max
  HERMES_CLAUDE_CLI_ARGS      Extra CLI args (space-separated)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CLAUDE_CLI_MARKER_BASE_URL = "cli://claude"

_DEFAULT_TIMEOUT_SECONDS = 600.0

_TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)
_TOOL_CALL_JSON_RE = re.compile(
    r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,"
    r"\s*\"function\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)

_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "adaptive": "medium",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Models whose short aliases the CLI accepts (saves token count in --model arg)
_MODEL_ALIASES = {
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_CLAUDE_CLI_COMMAND", "").strip()
        or "claude"
    )


def _resolve_extra_args() -> list[str]:
    raw = os.getenv("HERMES_CLAUDE_CLI_ARGS", "").strip()
    return shlex.split(raw) if raw else []


def _resolve_effort(model: str | None = None) -> str | None:
    env_effort = os.getenv("HERMES_CLAUDE_CLI_EFFORT", "").strip().lower()
    if env_effort:
        return _EFFORT_MAP.get(env_effort, env_effort)
    return None


def _normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    # Strip provider prefix (e.g. "claude-cli/claude-sonnet-4-6")
    if "/" in model:
        model = model.split("/", 1)[1]
    return _MODEL_ALIASES.get(model, model)


def _build_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    # Ensure HOME is set — some claude CLI versions need it for config/auth
    if not env.get("HOME"):
        env["HOME"] = str(Path.home())
    return env


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def _render_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
        inner = content.get("content")
        if isinstance(inner, str):
            return inner.strip()
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    """Format the full conversation as a single prompt string for stdin.

    Everything — system message, tool definitions, transcript — is folded into
    one block sent via stdin.  This avoids ARG_MAX issues with --system-prompt.
    """
    sections: list[str] = []

    tool_specs: list[dict[str, Any]] = []
    if isinstance(tools, list) and tools:
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            tool_specs.append({
                "name": name.strip(),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
    if tool_specs:
        sections.append(
            "Available tools (OpenAI function schema). "
            "When you need to call a tool, emit ONLY a "
            "<tool_call>{...}</tool_call> block containing one JSON object "
            "with id/type/function{name,arguments} — arguments must be a "
            "JSON string.\n"
            + json.dumps(tool_specs, ensure_ascii=False)
        )
    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")

    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        rendered = _render_content(content)
        if not rendered:
            continue
        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")

    if transcript:
        sections.append("Conversation:\n\n" + "\n\n".join(transcript))

    sections.append("Continue the conversation — respond to the latest User message.")
    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Tool call extraction
# ---------------------------------------------------------------------------

def _extract_tool_calls(text: str) -> tuple[list[SimpleNamespace], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[SimpleNamespace] = []
    consumed: list[tuple[int, int]] = []

    def _try_add(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id") or f"ccli_call_{len(extracted) + 1}"
        extracted.append(SimpleNamespace(
            id=call_id,
            call_id=call_id,
            response_item_id=None,
            type="function",
            function=SimpleNamespace(name=fn_name.strip(), arguments=fn_args),
        ))

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        _try_add(m.group(1))
        consumed.append((m.start(), m.end()))

    # Bare-JSON fallback only when no XML blocks found
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            _try_add(m.group(0))
            consumed.append((m.start(), m.end()))

    if not consumed:
        return extracted, text.strip()

    consumed.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p.strip()).strip()
    return extracted, cleaned


# ---------------------------------------------------------------------------
# JSONL output parsing
# ---------------------------------------------------------------------------

def _parse_json_output(lines: list[str]) -> tuple[str, str | None, dict[str, Any]]:
    """Parse claude --output-format json output (single JSON object on stdout)."""
    raw = "".join(lines).strip()
    if not raw:
        return "", None, {}

    try:
        event = json.loads(raw)
    except Exception:
        return raw, None, {}

    if not isinstance(event, dict):
        return str(event), None, {}

    subtype = str(event.get("subtype") or "").strip()
    if subtype and subtype != "success":
        error_msg = str(event.get("error") or subtype or "Unknown CLI error")
        raise RuntimeError(f"claude CLI error: {error_msg}")

    session_id: str | None = None
    sid = event.get("session_id")
    if isinstance(sid, str) and sid.strip():
        session_id = sid.strip()

    usage: dict[str, Any] = {}
    raw_usage = event.get("usage") or {}
    if isinstance(raw_usage, dict):
        usage = raw_usage

    result = event.get("result")
    if isinstance(result, str) and result.strip():
        return result, session_id, usage

    text_parts: list[str] = []
    msg = event.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t:
                        text_parts.append(t)
        elif isinstance(content, str):
            text_parts.append(content)

    return "".join(text_parts), session_id, usage


# ---------------------------------------------------------------------------
# Client shim
# ---------------------------------------------------------------------------

class _ClaudeCLIChatCompletions:
    def __init__(self, client: "ClaudeCliClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ClaudeCLIChatNamespace:
    def __init__(self, client: "ClaudeCliClient"):
        self.completions = _ClaudeCLIChatCompletions(client)


class ClaudeCliClient:
    """Minimal OpenAI-client-compatible facade wrapping `claude --print`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        command: str | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "claude-cli"
        self.base_url = base_url or CLAUDE_CLI_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._command = command or _resolve_command()
        self._extra_args = _resolve_extra_args()
        self.chat = _ClaudeCLIChatNamespace(self)
        self.is_closed = False

    def close(self) -> None:
        self.is_closed = True

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> Any:
        prompt = _format_messages_as_prompt(
            messages or [],
            model=model,
            tools=tools,
            tool_choice=tool_choice,
        )

        if timeout is None:
            effective_timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, (int, float)):
            effective_timeout = float(timeout)
        else:
            candidates = [
                getattr(timeout, attr, None)
                for attr in ("read", "write", "connect", "pool", "timeout")
            ]
            numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
            effective_timeout = max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS

        response_text, session_id, raw_usage = self._run_prompt(
            prompt=prompt,
            model=model,
            timeout_seconds=effective_timeout,
        )

        tool_calls, cleaned_text = _extract_tool_calls(response_text)

        usage = SimpleNamespace(
            prompt_tokens=raw_usage.get("input_tokens", 0),
            completion_tokens=raw_usage.get("output_tokens", 0),
            total_tokens=raw_usage.get("input_tokens", 0) + raw_usage.get("output_tokens", 0),
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=raw_usage.get("cache_read_input_tokens", 0)
            ),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls or None,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or "claude-cli",
        )

    def _run_prompt(
        self,
        *,
        prompt: str,
        model: str | None,
        timeout_seconds: float,
    ) -> tuple[str, str | None, dict[str, Any]]:
        normalized_model = _normalize_model(model)
        effort = _resolve_effort(model)

        cmd: list[str] = [self._command, "--print", "--output-format", "json"]
        if normalized_model:
            cmd += ["--model", normalized_model]
        if effort:
            cmd += ["--effort", effort]
        cmd += self._extra_args

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=_build_subprocess_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start claude CLI at '{self._command}'. "
                "Install Claude Code (https://claude.ai/code) or set "
                "HERMES_CLAUDE_CLI_COMMAND to the correct path."
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("claude CLI process did not expose stdin/stdout pipes.")

        lines: list[str] = []
        stderr_lines: list[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_lines.append(line.rstrip("\n"))

        out_thread = threading.Thread(target=_read_stdout, daemon=True)
        err_thread = threading.Thread(target=_read_stderr, daemon=True)
        out_thread.start()
        err_thread.start()

        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        out_thread.join(timeout=timeout_seconds)
        err_thread.join(timeout=2.0)
        proc.wait(timeout=5.0)

        if proc.returncode not in (0, None) and not lines:
            stderr_text = "\n".join(stderr_lines).strip()
            raise RuntimeError(
                f"claude CLI exited with code {proc.returncode}"
                + (f":\n{stderr_text}" if stderr_text else "")
            )

        return _parse_json_output(lines)
