from __future__ import annotations

from harbor.models.trial.config import AgentConfig

from oddish.config import META_DEFAULT_BASE_URL
from oddish.workers.harbor.agent_config import _build_agent_config


def _opencode_meta_config(**kwargs) -> AgentConfig:
    return _build_agent_config(
        agent="opencode",
        model="meta/redacted_model",
        raw_harbor_config=kwargs.get("raw_harbor_config", {}),
    )


def test_opencode_meta_registers_openai_compatible_provider():
    cfg = _opencode_meta_config()

    # opencode is invoked with the canonical meta/<model> id.
    assert cfg.model_name == "meta/redacted_model"

    # Meta credential surfaced under OPENAI_API_KEY as a ${...} template so the
    # worker resolves it and never persists the raw key.
    assert cfg.env["OPENAI_API_KEY"] == "${META_API_KEY}"
    assert cfg.env["OPENAI_BASE_URL"].startswith("https://")

    provider = cfg.kwargs["opencode_config"]["provider"]["meta"]
    # /v1/chat/completions adapter, NOT the built-in openai /v1/responses one.
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == META_DEFAULT_BASE_URL.rstrip("/")
    # apiKey is an opencode {env:...} template resolved from the process env.
    assert provider["options"]["apiKey"] == "{env:OPENAI_API_KEY}"
    assert "redacted_model" in provider["models"]
    # Default reasoning effort is capped at "medium": Meta's ~32k completion
    # budget truncates a full-reasoning turn, stranding the agent.
    model_entry = provider["models"]["redacted_model"]
    assert model_entry["options"]["reasoningEffort"] == "medium"
    assert model_entry["limit"]["output"] >= 32000


def test_opencode_meta_preserves_caller_opencode_config_overrides():
    raw = {
        "agent_config": {
            "name": "opencode",
            "model_name": "meta/redacted_model",
            "kwargs": {
                "opencode_config": {
                    "provider": {
                        "meta": {
                            "options": {"baseURL": "https://relay.example/v1"},
                            "models": {"redacted_model": {"name": "Custom"}},
                        }
                    }
                }
            },
        }
    }
    cfg = _build_agent_config(
        agent="opencode",
        model="meta/redacted_model",
        raw_harbor_config=raw,
    )
    provider = cfg.kwargs["opencode_config"]["provider"]["meta"]
    # Caller override wins; defaults fill the gaps.
    assert provider["options"]["baseURL"] == "https://relay.example/v1"
    assert provider["options"]["apiKey"] == "{env:OPENAI_API_KEY}"
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    # Caller's model fields are preserved; defaults (reasoningEffort, limit) fill
    # the gaps around them.
    model_entry = provider["models"]["redacted_model"]
    assert model_entry["name"] == "Custom"
    assert model_entry["options"]["reasoningEffort"] == "medium"
    assert model_entry["limit"]["output"] >= 32000


def test_opencode_non_meta_model_is_not_meta_routed():
    cfg = _build_agent_config(
        agent="opencode",
        model="anthropic/claude-sonnet-4-5",
        raw_harbor_config={},
    )
    opencode_config = (cfg.kwargs or {}).get("opencode_config") or {}
    provider = opencode_config.get("provider") or {}
    assert "meta" not in provider


def test_mini_swe_agent_still_routed_for_meta_not_opencode():
    cfg = _build_agent_config(
        agent="mini-swe-agent",
        model="meta/redacted_model",
        raw_harbor_config={},
    )
    # mini-swe keeps its own import-path wrapper and does not gain an
    # opencode_config block.
    assert "opencode" not in (cfg.import_path or "")
    assert "opencode_config" not in (cfg.kwargs or {})
