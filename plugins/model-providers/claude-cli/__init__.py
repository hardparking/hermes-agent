"""Claude CLI provider profile.

Routes requests through the local `claude` CLI subprocess instead of the
Anthropic REST API.  Useful as a fallback when API keys are unavailable or
when you want to leverage Claude Code's native auth (OAuth / keychain).

Set HERMES_CLAUDE_CLI_COMMAND to override the `claude` binary path.
Set HERMES_CLAUDE_CLI_EFFORT to a fixed effort level (low/medium/high/xhigh/max).
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
        return [
            "claude-opus-4-8",
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
    fallback_models=("claude-opus-4-8", "claude-sonnet-4-6"),
    default_aux_model="claude-haiku-4-5-20251001",
)

register_provider(claude_cli)
