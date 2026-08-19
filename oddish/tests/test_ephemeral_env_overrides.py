"""Ephemeral (override-Harbor) child env: direct-Anthropic routing overrides.

The child builds a plain AgentConfig and never runs _build_agent_config's
injectors, so anthropic/ and anthropic-hdo/ routing must arrive as ambient
env from _runtime_env_overrides.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.harbor.ephemeral import _runtime_env_overrides  # noqa: E402


def _overrides(model: str, agent: str = "claude-code") -> dict[str, str]:
    return _runtime_env_overrides(
        agent=agent, model=model, raw_harbor_config={}, is_probe=False
    )


def test_anthropic_platform_model_pins_direct_api_env(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-platform")

    env = _overrides("anthropic/claude-haiku-4-5")

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-platform"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    assert env["ANTHROPIC_MODEL"] == "claude-haiku-4-5"


def test_anthropic_hdo_model_pins_hdo_key(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "anthropic_hdo_api_key", "sk-ant-hdo")

    env = _overrides("anthropic-hdo/claude-haiku-4-5")

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-hdo"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["ANTHROPIC_MODEL"] == "claude-haiku-4-5"


def test_bare_claude_model_keeps_bedrock_default(monkeypatch):
    env = _overrides("global.anthropic.claude-haiku-4-5-20251001-v1:0")

    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env


def test_non_claude_code_agent_gets_key_but_no_model_pin(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-platform")

    env = _overrides("anthropic/claude-haiku-4-5", agent="mini-swe-agent")

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-platform"
    assert "ANTHROPIC_MODEL" not in env
