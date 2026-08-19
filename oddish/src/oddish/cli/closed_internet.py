"""Helpers for closed-internet Oddish runs on upstream Harbor network policy.

Workers apply these automatically when a task's agent phase is restricted.
CLI flags remain available as explicit overrides:

- ``--allow-agent-host`` → Harbor ``AgentConfig.extra_allowed_hosts``
- ``--disable-web-tools`` → agent kwargs that turn off server-side web tools
"""

from __future__ import annotations

from typing import Any


_CLAUDE_WEB_TOOLS = "WebSearch WebFetch"


def web_tool_kwargs_for_agent(
    *,
    agent_name: str | None,
    import_path: str | None = None,
) -> dict[str, Any]:
    """Return default kwargs that disable web tools for a known agent family.

    Empty when the agent has no known toggle. Callers should ``setdefault`` these
    so explicit user kwargs always win.
    """
    name = (agent_name or "").strip().lower()
    path = (import_path or "").strip().lower()

    if "claude-code" in name or "claude_code" in path:
        return {"disallowed_tools": _CLAUDE_WEB_TOOLS}
    if (
        name == "codex"
        or name.startswith("codex-")
        or path.endswith(":oddishcodex")
        or path.endswith(":azurecompatiblecodex")
    ):
        return {"web_search": "disabled"}
    if "grok-build" in name or "grok_build" in path:
        return {"disable_web_search": True}
    # cursor-cli and gemini-cli both accept a ``disable_web_tools`` bool that
    # their wrappers translate into the vendor-specific exclusion (cursor:
    # ``--exclude-tools web_search_tool_call/web_fetch_tool_call``; gemini:
    # ``tools.exclude = [google_web_search, web_fetch]``). Their web tools run
    # provider-side, outside the container network namespace, so this switch --
    # not any container network policy -- is the only thing that removes them.
    if name == "cursor-cli" or name.startswith("cursor") or "cursor_cli" in path:
        return {"disable_web_tools": True}
    if name == "gemini-cli" or name.startswith("gemini") or "gemini_cli" in path:
        return {"disable_web_tools": True}
    return {}


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
    import_path: str | None = None,
) -> dict[str, Any]:
    """Inject agent kwargs that disable web search/fetch when unset.

    Explicit user kwargs always win. Agents without a known web-tool toggle are
    left unchanged (callers can still pass ``--agent-kwarg`` directly).
    """
    kwargs = dict(agent_config.get("kwargs") or {})
    for key, value in web_tool_kwargs_for_agent(
        agent_name=agent_name,
        import_path=import_path or agent_config.get("import_path"),
    ).items():
        kwargs.setdefault(key, value)
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
