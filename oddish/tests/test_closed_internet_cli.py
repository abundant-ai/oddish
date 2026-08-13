from oddish.cli.api import build_sweep_payload
from oddish.cli.closed_internet import (
    apply_allow_agent_hosts,
    apply_disable_web_tools,
)


def test_apply_allow_agent_hosts_merges_unique():
    cfg = {"extra_allowed_hosts": ["api.openai.com"]}
    apply_allow_agent_hosts(cfg, ["api.anthropic.com", "api.openai.com"])
    assert cfg["extra_allowed_hosts"] == ["api.openai.com", "api.anthropic.com"]


def test_apply_disable_web_tools_claude_and_codex():
    claude = apply_disable_web_tools(agent_name="claude-code", agent_config={})
    assert claude["kwargs"]["disallowed_tools"] == "WebSearch WebFetch"

    codex = apply_disable_web_tools(agent_name="codex", agent_config={})
    assert codex["kwargs"]["web_search"] == "disabled"

    grok = apply_disable_web_tools(agent_name="grok-build", agent_config={})
    assert grok["kwargs"]["disable_web_search"] is True

    # cursor-cli and gemini-cli run their web tools provider-side, so the
    # container network can't reach them -- the disable_web_tools switch is the
    # only lever. Both must get it, or closed-book tasks leak via web fetch.
    cursor = apply_disable_web_tools(agent_name="cursor-cli", agent_config={})
    assert cursor["kwargs"]["disable_web_tools"] is True

    gemini = apply_disable_web_tools(agent_name="gemini-cli", agent_config={})
    assert gemini["kwargs"]["disable_web_tools"] is True

    # Explicit user kwargs win.
    custom = apply_disable_web_tools(
        agent_name="claude-code",
        agent_config={"kwargs": {"disallowed_tools": "WebSearch"}},
    )
    assert custom["kwargs"]["disallowed_tools"] == "WebSearch"

    cursor_override = apply_disable_web_tools(
        agent_name="cursor-cli",
        agent_config={"kwargs": {"disable_web_tools": False}},
    )
    assert cursor_override["kwargs"]["disable_web_tools"] is False


def test_build_sweep_payload_closed_internet_flags():
    payload = build_sweep_payload(
        task_id="task",
        configs=[{"agent": "claude-code", "model": "anthropic/claude-opus-4-8", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        allow_agent_hosts=["api.anthropic.com"],
        disable_web_tools=True,
    )
    agent_config = payload["configs"][0]["agent_config"]
    assert agent_config["extra_allowed_hosts"] == ["api.anthropic.com"]
    assert agent_config["kwargs"]["disallowed_tools"] == "WebSearch WebFetch"
