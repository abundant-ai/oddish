"""Tests for the personal-subscription auth route (Claude Code OAuth / Codex
auth.json), used to run trials on a Claude Pro/Max or ChatGPT plan instead of
Bedrock / Azure / an API key.

There are two ways to opt a trial into the route:

- a ``sub/<bare-id>`` model id (per-trial, model-keyed), or
- listing the agent in ``ODDISH_SUBSCRIPTION_AGENTS`` (deployment flag), which
  keeps the STANDARD model id (e.g. ``claude-opus-4-8``) for storage/display.

Either way the trial must stay off the Bedrock chokepoint, get a dedicated
``subscription`` provider and ``sub/<id>`` queue bucket, and have the right
credentials injected into the agent env.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from harbor.models.trial.config import AgentConfig

from oddish.config import (
    is_subscription_model,
    settings,
    subscription_bare_model_id,
    to_subscription_model_id,
)
from oddish.workers import harbor_runner as hr


def test_is_subscription_model() -> None:
    assert is_subscription_model("sub/claude-opus-4-5")
    assert is_subscription_model("subscription/gpt-5-codex")
    assert is_subscription_model("SUB/Claude-Opus-4-5")
    assert not is_subscription_model("claude-opus-4-5")
    assert not is_subscription_model("zai/glm-4.6")
    assert not is_subscription_model(None)
    assert not is_subscription_model("")


def test_bare_and_canonical_ids() -> None:
    assert subscription_bare_model_id("sub/claude-opus-4-5") == "claude-opus-4-5"
    assert subscription_bare_model_id("subscription/gpt-5-codex") == "gpt-5-codex"
    assert subscription_bare_model_id("claude-opus-4-5") == "claude-opus-4-5"
    assert to_subscription_model_id("subscription/gpt-5-codex") == "sub/gpt-5-codex"
    assert to_subscription_model_id("gpt-5-codex") == "gpt-5-codex"


def test_normalize_trial_model_stays_off_bedrock() -> None:
    # A "claude" id would normally hit the Bedrock chokepoint (and raise if
    # unmapped); the sub/ prefix must bypass it and round-trip unchanged.
    assert (
        settings.normalize_trial_model("claude-code", "sub/claude-opus-4-5")
        == "sub/claude-opus-4-5"
    )
    assert (
        settings.normalize_trial_model("codex", "sub/gpt-5-codex")
        == "sub/gpt-5-codex"
    )


def test_provider_and_queue_bucket_are_dedicated() -> None:
    # Codex is normally pinned to the "openai" provider; the sub route must
    # override that so it never drags onto the Azure/OpenAI credential path.
    assert settings.get_provider_for_trial("codex", "sub/gpt-5-codex") == "subscription"
    assert (
        settings.get_provider_for_trial("claude-code", "sub/claude-opus-4-5")
        == "subscription"
    )
    assert settings.get_queue_key_for_trial("codex", "sub/gpt-5-codex") == "sub/gpt-5-codex"
    # Non-subscription routing is unchanged.
    assert settings.get_provider_for_trial("codex", "gpt-5.2") == "openai"


def test_claude_code_subscription_agent_config() -> None:
    ac = hr._build_agent_config(
        agent="claude-code", model="sub/claude-opus-4-5", raw_harbor_config={}
    )
    # Bare model id reaches the agent; the Bedrock chokepoint never ran.
    assert ac.model_name == "claude-opus-4-5"
    assert ac.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "${CS_CLAUDE_CODE_OAUTH_TOKEN}"
    # ANTHROPIC_API_KEY overrides the subscription in -p mode -> must be blank.
    assert ac.env.get("ANTHROPIC_API_KEY") == ""
    assert ac.env.get("CLAUDE_CODE_USE_BEDROCK") == ""
    assert ac.env.get("AWS_BEARER_TOKEN_BEDROCK") == ""


def test_codex_subscription_agent_config_skips_azure() -> None:
    ac = hr._build_agent_config(
        agent="codex", model="sub/gpt-5-codex", raw_harbor_config={}
    )
    assert ac.model_name == "gpt-5-codex"
    # Routed through the plain Oddish wrapper, NOT the Azure-compat one.
    assert (ac.import_path or "").endswith("OddishCodex")


def test_materialize_codex_auth_json(monkeypatch) -> None:
    payload = {"auth_mode": "chatgpt", "tokens": {"access_token": "x"}}
    monkeypatch.setenv(
        "CS_CODEX_AUTH_JSON_B64",
        base64.b64encode(json.dumps(payload).encode()).decode(),
    )
    ac = AgentConfig(name="codex", model_name="gpt-5-codex")
    tmp = hr._materialize_codex_auth_json(ac)
    try:
        path = Path(ac.env["CODEX_AUTH_JSON_PATH"])
        assert path.is_file()
        assert json.loads(path.read_text())["auth_mode"] == "chatgpt"
    finally:
        if tmp is not None:
            tmp.cleanup()


def test_materialize_codex_auth_json_respects_explicit_path(monkeypatch) -> None:
    # If the operator already pointed at a local file, don't override it.
    monkeypatch.setenv("CS_CODEX_AUTH_JSON_B64", base64.b64encode(b"{}").decode())
    ac = AgentConfig(name="codex", model_name="gpt-5-codex", env={"CODEX_FORCE_AUTH_JSON": "1"})
    assert hr._materialize_codex_auth_json(ac) is None
    assert "CODEX_AUTH_JSON_PATH" not in (ac.env or {})


def test_materialize_codex_auth_json_no_secret(monkeypatch) -> None:
    monkeypatch.delenv("CS_CODEX_AUTH_JSON_B64", raising=False)
    ac = AgentConfig(name="codex", model_name="gpt-5-codex")
    assert hr._materialize_codex_auth_json(ac) is None


# --- Agent-flag route (ODDISH_SUBSCRIPTION_AGENTS) ---------------------------
#
# Instead of tagging each model "sub/<id>", an operator can list the agents
# whose trials use the subscription route. The trial then keeps its STANDARD
# model id (e.g. "claude-opus-4-8") for storage/display while still getting the
# dedicated provider, the "sub/<id>" queue bucket, and the subscription creds.


def test_is_subscription_agent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "subscription_agents", "claude-code, Codex")
    assert settings._is_subscription_agent("claude-code")
    assert settings._is_subscription_agent("CODEX")
    assert not settings._is_subscription_agent("terminus")
    assert not settings._is_subscription_agent(None)
    # Empty config -> nothing routed by agent.
    monkeypatch.setattr(settings, "subscription_agents", "")
    assert not settings._is_subscription_agent("claude-code")


def test_agent_flag_keeps_standard_model_off_bedrock(monkeypatch) -> None:
    # A "claude" id would normally hit the Bedrock chokepoint; the agent flag
    # must bypass it and keep the STANDARD id (no Bedrock map, no "sub/").
    monkeypatch.setattr(settings, "subscription_agents", "claude-code,codex")
    assert (
        settings.normalize_trial_model("claude-code", "claude-opus-4-8")
        == "claude-opus-4-8"
    )
    assert settings.normalize_trial_model("codex", "gpt-5.5") == "gpt-5.5"


def test_agent_flag_provider_and_queue_bucket(monkeypatch) -> None:
    monkeypatch.setattr(settings, "subscription_agents", "claude-code,codex")
    assert (
        settings.get_provider_for_trial("codex", "gpt-5.5") == "subscription"
    )
    assert (
        settings.get_provider_for_trial("claude-code", "claude-opus-4-8")
        == "subscription"
    )
    # Internal queue bucket carries the "sub/" prefix (so codex can be
    # serialized) even though the stored model id stays standard.
    assert settings.get_queue_key_for_trial("codex", "gpt-5.5") == "sub/gpt-5.5"
    assert (
        settings.get_queue_key_for_trial("claude-code", "claude-opus-4-8")
        == "sub/claude-opus-4-8"
    )
    # A flagged agent routes ALL its models to the subscription provider,
    # regardless of the specific model id.
    assert settings.get_provider_for_trial("codex", "gpt-5.2") == "subscription"


def test_agent_flag_off_is_inert(monkeypatch) -> None:
    monkeypatch.setattr(settings, "subscription_agents", "")
    # Codex falls back to its fixed "openai" provider when not flagged.
    assert settings.get_provider_for_trial("codex", "gpt-5.2") == "openai"


def test_claude_code_agent_flag_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "subscription_agents", "claude-code,codex")
    ac = hr._build_agent_config(
        agent="claude-code", model="claude-opus-4-8", raw_harbor_config={}
    )
    # STANDARD id reaches the agent (no "sub/"); the Bedrock chokepoint never ran.
    assert ac.model_name == "claude-opus-4-8"
    assert ac.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "${CS_CLAUDE_CODE_OAUTH_TOKEN}"
    assert ac.env.get("ANTHROPIC_API_KEY") == ""
    assert ac.env.get("CLAUDE_CODE_USE_BEDROCK") == ""
    assert ac.env.get("AWS_BEARER_TOKEN_BEDROCK") == ""


def test_codex_agent_flag_config_skips_azure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "subscription_agents", "claude-code,codex")
    ac = hr._build_agent_config(
        agent="codex", model="gpt-5.5", raw_harbor_config={}
    )
    assert ac.model_name == "gpt-5.5"
    # Routed through the plain Oddish wrapper, NOT the Azure-compat one.
    assert (ac.import_path or "").endswith("OddishCodex")
