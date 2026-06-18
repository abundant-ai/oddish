from __future__ import annotations

import contextlib
import os
import warnings
from typing import Any, Iterator

from harbor.models.trial.config import AgentConfig

from oddish.config import (
    FIREWORKS_DEFAULT_BASE_URL,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    OPENAI_PROVIDER_OPENAI,
    ZAI_DEFAULT_BASE_URL,
    fireworks_api_model_id,
    fireworks_bare_model_id,
    is_fireworks_model,
    is_minimax_model,
    is_moonshot_model,
    is_zai_model,
    minimax_api_model_id,
    minimax_bare_model_id,
    moonshot_bare_model_id,
    settings,
    to_anthropic_api_model_id,
    to_bedrock_model_id,
    to_fireworks_model_id,
    to_minimax_model_id,
    to_moonshot_model_id,
    to_zai_model_id,
    zai_bare_model_id,
)
from oddish.task_timeouts import PROBE_AGENT_TIMEOUT_SEC

_ODDISH_CODEX_IMPORT_PATH = "oddish.workers.codex_agent:OddishCodex"
_AZURE_COMPAT_CODEX_IMPORT_PATH = "oddish.workers.codex_agent:AzureCompatibleCodex"
_ODDISH_CLAUDE_CODE_IMPORT_PATH = "oddish.workers.claude_code_agent:OddishClaudeCode"


def _apply_claude_code_openrouter_env(agent_config: AgentConfig) -> None:
    """Apply the env shape Claude Code expects for OpenRouter's Anthropic skin."""
    agent_name = (agent_config.name or "").strip().lower()
    model_name = (agent_config.model_name or "").strip().lower()
    if agent_name != "claude-code" or not model_name.startswith("openrouter/"):
        return

    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${OPENROUTER_API_KEY}")
    env.setdefault("ENABLE_TOOL_SEARCH", "false")

    # Claude Code prioritizes these ambient credentials when present in the
    # Modal image. Blank them so the OpenRouter auth/base-url route wins.
    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


# Fireworks env is kept deliberately minimal: the default claude-code agent
# settings (no forced thinking / effort), matching how the same agent runs
# Claude/Opus.
_FIREWORKS_RECOMMENDED_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
}


def _apply_claude_code_fireworks_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to Fireworks' endpoint."""
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_fireworks_model(agent_config.model_name):
        return

    api_model = fireworks_api_model_id(
        fireworks_bare_model_id(agent_config.model_name or "")
    )
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("FIREWORKS_BASE_URL") or FIREWORKS_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${FIREWORKS_API_KEY}")

    if api_model:
        env["ANTHROPIC_MODEL"] = api_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, api_model)

    for key, value in _FIREWORKS_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


_ZAI_RECOMMENDED_ENV: dict[str, str] = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
    "API_TIMEOUT_MS": "3600000",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "3600000",
    "CLAUDE_CODE_EAGER_FLUSH": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
}


def _apply_claude_code_zai_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to z.ai's GLM endpoint."""
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_zai_model(agent_config.model_name):
        return

    bare_model = zai_bare_model_id(agent_config.model_name or "")
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${ZAI_API_KEY}")
    env.setdefault("ENABLE_TOOL_SEARCH", "false")

    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _ZAI_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env

    kwargs = dict(agent_config.kwargs or {})
    kwargs.setdefault("thinking", "adaptive")
    kwargs.setdefault("reasoning_effort", "max")
    agent_config.kwargs = kwargs


_MINIMAX_RECOMMENDED_ENV: dict[str, str] = {
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "512000",
}

_MOONSHOT_RECOMMENDED_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "262144",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32768",
}


def _apply_claude_code_minimax_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to MiniMax's direct endpoint."""
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_minimax_model(agent_config.model_name):
        return

    bare_model = minimax_api_model_id(
        minimax_bare_model_id(agent_config.model_name or "")
    )
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("MINIMAX_BASE_URL") or MINIMAX_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${MINIMAX_API_KEY}")

    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _MINIMAX_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


def _apply_claude_code_moonshot_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to Moonshot's Kimi endpoint."""
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_moonshot_model(agent_config.model_name):
        return

    bare_model = moonshot_bare_model_id(agent_config.model_name or "")
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("MOONSHOT_BASE_URL") or MOONSHOT_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${MOONSHOT_API_KEY}")

    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _MOONSHOT_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


def _apply_codex_azure_compat(agent_config: AgentConfig) -> None:
    """Route Azure Codex trials through Oddish's transport-compatible wrapper."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "codex":
        return
    if settings.get_openai_provider() != OPENAI_PROVIDER_AZURE:
        return

    agent_config.name = None
    agent_config.import_path = _AZURE_COMPAT_CODEX_IMPORT_PATH


def _apply_codex_oddish_wrapper(agent_config: AgentConfig) -> None:
    """Route Codex trials through Oddish's compatibility wrapper."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "codex":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_CODEX_IMPORT_PATH


def _apply_claude_code_probe_harbor(agent_config: AgentConfig, is_probe: bool) -> None:
    """Install the harbor package in the sandbox for probe claude-code trials."""
    if not is_probe or agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "claude-code":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_CLAUDE_CODE_IMPORT_PATH


def _agent_uses_bedrock() -> bool:
    """Mirror Harbor's claude-code Bedrock-mode detection."""
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip() == "1":
        return True
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        return True
    return False


def _claude_code_forces_direct_api(is_probe: bool) -> bool:
    """Whether a claude-code agent must use the direct Anthropic API over Bedrock."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    return is_probe or settings.claude_code_force_direct_api


def _build_agent_config(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
    is_probe: bool = False,
) -> AgentConfig:
    """Build Harbor's full AgentConfig, preserving rich per-trial fields."""
    raw_agent_config = raw_harbor_config.get("agent_config")
    agent_config = (
        AgentConfig.model_validate(raw_agent_config)
        if isinstance(raw_agent_config, dict)
        else AgentConfig(name=agent, model_name=model)
    )

    # Backward compatibility for rows persisted before Oddish stored full
    # Harbor AgentConfig payloads.
    raw_agent_overrides = raw_harbor_config.get("agent_overrides")
    legacy_overrides = (
        dict(raw_agent_overrides) if isinstance(raw_agent_overrides, dict) else {}
    )

    legacy_env = legacy_overrides.get("env")
    if isinstance(legacy_env, dict):
        agent_config.env = {**legacy_env, **agent_config.env}

    legacy_kwargs = legacy_overrides.get("kwargs")
    if isinstance(legacy_kwargs, dict):
        agent_config.kwargs = {**legacy_kwargs, **agent_config.kwargs}

    if (
        agent_config.override_timeout_sec is None
        and legacy_overrides.get("override_timeout_sec") is not None
    ):
        agent_config.override_timeout_sec = legacy_overrides["override_timeout_sec"]
    if (
        agent_config.override_setup_timeout_sec is None
        and legacy_overrides.get("override_setup_timeout_sec") is not None
    ):
        agent_config.override_setup_timeout_sec = legacy_overrides[
            "override_setup_timeout_sec"
        ]
    if (
        agent_config.max_timeout_sec is None
        and legacy_overrides.get("max_timeout_sec") is not None
    ):
        agent_config.max_timeout_sec = legacy_overrides["max_timeout_sec"]

    # Probe trials inherit an existing task's task.toml, which may carry a
    # multi-hour agent timeout (or none at all). Cap them at the probe default
    # unless the trial explicitly set its own override above.
    if is_probe and agent_config.override_timeout_sec is None:
        agent_config.override_timeout_sec = PROBE_AGENT_TIMEOUT_SEC

    if agent_config.import_path is None:
        agent_config.name = agent
    if model is not None:
        agent_config.model_name = model

    if is_fireworks_model(agent_config.model_name):
        agent_config.model_name = to_fireworks_model_id(agent_config.model_name)
    elif is_zai_model(agent_config.model_name):
        agent_config.model_name = to_zai_model_id(agent_config.model_name)
    elif is_minimax_model(agent_config.model_name):
        agent_config.model_name = to_minimax_model_id(agent_config.model_name)
    elif is_moonshot_model(agent_config.model_name):
        agent_config.model_name = to_moonshot_model_id(agent_config.model_name)
    elif _claude_code_forces_direct_api(is_probe):
        agent_config.model_name = to_anthropic_api_model_id(agent_config.model_name)
    elif _agent_uses_bedrock():
        agent_config.model_name = to_bedrock_model_id(agent_config.model_name)
    else:
        agent_config.model_name = to_anthropic_api_model_id(agent_config.model_name)

    _apply_claude_code_openrouter_env(agent_config)
    _apply_claude_code_fireworks_env(agent_config)
    _apply_claude_code_zai_env(agent_config)
    _apply_claude_code_minimax_env(agent_config)
    _apply_claude_code_moonshot_env(agent_config)

    if _agent_uses_openai_provider(agent_config):
        if settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            warnings.warn(settings.get_public_openai_warning(), stacklevel=2)
        else:
            agent_config.model_name = settings.resolve_azure_openai_deployment(
                agent_config.model_name
            )
            _apply_codex_azure_compat(agent_config)

    _apply_codex_oddish_wrapper(agent_config)
    _apply_claude_code_probe_harbor(agent_config, is_probe)

    return agent_config


def _agent_uses_openai_provider(agent_config: AgentConfig) -> bool:
    agent = getattr(agent_config, "name", None)
    if not agent:
        return False
    return (
        settings.get_provider_for_trial(
            agent,
            getattr(agent_config, "model_name", None),
        )
        == "openai"
    )


def _trial_requested_model(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
) -> tuple[str, str | None]:
    raw_agent_config = raw_harbor_config.get("agent_config")
    agent_name = agent
    model_name = model
    if isinstance(raw_agent_config, dict):
        agent_name = str(raw_agent_config.get("name") or agent_name)
        if model_name is None:
            raw_model_name = raw_agent_config.get("model_name")
            model_name = str(raw_model_name) if raw_model_name is not None else None
    return agent_name, model_name


def _trial_uses_openai_provider(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
) -> bool:
    agent_name, model_name = _trial_requested_model(
        agent=agent,
        model=model,
        raw_harbor_config=raw_harbor_config,
    )
    return settings.get_provider_for_trial(agent_name, model_name) == "openai"


@contextlib.contextmanager
def _temporary_env(env: dict[str, str]) -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
