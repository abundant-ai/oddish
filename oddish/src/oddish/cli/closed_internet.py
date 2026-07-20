"""Helpers for closed-internet Oddish runs on upstream Harbor network policy.

Matches swe-marathon ``scripts/run-benchmark.sh``:

- ``--allow-agent-host`` → Harbor ``AgentConfig.extra_allowed_hosts``
- ``--disable-web-tools`` → agent kwargs that turn off server-side web tools
"""

from __future__ import annotations

from typing import Any


_CLAUDE_WEB_TOOLS = "WebSearch WebFetch"


def apply_allow_agent_hosts(
    agent_config: dict[str, Any],
    hosts: list[str] | None,
) -> dict[str, Any]:
    """Merge run-specific model hosts into ``extra_allowed_hosts``."""
    if not hosts:
        return agent_config
    cleaned = [h.strip() for h in hosts if h and h.strip()]
    if not cleaned:
        return agent_config
    existing = list(agent_config.get("extra_allowed_hosts") or [])
    agent_config["extra_allowed_hosts"] = list(dict.fromkeys([*existing, *cleaned]))
    return agent_config


def apply_disable_web_tools(
    *,
    agent_name: str | None,
    agent_config: dict[str, Any],
) -> dict[str, Any]:
    """Inject agent kwargs that disable web search/fetch when unset.

    Explicit user kwargs always win. Agents without a known web-tool toggle are
    left unchanged (callers can still pass ``--agent-kwarg`` directly).
    """
    name = (agent_name or "").strip().lower()
    kwargs = dict(agent_config.get("kwargs") or {})

    if "claude-code" in name:
        kwargs.setdefault("disallowed_tools", _CLAUDE_WEB_TOOLS)
    elif name == "codex" or name.startswith("codex-"):
        kwargs.setdefault("web_search", "disabled")
    elif name == "grok-build" or name.startswith("grok-build"):
        # Upstream/Oddish Grok Build already defaults web search off; make the
        # closed-internet intent explicit when the flag is set.
        kwargs.setdefault("disable_web_search", True)

    if kwargs:
        agent_config["kwargs"] = kwargs
    return agent_config


def apply_closed_internet_overrides(
    configs: list[dict[str, Any]],
    *,
    allow_agent_hosts: list[str] | None = None,
    disable_web_tools: bool = False,
) -> None:
    """Mutate sweep trial configs with closed-internet CLI overrides."""
    if not allow_agent_hosts and not disable_web_tools:
        return
    for config in configs:
        existing = dict(config.get("agent_config") or {})
        apply_allow_agent_hosts(existing, allow_agent_hosts)
        if disable_web_tools:
            apply_disable_web_tools(
                agent_name=config.get("agent"),
                agent_config=existing,
            )
        config["agent_config"] = existing
