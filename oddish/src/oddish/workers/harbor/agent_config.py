from __future__ import annotations

import contextlib
import os
import warnings
from collections.abc import Mapping
from typing import Any, Iterator

from harbor.models.trial.config import AgentConfig

from oddish.config import (
    FIREWORKS_DEFAULT_BASE_URL,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    OPENAI_PROVIDER_OPENAI,
    ZAI_DEFAULT_BASE_URL,
    anthropic_hdo_bare_model_id,
    anthropic_platform_bare_model_id,
    fireworks_api_model_id,
    fireworks_bare_model_id,
    is_anthropic_hdo_model,
    is_anthropic_platform_model,
    is_fireworks_model,
    is_meta_model,
    is_minimax_model,
    is_moonshot_model,
    is_openai_platform_prefixed,
    is_xai_model,
    is_zai_model,
    to_meta_model_id,
    minimax_api_model_id,
    minimax_bare_model_id,
    moonshot_bare_model_id,
    settings,
    to_anthropic_api_model_id,
    to_anthropic_hdo_model_id,
    to_anthropic_platform_model_id,
    to_bedrock_model_id,
    to_fireworks_model_id,
    to_minimax_model_id,
    to_moonshot_model_id,
    to_xai_model_id,
    to_zai_model_id,
    zai_bare_model_id,
)
from oddish.task_timeouts import PROBE_AGENT_TIMEOUT_SEC
from .restricted_network import agent_keeps_public_model_identity

_ODDISH_CODEX_IMPORT_PATH = "oddish.workers.agents.codex:OddishCodex"
_AZURE_COMPAT_CODEX_IMPORT_PATH = "oddish.workers.agents.codex:AzureCompatibleCodex"
_ODDISH_CLAUDE_CODE_IMPORT_PATH = "oddish.workers.agents.claude_code:OddishClaudeCode"
_ODDISH_PROBE_CLAUDE_CODE_IMPORT_PATH = (
    "oddish.workers.agents.claude_code:OddishProbeClaudeCode"
)
_ODDISH_CURSOR_CLI_IMPORT_PATH = "oddish.workers.agents.cursor_cli:OddishCursorCli"
_ODDISH_GROK_BUILD_IMPORT_PATH = "oddish.workers.agents.grok_build:OddishGrokBuild"
_ODDISH_GEMINI_CLI_IMPORT_PATH = "oddish.workers.agents.gemini_cli:OddishGeminiCli"
_ODDISH_MINI_SWE_IMPORT_PATH = "oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent"
_ODDISH_META_MINI_SWE_IMPORT_PATH = (
    "oddish.workers.agents.mini_swe_agent:OddishMetaMiniSweAgent"
)
_ANTHROPIC_MODEL_ALIAS_KEYS = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
_AMBIENT_ANTHROPIC_CREDENTIAL_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_BEARER_TOKEN_BEDROCK",
)

# Oddish agent alias for Moonshot's Claude Code eval harness. Hardcodes the
# vendor-required version / disallowed tools / long-context env; callers still
# pass ``--model``.
_KIMI_CLAUDE_CODE_AGENT = "kimi-claude-code"
_KIMI_CLAUDE_CODE_VERSION = "2.1.181"
_KIMI_CLAUDE_CODE_DISALLOWED_TOOLS = (
    "WebSearch WebFetch EnterPlanMode EnterWorktree "
    "ExitPlanMode ExitWorktree AskUserQuestion"
)
_KIMI_CLAUDE_CODE_RECOMMENDED_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "FORCE_AUTO_BACKGROUND_TASKS": "1",
    "ENABLE_BACKGROUND_TASKS": "1",
    "IS_SANDBOX": "1",
    "API_TIMEOUT_MS": "12000000",
    "BUN_CONFIG_HTTP_IDLE_TIMEOUT": "2000",
}


def _is_claude_code_agent(agent_config: AgentConfig) -> bool:
    return "claude-code" in (agent_config.name or "").strip().lower()


def _is_kimi_claude_code_agent(agent_config: AgentConfig) -> bool:
    return (agent_config.name or "").strip().lower() == _KIMI_CLAUDE_CODE_AGENT


def _prepare_kimi_claude_code_agent(agent_config: AgentConfig) -> None:
    """Apply vendor kwargs and moonshot-prefix the caller-supplied model."""
    if not _is_kimi_claude_code_agent(agent_config):
        return

    model = (agent_config.model_name or "").strip()
    if model:
        bare = (
            moonshot_bare_model_id(model)
            if is_moonshot_model(model)
            else model.split("/")[-1].strip()
        )
        if bare:
            agent_config.model_name = f"moonshot/{bare}"

    kwargs = dict(agent_config.kwargs or {})
    kwargs.setdefault("version", _KIMI_CLAUDE_CODE_VERSION)
    kwargs.setdefault("disallowed_tools", _KIMI_CLAUDE_CODE_DISALLOWED_TOOLS)
    agent_config.kwargs = kwargs


def _is_mini_swe_agent(agent_config: AgentConfig) -> bool:
    return (agent_config.name or "").strip().lower() == "mini-swe-agent"


def _to_litellm_claude_model_id(model: str | None) -> str | None:
    """Provider-prefix a Claude id for litellm-based (non claude-code) agents.

    claude-code consumes the bare Bedrock inference-profile id via its own
    InvokeModel transport; every other Harbor agent (mini-swe, ...) runs the
    model through litellm, which requires a ``provider/model`` id. Route Claude
    to the direct Anthropic API (``ANTHROPIC_API_KEY``, which Harbor forwards
    into the agent sandbox for ``anthropic/`` models) as ``anthropic/<api-id>``.
    Non-Claude and already-prefixed ids pass through unchanged.
    """
    api_id = to_anthropic_api_model_id(model)
    if not api_id or "/" in api_id:
        return api_id
    if "claude" in api_id.lower():
        return f"anthropic/{api_id}"
    return api_id


def _apply_anthropic_compat_env(
    agent_config: AgentConfig,
    *,
    base_url: str,
    auth_token: str,
    model: str | None = None,
    recommended_env: Mapping[str, str] | None = None,
) -> None:
    """Apply the shared Claude Code env shape for Anthropic-compatible APIs."""
    env = dict(agent_config.env or {})
    env.setdefault("ANTHROPIC_BASE_URL", base_url)
    env.setdefault("ANTHROPIC_AUTH_TOKEN", auth_token)

    if model:
        env["ANTHROPIC_MODEL"] = model
        for alias in _ANTHROPIC_MODEL_ALIAS_KEYS:
            env.setdefault(alias, model)

    for key, value in (recommended_env or {}).items():
        env.setdefault(key, value)

    # Claude Code prioritizes ambient credentials in the Modal image. Blank
    # them so the explicit Anthropic-compatible route wins.
    for key in _AMBIENT_ANTHROPIC_CREDENTIAL_KEYS:
        env[key] = ""

    agent_config.env = env


def _apply_claude_code_openrouter_env(agent_config: AgentConfig) -> None:
    """Apply the env shape Claude Code expects for OpenRouter's Anthropic skin."""
    agent_name = (agent_config.name or "").strip().lower()
    model_name = (agent_config.model_name or "").strip().lower()
    if agent_name != "claude-code" or not model_name.startswith("openrouter/"):
        return

    _apply_anthropic_compat_env(
        agent_config,
        base_url=os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        auth_token="${OPENROUTER_API_KEY}",
        recommended_env={"ENABLE_TOOL_SEARCH": "false"},
    )


# Fireworks env is kept deliberately minimal: the default claude-code agent
# settings (no forced thinking / effort), matching how the same agent runs
# Claude/Opus.
_FIREWORKS_RECOMMENDED_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
}


def _apply_claude_code_fireworks_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to Fireworks' endpoint."""
    if not _is_claude_code_agent(agent_config):
        return
    if not is_fireworks_model(agent_config.model_name):
        return

    api_model = fireworks_api_model_id(
        fireworks_bare_model_id(agent_config.model_name or "")
    )
    _apply_anthropic_compat_env(
        agent_config,
        base_url=os.environ.get("FIREWORKS_BASE_URL") or FIREWORKS_DEFAULT_BASE_URL,
        auth_token="${FIREWORKS_API_KEY}",
        model=api_model,
        recommended_env=_FIREWORKS_RECOMMENDED_ENV,
    )


_ZAI_RECOMMENDED_ENV: dict[str, str] = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
    "API_TIMEOUT_MS": "3600000",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "3600000",
    "CLAUDE_CODE_EAGER_FLUSH": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
}


def _apply_claude_code_zai_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to z.ai's GLM endpoint."""
    if not _is_claude_code_agent(agent_config):
        return
    if not is_zai_model(agent_config.model_name):
        return

    bare_model = zai_bare_model_id(agent_config.model_name or "")
    _apply_anthropic_compat_env(
        agent_config,
        base_url=os.environ.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL,
        auth_token="${ZAI_API_KEY}",
        model=bare_model,
        recommended_env={"ENABLE_TOOL_SEARCH": "false", **_ZAI_RECOMMENDED_ENV},
    )

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
    if not _is_claude_code_agent(agent_config):
        return
    if not is_minimax_model(agent_config.model_name):
        return

    bare_model = minimax_api_model_id(
        minimax_bare_model_id(agent_config.model_name or "")
    )
    _apply_anthropic_compat_env(
        agent_config,
        base_url=os.environ.get("MINIMAX_BASE_URL") or MINIMAX_DEFAULT_BASE_URL,
        auth_token="${MINIMAX_API_KEY}",
        model=bare_model,
        recommended_env=_MINIMAX_RECOMMENDED_ENV,
    )


def _apply_claude_code_moonshot_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to Moonshot's Kimi endpoint."""
    if not _is_claude_code_agent(agent_config):
        return
    if not is_moonshot_model(agent_config.model_name):
        return

    bare_model = moonshot_bare_model_id(agent_config.model_name or "")
    recommended = (
        _KIMI_CLAUDE_CODE_RECOMMENDED_ENV
        if _is_kimi_claude_code_agent(agent_config)
        else _MOONSHOT_RECOMMENDED_ENV
    )
    _apply_anthropic_compat_env(
        agent_config,
        base_url=os.environ.get("MOONSHOT_BASE_URL") or MOONSHOT_DEFAULT_BASE_URL,
        auth_token="${MOONSHOT_API_KEY}",
        model=bare_model,
        recommended_env=recommended,
    )


def _resolve_anthropic_hdo_api_key() -> str:
    """Return the HDO Anthropic key from settings or the process environment."""
    configured = (settings.anthropic_hdo_api_key or "").strip()
    if configured:
        return configured
    return (os.environ.get("ANTHROPIC_HDO_API_KEY") or "").strip()


def _resolve_anthropic_platform_api_key() -> str:
    """Return the platform Anthropic key from settings or the process environment."""
    configured = (settings.anthropic_api_key or "").strip()
    if configured:
        return configured
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _inject_direct_anthropic_env(
    agent_config: AgentConfig, *, bare_model: str, api_key: str
) -> None:
    """Pin a trial to the direct Anthropic API with *api_key*.

    Always overwrites: an empty key must not fall through to the Bedrock /
    ambient Anthropic credentials, and the Bedrock routing toggles are blanked
    so claude-code picks the direct API even on a Bedrock-equipped worker.
    """
    api_model = to_anthropic_api_model_id(bare_model) or bare_model
    env = dict(agent_config.env or {})
    env["ANTHROPIC_API_KEY"] = api_key
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    if _is_claude_code_agent(agent_config) and api_model:
        env["ANTHROPIC_MODEL"] = api_model
        for alias in _ANTHROPIC_MODEL_ALIAS_KEYS:
            env.setdefault(alias, api_model)
    agent_config.env = env


def _inject_anthropic_hdo_api_key(
    agent_config: AgentConfig, *, model_name: str | None
) -> None:
    """Overwrite ``ANTHROPIC_API_KEY`` with the HDO key for an HDO-prefixed trial.

    Call while the model still carries the ``anthropic-hdo/`` prefix (or pass
    that original id as *model_name*).
    """
    if not is_anthropic_hdo_model(model_name):
        return
    _inject_direct_anthropic_env(
        agent_config,
        bare_model=anthropic_hdo_bare_model_id(model_name or ""),
        api_key=_resolve_anthropic_hdo_api_key(),
    )


def _inject_anthropic_platform_api_key(
    agent_config: AgentConfig, *, model_name: str | None
) -> None:
    """Pin an ``anthropic/``-prefixed trial to the direct Anthropic API.

    Call while the model still carries the ``anthropic/`` prefix (or pass that
    original id as *model_name*).
    """
    if not is_anthropic_platform_model(model_name):
        return
    _inject_direct_anthropic_env(
        agent_config,
        bare_model=anthropic_platform_bare_model_id(model_name or ""),
        api_key=_resolve_anthropic_platform_api_key(),
    )


def _direct_anthropic_child_env(agent: str, model_name: str | None) -> dict[str, str]:
    """Ambient env pinning the direct Anthropic API for the override-Harbor child.

    The ephemeral child builds a plain ``AgentConfig`` and never runs
    ``_build_agent_config``'s injectors, so ``anthropic/`` and
    ``anthropic-hdo/`` routing must arrive as ambient env: the direct-API key,
    both Bedrock toggles blanked, and (for claude-code) the model pins
    ``_inject_direct_anthropic_env`` would have set. Empty for other models.
    """
    if is_anthropic_hdo_model(model_name):
        bare_model = anthropic_hdo_bare_model_id(model_name or "")
        api_key = _resolve_anthropic_hdo_api_key()
    elif is_anthropic_platform_model(model_name):
        bare_model = anthropic_platform_bare_model_id(model_name or "")
        api_key = _resolve_anthropic_platform_api_key()
    else:
        return {}
    api_model = to_anthropic_api_model_id(bare_model) or bare_model
    env = {
        "ANTHROPIC_API_KEY": api_key,
        "CLAUDE_CODE_USE_BEDROCK": "",
        "AWS_BEARER_TOKEN_BEDROCK": "",
    }
    if "claude-code" in (agent or "").strip().lower() and api_model:
        env["ANTHROPIC_MODEL"] = api_model
        for alias in _ANTHROPIC_MODEL_ALIAS_KEYS:
            env[alias] = api_model
    return env


def _apply_codex_azure_compat(agent_config: AgentConfig) -> None:
    """Route Azure Codex trials through Oddish's transport-compatible wrapper.

    Only called for trials whose model resolved to the Azure route
    (``get_openai_route_for_model``); public-platform Codex trials keep the
    stock ``OddishCodex`` wrapper against api.openai.com.
    """
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "codex":
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


def _apply_grok_build_oddish_wrapper(agent_config: AgentConfig) -> None:
    """Route Grok Build trials through Oddish's streaming trajectory wrapper."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "grok-build":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_GROK_BUILD_IMPORT_PATH
    kwargs = dict(agent_config.kwargs or {})
    kwargs.setdefault("reasoning_effort", "high")
    agent_config.kwargs = kwargs


def _apply_gemini_cli_oddish_wrapper(agent_config: AgentConfig) -> None:
    """Route Gemini CLI through the wrapper that can disable remote web tools."""
    if agent_config.import_path is not None:
        return
    if (agent_config.name or "").strip().lower() != "gemini-cli":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_GEMINI_CLI_IMPORT_PATH


def _apply_cursor_cli_oddish_wrapper(agent_config: AgentConfig) -> None:
    """Route Cursor through the restricted-Compose compatibility wrapper."""
    if agent_config.import_path is not None:
        return
    if (agent_config.name or "").strip().lower() != "cursor-cli":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_CURSOR_CLI_IMPORT_PATH


def _apply_meta_mini_swe_agent(agent_config: AgentConfig) -> None:
    """Route Meta model evals through mini-swe-agent with Meta API settings."""
    if agent_config.import_path is not None:
        return
    if not _is_mini_swe_agent(agent_config):
        return
    if not is_meta_model(agent_config.model_name):
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_META_MINI_SWE_IMPORT_PATH

    env = dict(agent_config.env or {})
    for key, value in settings.get_meta_agent_env().items():
        env.setdefault(key, value)
    agent_config.env = env

    # Preserve reasoning_effort so callers can request a specific effort
    # (e.g. --agent-kwarg reasoning_effort=xhigh). The mini-swe-agent harness
    # forwards it to the model via model.model_kwargs.extra_body.reasoning_effort
    # (see harbor MiniSweAgent.run). When the caller does not set it, effort
    # stays unset (vendor default), so other sampling params are untouched.
    agent_config.kwargs = dict(agent_config.kwargs or {})


def _apply_mini_swe_agent(agent_config: AgentConfig) -> None:
    if agent_config.import_path is not None or not _is_mini_swe_agent(agent_config):
        return
    agent_config.name = None
    agent_config.import_path = _ODDISH_MINI_SWE_IMPORT_PATH


def _apply_claude_code_oddish_wrapper(
    agent_config: AgentConfig, is_probe: bool
) -> None:
    """Keep all Claude prompts off argv; probes also install Harbor."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name not in {"claude-code", _KIMI_CLAUDE_CODE_AGENT}:
        return

    agent_config.name = None
    agent_config.import_path = (
        _ODDISH_PROBE_CLAUDE_CODE_IMPORT_PATH
        if is_probe
        else _ODDISH_CLAUDE_CODE_IMPORT_PATH
    )


def _apply_claude_code_probe_subagent_model(
    agent_config: AgentConfig, is_probe: bool
) -> None:
    """Pin a probe claude-code agent's subagent model.

    The per-provider env shapers (fireworks/z.ai/...) set
    ``CLAUDE_CODE_SUBAGENT_MODEL``, but Harbor's claude-code ``run`` only forwards
    it on the custom-base-url branch. On the direct Anthropic API / Bedrock path a
    probe uses, nothing sets it, so a subagent the probe spawns has no explicit
    model. Set it to the same (already provider-normalized) model id as the main
    agent so Task-tool subagents work. Call while ``name`` is still
    ``"claude-code"`` (before the probe-harbor wrapper nulls it); never overrides
    an existing value.
    """
    if not is_probe or not agent_config.model_name:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name not in {"claude-code", _KIMI_CLAUDE_CODE_AGENT}:
        return
    env = dict(agent_config.env or {})
    env.setdefault("CLAUDE_CODE_SUBAGENT_MODEL", agent_config.model_name)
    agent_config.env = env


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


def _apply_probe_oddish_creds(
    agent_config: AgentConfig, probe_env: dict[str, str] | None
) -> None:
    """Inject the minted read-only oddish CLI creds into the probe agent env.

    Applied last so it wins over any provider-specific env shaping above. The
    raw key lives only in process memory + the agent container env; it is never
    written into harbor_config or anything persisted to S3.
    """
    if not probe_env:
        return
    env = dict(agent_config.env or {})
    env.update(probe_env)
    agent_config.env = env


def _build_agent_config(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
    is_probe: bool = False,
    probe_oddish_env: dict[str, str] | None = None,
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

    # Before provider canonicalization: moonshot-prefix the model + vendor kwargs.
    _prepare_kimi_claude_code_agent(agent_config)

    if is_fireworks_model(agent_config.model_name):
        agent_config.model_name = to_fireworks_model_id(agent_config.model_name)
    elif is_meta_model(agent_config.model_name):
        agent_config.model_name = to_meta_model_id(agent_config.model_name)
    elif is_xai_model(agent_config.model_name):
        agent_config.model_name = to_xai_model_id(agent_config.model_name)
    elif is_zai_model(agent_config.model_name):
        agent_config.model_name = to_zai_model_id(agent_config.model_name)
    elif is_minimax_model(agent_config.model_name):
        agent_config.model_name = to_minimax_model_id(agent_config.model_name)
    elif is_moonshot_model(agent_config.model_name):
        agent_config.model_name = to_moonshot_model_id(agent_config.model_name)
    elif is_anthropic_hdo_model(agent_config.model_name):
        # Inject the HDO key before rewriting the model id so non-claude-code
        # agents (which become anthropic/<id>) still authenticate with it.
        hdo_model = agent_config.model_name
        _inject_anthropic_hdo_api_key(agent_config, model_name=hdo_model)
        canonical = to_anthropic_hdo_model_id(hdo_model)
        if _is_claude_code_agent(agent_config):
            # Keep the anthropic-hdo/ prefix for provider/queue/allowlist; the
            # injector pins ANTHROPIC_MODEL to the bare Anthropic API id.
            agent_config.model_name = canonical
        else:
            # litellm agents need anthropic/<api-id> plus ANTHROPIC_API_KEY.
            bare = anthropic_hdo_bare_model_id(canonical or "")
            api_id = to_anthropic_api_model_id(bare) or bare
            agent_config.model_name = f"anthropic/{api_id}" if api_id else canonical
    elif is_anthropic_platform_model(agent_config.model_name):
        # Explicit ``anthropic/`` pins the trial to the direct Anthropic API
        # with the platform key instead of the Bedrock default. Same shape as
        # the HDO branch above, with the platform ANTHROPIC_API_KEY.
        platform_model = agent_config.model_name
        _inject_anthropic_platform_api_key(agent_config, model_name=platform_model)
        canonical = to_anthropic_platform_model_id(platform_model)
        if _is_claude_code_agent(agent_config):
            # Keep the anthropic/ prefix for provider/queue/allowlist; the
            # injector pins ANTHROPIC_MODEL to the bare Anthropic API id.
            agent_config.model_name = canonical
        else:
            # litellm agents need anthropic/<api-id> plus ANTHROPIC_API_KEY.
            bare = anthropic_platform_bare_model_id(canonical or "")
            api_id = to_anthropic_api_model_id(bare) or bare
            agent_config.model_name = f"anthropic/{api_id}" if api_id else canonical
    elif not _is_claude_code_agent(agent_config):
        # litellm-based agents need a "provider/model" id; claude-code is the
        # only agent that consumes the bare Bedrock inference-profile id.
        agent_config.model_name = _to_litellm_claude_model_id(agent_config.model_name)
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
    _apply_claude_code_probe_subagent_model(agent_config, is_probe)

    # Gate on agent_keeps_public_model_identity: a harness that routes the model
    # through its own service (Cursor) or pins its egress to one provider
    # (gemini-cli -> Gemini, grok-build -> xAI) never talks to the OpenAI/Azure
    # endpoint, so its model must NOT be rewritten to the private Azure
    # deployment id -- it needs the public model identity, and a pinned
    # transport could never resolve a worker-private deployment id anyway.
    # Agents that talk to the endpoint directly (codex, mini-swe) get the
    # rewrite only on the Azure route: an explicit ``azure/`` id or a bare id
    # under the Azure default. ``openai/`` ids run on the public platform and
    # keep their public identity. This is the source the runner's runtime-model
    # swap later undoes for serialization/redaction; both gate the same way.
    if _agent_uses_openai_provider(
        agent_config
    ) and not agent_keeps_public_model_identity(agent_config):
        route = settings.get_openai_route_for_model(agent_config.model_name)
        if route == OPENAI_PROVIDER_AZURE:
            agent_config.model_name = settings.resolve_azure_openai_deployment(
                agent_config.model_name
            )
            _apply_codex_azure_compat(agent_config)
        elif settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI and (
            not is_openai_platform_prefixed(agent_config.model_name)
        ):
            # Public platform as the *global default* is the governance
            # exception worth flagging; an explicit openai/ prefix is an
            # intentional per-model choice and stays quiet even when the
            # default also happens to be public.
            warnings.warn(settings.get_public_openai_warning(), stacklevel=2)

    _apply_codex_oddish_wrapper(agent_config)
    _apply_grok_build_oddish_wrapper(agent_config)
    _apply_meta_mini_swe_agent(agent_config)
    _apply_mini_swe_agent(agent_config)
    _apply_claude_code_oddish_wrapper(agent_config, is_probe)
    _apply_probe_oddish_creds(agent_config, probe_oddish_env)
    # Direct-API keys must win over probe/BYOK/platform ANTHROPIC_API_KEY
    # merges above. Use the original *model* arg: non-claude-code agents
    # rewrite model_name to anthropic/<id> and would otherwise lose the
    # prefix signal (and an ``anthropic/``-rewritten litellm id must not
    # re-trigger the platform injector for a Bedrock-routed trial).
    if is_anthropic_platform_model(model):
        _inject_anthropic_platform_api_key(agent_config, model_name=model)
    if is_anthropic_hdo_model(model):
        _inject_anthropic_hdo_api_key(agent_config, model_name=model)

    return agent_config


# Canonical providers that name the OpenAI-family transport (public platform
# or Azure). Mirrors job_tokens._OPENAI_FAMILY; ``azure_openai`` rides along
# because the normalizer passes it through verbatim.
_OPENAI_FAMILY_PROVIDERS = frozenset({"openai", "azure", "azure_openai"})


def _agent_uses_openai_provider(agent_config: AgentConfig) -> bool:
    agent = getattr(agent_config, "name", None)
    if not agent:
        return False
    return (
        settings.get_provider_for_trial(
            agent,
            getattr(agent_config, "model_name", None),
        )
        in _OPENAI_FAMILY_PROVIDERS
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
    return (
        settings.get_provider_for_trial(agent_name, model_name)
        in _OPENAI_FAMILY_PROVIDERS
    )


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
