"""Claude Code CLI stream-json wire client.

Spawns a persistent `claude` subprocess in stream-json live mode and speaks
newline-delimited JSON over stdio:

    claude -p --input-format stream-json --output-format stream-json ...

Each user turn is one NDJSON line written to stdin; the CLI streams events
back on stdout (`system`, `stream_event`, `assistant`, `user`, `result`,
`control_request`) until the turn's terminal `result` line. The process
persists across turns — Claude Code owns the conversation context, its own
compaction, and the agentic tool loop.

This module is the wire-level speaker only. Turn driving, event projection
into Hermes' messages shape, permission bridging, and watchdogs live in
`claude_cli_session.py`. The design mirrors `codex_app_server.py` (Hermes'
other persistent-CLI runtime) and OpenClaw's claude-cli live-session backend.

Threading model (same as CodexAppServerClient):
  - Caller thread writes turns and drains the event queue synchronously.
  - One reader thread parses stdout NDJSON into a queue.
  - One reader thread captures stderr for diagnostics.
Intentionally NOT async — AIAgent.run_conversation() is synchronous.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any, Optional

from tools.environments.local import hermes_subprocess_env

# Environment variables that must not leak into the spawned `claude` process.
# These reroute or re-bill the CLI's own Anthropic session: a stale
# ANTHROPIC_BASE_URL would point the CLI at a proxy meant for Hermes' REST
# path, ANTHROPIC_AUTH_TOKEN overrides the CLI's login, and the Bedrock/
# Vertex/Foundry switches flip it off the subscription entirely. OTEL_* is
# stripped so Hermes' telemetry endpoints don't receive Claude Code spans.
#
# Deviation from OpenClaw (which also strips CLAUDE_CODE_OAUTH_TOKEN and
# ANTHROPIC_API_KEY): Hermes gateways commonly run headless under launchd/
# systemd where the macOS Keychain is unreachable, and CLAUDE_CODE_OAUTH_TOKEN
# in ~/.hermes/.env is the only working auth. Those two stay.
_CLAUDE_CLI_ENV_STRIP = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
)
_CLAUDE_CLI_ENV_STRIP_PREFIXES = ("OTEL_",)

# Claude Code native tools we never allow the subprocess to use: they collide
# with Hermes' own scheduling/automation surface (the gateway owns cron and
# background work, not the model's inner CLI).
DISALLOWED_NATIVE_TOOLS = (
    "ScheduleWakeup",
    "CronCreate",
    "Monitor",
)

# The MCP server name Hermes registers its tools under. Tool names arrive as
# mcp__<server>__<tool>; the session strips this prefix when projecting back
# into Hermes' transcript.
HERMES_MCP_SERVER_NAME = "hermes-tools"
HERMES_MCP_TOOL_PREFIX = f"mcp__{HERMES_MCP_SERVER_NAME}__"


def build_claude_live_args(
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    permission_mode: str = "default",
    system_prompt_file: Optional[str] = None,
    mcp_config_file: Optional[str] = None,
    session_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """Build the argv tail for a persistent stream-json `claude` process.

    Exactly one of session_id (fresh) / resume_session_id (respawn after a
    retire) should be set; passing both prefers resume.
    """
    args = [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        # stream-json omits event types without --verbose.
        "--verbose",
        # Never let project/local .claude settings of the cwd leak into a
        # gateway-driven session; user-level settings still apply.
        "--setting-sources", "user",
        # Note: no --permission-prompt-tool — Claude Code 2.x dropped it; in
        # stream-json mode permission asks arrive as control_request events
        # natively (handled in claude_cli_session._handle_control_request).
        "--replay-user-messages",
        "--permission-mode", permission_mode,
        "--allowedTools", f"{HERMES_MCP_TOOL_PREFIX}*",
        "--disallowedTools", ",".join(DISALLOWED_NATIVE_TOOLS),
    ]
    if mcp_config_file:
        args += ["--strict-mcp-config", "--mcp-config", mcp_config_file]
    if system_prompt_file:
        args += ["--append-system-prompt-file", system_prompt_file]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    elif session_id:
        args += ["--session-id", session_id]
    if extra_args:
        args += list(extra_args)
    return args


class ClaudeCliProcess:
    """One persistent `claude` stream-json subprocess.

    Lifecycle: spawn on construction, one NDJSON line per user turn via
    send_user_message()/send_raw(), events drained via take_event(), torn
    down with close(). The caller decides when to retire and respawn.
    """

    def __init__(
        self,
        *,
        command: str = "claude",
        args: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._command = command
        # The claude CLI legitimately needs provider credentials (it makes the
        # model calls), so inherit them — but Tier-1 Hermes secrets (gateway
        # bot tokens, GitHub auth, infra tokens) are always stripped, same as
        # the codex app-server spawn site.
        spawn_env = hermes_subprocess_env(inherit_credentials=True)
        for key in _CLAUDE_CLI_ENV_STRIP:
            spawn_env.pop(key, None)
        for key in list(spawn_env):
            if key.startswith(_CLAUDE_CLI_ENV_STRIP_PREFIXES):
                spawn_env.pop(key, None)
        if env:
            spawn_env.update(env)
        if not spawn_env.get("HOME"):
            spawn_env["HOME"] = os.path.expanduser("~")

        try:
            self._proc = subprocess.Popen(
                [command] + list(args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=cwd,
                env=spawn_env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start claude CLI at {command!r}. Install Claude "
                "Code (https://claude.ai/code) or set HERMES_CLAUDE_CLI_COMMAND."
            ) from exc

        self._events: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._closed = False

        self._reader = threading.Thread(
            target=self._read_stdout, daemon=True, name="claude-cli-stdout"
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, daemon=True, name="claude-cli-stderr"
        )
        self._stderr_reader.start()

    # ---------- send ----------

    def send_user_message(self, text: str) -> None:
        """Write one user turn. Shape matches Claude Code's stream-json input
        protocol (and OpenClaw's createClaudeUserInputMessage)."""
        self.send_raw(
            {
                "type": "user",
                "session_id": "",
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": text},
            }
        )

    def send_control_response(
        self,
        request_id: Any,
        *,
        allow: bool,
        updated_input: Any = None,
        tool_use_id: Optional[str] = None,
        deny_message: str = "",
    ) -> None:
        """Answer a `control_request` (subtype can_use_tool) from the CLI."""
        if allow:
            inner: dict[str, Any] = {"behavior": "allow", "updatedInput": updated_input}
            if tool_use_id:
                inner["toolUseID"] = tool_use_id
        else:
            inner = {
                "behavior": "deny",
                "decisionClassification": "user_reject",
                "message": deny_message or "Denied by Hermes permission policy.",
            }
        self.send_raw(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": inner,
                },
            }
        )

    def send_interrupt(self, request_id: str) -> None:
        """Ask the CLI to abort the in-flight turn. The CLI answers with a
        control_response and a terminal result; the session escalates to
        kill if neither arrives."""
        self.send_raw(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "interrupt"},
            }
        )

    def send_raw(self, obj: dict) -> None:
        if self._closed:
            raise RuntimeError("claude CLI process is closed")
        stdin = self._proc.stdin
        if stdin is None:
            raise RuntimeError("claude CLI stdin not available")
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._stdin_lock:
                stdin.write(payload)
                stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise RuntimeError(
                f"claude CLI stdin closed unexpectedly: {exc}"
            ) from exc

    # ---------- receive ----------

    def take_event(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next parsed stdout event, or None on timeout. Use small
        positive timeouts in the turn loop to interleave with interrupt and
        watchdog checks."""
        try:
            if timeout <= 0:
                return self._events.get_nowait()
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------- diagnostics / lifecycle ----------

    def stderr_tail(self, n: int = 20) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr_lines[-n:])

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.poll()

    def close(self, timeout: float = 3.0) -> None:
        """Close stdin (the CLI exits on EOF) and escalate to kill."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception:
                pass

    def __enter__(self) -> "ClaudeCliProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- internals ----------

    def _read_stdout(self) -> None:
        if self._proc.stdout is None:
            return
        try:
            for line in iter(self._proc.stdout.readline, b""):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    with self._stderr_lock:
                        self._stderr_lines.append(
                            f"<non-json on stdout> {line[:200]!r}"
                        )
                    continue
                if isinstance(msg, dict):
                    self._events.put(msg)
        except Exception as exc:  # pragma: no cover - reader crash
            with self._stderr_lock:
                self._stderr_lines.append(f"<stdout reader error> {exc}")
        finally:
            # Wake any blocked take_event() so the session notices the exit
            # promptly instead of waiting out its poll timeout.
            self._events.put({"type": "_process_exited"})

    def _read_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for line in iter(self._proc.stderr.readline, b""):
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(
                        line.decode("utf-8", "replace").rstrip()
                    )
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines = self._stderr_lines[-500:]
        except Exception:  # pragma: no cover
            pass


def check_claude_binary(command: str = "claude") -> tuple[bool, str]:
    """Verify the claude CLI is installed. Returns (ok, version-or-message)."""
    try:
        proc = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, (
            f"claude CLI not found at {command!r}. Install Claude Code: "
            "npm i -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        return False, "claude --version timed out"
    if proc.returncode != 0:
        return False, f"claude --version exited {proc.returncode}: {proc.stderr.strip()}"
    return True, proc.stdout.strip()
