"""Claude CLI provider profile.

Routes turns through the local `claude` CLI instead of the Anthropic REST
API — no API key required; uses Claude Code's native auth (OAuth / keychain /
CLAUDE_CODE_OAUTH_TOKEN).

Default runtime (api_mode "claude_cli"): a persistent `claude` subprocess
speaking stream-json owns the agentic loop — native Bash/Read/Write/Edit
tools, internal context and compaction, session resume across respawns —
while Hermes' extra tools (web search, browser, skills, kanban, …) reach it
via the hermes-tools MCP server. See agent/claude_cli_runtime.py.

Environment variables:
  HERMES_CLAUDE_CLI_COMMAND   Path to the claude binary (default: "claude")
  HERMES_CLAUDE_CLI_EFFORT    Fixed effort level: low/medium/high/xhigh/max
  HERMES_CLAUDE_CLI_ARGS      Extra CLI args (space-separated)
  HERMES_CLAUDE_CLI_RUNTIME   "shim" reverts to the legacy one-shot
                              OpenAI-compat shim (agent/claude_cli_client.py)
"""

from providers import register_provider
from providers.base import ProviderProfile

CLAUDE_CLI_MARKER_BASE_URL = "cli://claude"


class ClaudeCliProfile(ProviderProfile):
    """Claude via local CLI subprocess — no REST API key required."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        # Keep in sync with _PROVIDER_MODELS["claude-cli"] in
        # hermes_cli/models.py — that static catalog is what /model and the
        # picker read; this hook serves the providers-registry consumers.
        return [
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]


claude_cli = ClaudeCliProfile(
    name="claude-cli",
    aliases=("claude-code-cli", "claude-local"),
    api_mode="chat_completions",
    env_vars=(),
    base_url=CLAUDE_CLI_MARKER_BASE_URL,
    auth_type="external_process",
    fallback_models=("claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6"),
    default_aux_model="claude-haiku-4-5-20251001",
)

register_provider(claude_cli)
