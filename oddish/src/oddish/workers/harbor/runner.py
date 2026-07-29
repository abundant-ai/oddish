from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.environments.kube_ops import kube_chart_present
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import (
    EnvironmentConfig,
    MCPServerConfig,
    NetworkMode,
    TaskConfig as HarborTaskConfig,
    normalize_allowed_hosts,
)
from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
from harbor.models.trial.config import AgentConfig as HarborAgentConfig
from harbor.models.trial.config import EnvironmentConfig as HarborEnvironmentConfig
from harbor.models.trial.config import ResourceMode
from harbor.models.trial.config import TaskConfig
from harbor.trial.hooks import TrialHookEvent
from harbor.utils.env import resolve_env_vars

from oddish.config import (
    BEDROCK_ENV_VARS,
    OPENAI_PROVIDER_AZURE,
    infer_model_provider_prefix,
    is_anthropic_hdo_model,
    is_anthropic_platform_model,
    settings,
)
from oddish.costs.modal_cost import (
    SpanResources,
    normalize_gpu_type,
    provider_default_request,
)
from oddish.runtime.registry import get_backend
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config
from oddish.worker.probe_staging import stage_org_skills
from .agent_config import (
    _build_agent_config,
    _claude_code_forces_direct_api,
    _apply_gemini_cli_oddish_wrapper,
    _apply_cursor_cli_oddish_wrapper,
    _resolve_anthropic_hdo_api_key,
    _resolve_anthropic_platform_api_key,
    _temporary_env,
    _trial_requested_model,
    _trial_uses_openai_provider,
)
from .model_hosts import (
    GEMINI_BASE_URL_KEYS,
    GEMINI_OAUTH_ENV_KEYS,
    agent_runtime_hosts,
    outbound_hosts_for_model,
)
from .redaction import redact_exact_text, redact_exact_value
from .restricted_network import (
    _KNOWN_TRANSPORT_BASE_URL_KEYS,
    RestrictedNetworkProfile,
    RestrictedNetworkProfileError,
    agent_keeps_public_model_identity,
    apply_restricted_network_profile,
    assert_no_serialized_restricted_routes,
    consumed_transport_base_url_keys,
    is_static_restricted_agent_supported,
    reject_submitted_restricted_routes,
    set_runtime_model_name,
)
from .modal_debug import (
    _capture_modal_output,
    _format_exception_message,
    _maybe_add_modal_debug_hint,
    _write_debug_result_json,
)
from .outcome import (
    HarborOutcome,
    _extract_outcome_from_job_result,
)
from .patches import apply_harbor_patches
from .storage import (
    _MIN_REQUIRED_FREE_GB,
    _MIN_REQUIRED_FREE_INODES,
    _probe_storage_root,
    _storage_probe_paths,
    log_local_storage_snapshot,
)

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]


# Harbor's default task-environment ``build_timeout_sec`` -- the base it
# multiplies. Sizing reads each task's own value; this is only the fallback base
# when a task's task.toml cannot be read. Sourced from Harbor so it cannot drift.
_ENV_BUILD_TIMEOUT_BASE_SEC: float = EnvironmentConfig.model_fields[
    "build_timeout_sec"
].default
# Headroom the GKE outer wait needs beyond the Pod's ready/capacity wait: it
# covers Harbor-side environment construction and keeps the outer cap strictly
# above the inner pod-ready timeout so the more specific inner error surfaces.
_GKE_ENV_BUILD_OVERHEAD_SEC = 300.0

# Existing setup-only compatibility for Claude Code. This predates the Daytona
# Compose agent-phase bridge below and remains unchanged for Modal and
# single-container trials.
_CLAUDE_CODE_INSTALLER_HOSTS = ("downloads.claude.ai", "registry.npmjs.org")
# Gemini env the stock gemini-cli agent forwards: its transport base-URL keys
# and its OAuth toggles. Both are single-sourced in model_hosts (the same source
# the restricted-egress filter and host discovery read), so this fold cannot
# drift from the host boundary the way a re-listed copy would.
_GEMINI_RUNTIME_ENV_KEYS = (*GEMINI_BASE_URL_KEYS, *GEMINI_OAUTH_ENV_KEYS)
# Ambient Gemini credentials must fold into the trial's redaction map the same
# way get_openai_agent_env folds OpenAI provider secrets, so a worker key used
# when job-scoped injection is off never survives raw in live-tail, lifecycle
# payloads, or scrubbed artifacts. These are credentials, not routes: they never
# affect transport-host selection (not base URLs), only redaction coverage. They
# are the only secret-VALUE env vars the stock gemini-cli agent forwards;
# GOOGLE_APPLICATION_CREDENTIALS is intentionally excluded because its value is a
# file path -- its secret is the file's contents, outside this exact-value map.
_GEMINI_RUNTIME_SECRET_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)
# Ambient claude-code platform credentials must fold into the redaction map for
# the same reason: when job-scoped injection is off, the direct/OAuth Anthropic
# credential or the Bedrock credential chain the stock agent forwards is only in
# the worker's os.environ, so without this it would never enter
# _runtime_transport_redactions and could survive raw in live-tail rows,
# lifecycle payloads, or scrubbed artifacts. Includes the full AWS chain the
# Bedrock path forwards (bearer token + access-key/secret/session). Credentials,
# not routes -- redaction coverage only, never transport-host selection; every
# name contains key/token/secret so the generic matcher redacts them once pulled.
_CLAUDE_RUNTIME_SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
# Ambient xAI/grok credentials the stock grok-build agent forwards into sandbox
# execs; folded into the redaction map for the same reason -- credentials, not
# routes: redaction coverage only, never transport-host selection.
_GROK_RUNTIME_SECRET_KEYS = (
    "XAI_API_KEY",
    "XAI_API_KEYS",
)
# Ambient Cursor auth the stock cursor-cli agent forwards into the sandbox
# (cursor_cli.py reads CURSOR_API_KEY from os.environ). Folded into the redaction
# map for the same reason -- credential, not a route: redaction coverage only.
_CURSOR_RUNTIME_SECRET_KEYS = ("CURSOR_API_KEY",)
# Ambient Azure OpenAI credentials, shared by both spellings of the provider
# below so the two rows cannot drift apart.
_AZURE_RUNTIME_SECRET_KEYS = (
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
)
# Provider-driven credential fold for stock agents (notably mini-swe) that
# authenticate from the worker os.environ by the model's provider rather than a
# fixed agent harness. Keyed on the CANONICAL provider from
# infer_model_provider_prefix; values are the ambient credential env vars each
# provider forwards. Redaction coverage only -- these are never transport routes.
# CLAUDE_CODE_OAUTH_TOKEN rides in the anthropic providers as defense-in-depth:
# it keeps OAuth-token redaction from resting solely on the is_claude_code
# class-name branch, so an anthropic-provider trial still scrubs it even if a
# future supported Claude profile were named without "claude_code". Folding a key
# absent from os.environ is a no-op, so this never over-redacts.
_PROVIDER_RUNTIME_SECRET_KEYS: dict[str, tuple[str, ...]] = {
    # ``openai`` covers both transports the provider can resolve to (an
    # explicit ``openai/`` id runs on the public platform; a bare id follows
    # the Azure default), so fold the Azure credentials too — folding a key
    # absent from os.environ is a no-op.
    "openai": _AZURE_RUNTIME_SECRET_KEYS,
    # Oddish routes OpenAI-family jobs through Azure OpenAI by default, and an
    # explicit ``azure/`` id is a first-class restricted provider (it resolves
    # real transport keys and a real host), so its ambient credentials need the
    # same redaction coverage as every other provider here. Both spellings are
    # listed for the same reason _model_transport_base_url_keys lists both:
    # ``azure_openai`` is not in the normalizer's provider set, so
    # infer_model_provider_prefix passes it through verbatim and a map keyed on
    # only ``azure`` would silently miss every ``azure_openai/`` trial.
    "azure": _AZURE_RUNTIME_SECRET_KEYS,
    "azure_openai": _AZURE_RUNTIME_SECRET_KEYS,
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "anthropic-hdo": (
        "ANTHROPIC_HDO_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "bedrock": (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY", "XAI_API_KEYS"),
    "meta": ("META_API_KEY", "OPENAI_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}
_ARTIFACT_REDACTION_CHUNK_BYTES = 1024 * 1024


def _resolved_runtime_transport_env(
    openai_env: dict[str, str] | None = None,
    *,
    agent_config: HarborAgentConfig | None = None,
) -> dict[str, str]:
    """Collect only worker routes consumed by the selected effective agent."""
    runtime_env = dict(openai_env or {})
    if agent_config is not None:
        name = (agent_config.name or "").strip().lower()
        import_path = (agent_config.import_path or "").strip().lower()
        # Match the harness on the ``<module>:`` class-path boundary, not an
        # ``agents.<module>:`` fragment. A trial can run the STOCK Harbor class
        # (``harbor.agents.installed.claude_code:ClaudeCode``, a supported
        # restricted profile) with ``name`` cleared, and that path contains
        # ``installed.claude_code:`` -- so an ``agents.claude_code:`` fragment
        # misses it, leaving CLAUDE_CODE_OAUTH_TOKEN and the other Claude-only
        # ambient secrets out of the redaction map. The boundary form matches the
        # stock class, every Oddish wrapper, and the claude-code-family
        # derivatives (glm/kimi/minimax_claude_code, all ClaudeCode subclasses
        # that forward the same credentials, so folding them is correct). The
        # trailing ``:`` anchors on the module->class boundary, so it does not
        # match a same-prefix-but-different module (e.g. a future
        # ``claude_code_v2``) or any module whose name lacks ``claude_code``.
        # Folding is redaction-coverage only -- it never adds a base-URL key, so
        # it cannot touch host selection or the fail-closed consumed-route guard.
        # This mirrors the cursor branch below.
        is_gemini = name == "gemini-cli" or "gemini_cli:" in import_path
        if is_gemini:
            for key in (*_GEMINI_RUNTIME_ENV_KEYS, *_GEMINI_RUNTIME_SECRET_KEYS):
                if key not in runtime_env and (value := os.environ.get(key)):
                    runtime_env[key] = value
        is_claude_code = name == "claude-code" or "claude_code:" in import_path
        if is_claude_code:
            for key in _CLAUDE_RUNTIME_SECRET_KEYS:
                if key not in runtime_env and (value := os.environ.get(key)):
                    runtime_env[key] = value
        is_grok = name == "grok-build" or "grok_build:" in import_path
        if is_grok:
            for key in _GROK_RUNTIME_SECRET_KEYS:
                if key not in runtime_env and (value := os.environ.get(key)):
                    runtime_env[key] = value
        is_cursor = name == "cursor-cli" or "cursor_cli:" in import_path
        if is_cursor:
            for key in _CURSOR_RUNTIME_SECRET_KEYS:
                if key not in runtime_env and (value := os.environ.get(key)):
                    runtime_env[key] = value
        # General provider-driven fold: a stock agent (e.g. mini-swe) that
        # authenticates by the model's provider from ambient os.environ, with no
        # agent-specific branch above. Keyed on the canonical provider so
        # anthropic / Bedrock / xai / etc. credentials are still redacted.
        provider = infer_model_provider_prefix(agent_config.model_name)
        for key in _PROVIDER_RUNTIME_SECRET_KEYS.get(provider or "", ()):
            if key not in runtime_env and (value := os.environ.get(key)):
                runtime_env[key] = value
        # Drop worker-injected model-transport base URLs the effective agent's
        # restricted profile does not consume, honoring this function's contract
        # of collecting "only worker routes consumed by the selected effective
        # agent." Worker route injection can surface one provider's *_BASE_URL
        # (e.g. an Azure OPENAI_BASE_URL on an OpenAI-provider trial) while the
        # effective agent fronts an unrelated transport (e.g. Cursor -> only
        # *.cursor.sh). Left in, that stray *known* transport key would trip the
        # profile's fail-closed "does not consume" guard in
        # _selected_transport_hosts and fail an otherwise valid trial before
        # Job.create -- even though the key is never granted egress. Non-transport
        # keys (credentials, feature flags) are preserved; an indeterminate
        # effective agent (custom hook / unrecognised class -> keys is None) is
        # left untouched, since those paths never reach _selected_transport_hosts.
        consumed = consumed_transport_base_url_keys(agent_config)
        if consumed is not None:
            allowed = frozenset(consumed)
            runtime_env = {
                key: value
                for key, value in runtime_env.items()
                if key not in _KNOWN_TRANSPORT_BASE_URL_KEYS or key in allowed
            }
    return runtime_env


def _drop_nonconsumed_agent_transport_routes(
    agent_config: HarborAgentConfig,
    worker_minted_env: Mapping[str, str] | None = None,
) -> None:
    """Drop WORKER-MINTED transport routes the effective agent never consumes.

    Job-scoped credential injection (``job_tokens.scoped_model_env``) writes the
    model provider's full route env -- for OpenAI-family that includes
    ``OPENAI_BASE_URL`` and the Azure aliases -- into ``agent_config.env``, a
    channel the worker-transport filter above never sees. An agent that fronts
    its own transport (e.g. Cursor or Gemini on an ``openai/`` model) does not
    consume those routes: left in place they trip the fail-closed "does not
    consume" guard and kill an otherwise valid trial before ``Job.create``.
    Dropping them also keeps the worker's private endpoint out of a sandbox that
    never dials it -- but only in combination with the profile pinning its own
    transport (``infer_model=False``), since host inference would otherwise
    re-derive that same endpoint from the model id after this drop.

    Only routes present in *worker_minted_env* (same key AND same value) are
    dropped. Everything else is deliberately left for the fail-closed guard,
    because silently deleting it would turn that guard into a no-op: a submitted
    ``extra_env`` naming an irrelevant transport still fails the trial rather
    than disappearing. That deliberately errs toward failing closed --
    *worker_minted_env* carries the provider env and job-scoped token env, not
    routes ``_build_agent_config`` shapes in (e.g. the ``ANTHROPIC_BASE_URL`` a
    z.ai/Fireworks claude-code trial gets), so those survive here too. For every
    stock shape that is harmless, since the class that receives such a route
    also consumes it; a mismatched caller ``import_path`` would fail closed.

    Credentials and non-route keys are preserved, and an indeterminate effective
    agent (``consumed`` is ``None``) is left untouched, matching
    ``_resolved_runtime_transport_env``. Passing no *worker_minted_env* drops
    nothing, so the default is fail-closed rather than fail-open.
    """
    consumed = consumed_transport_base_url_keys(agent_config)
    if consumed is None:
        return
    allowed = frozenset(consumed)
    minted = dict(worker_minted_env or {})

    def _filtered(mapping: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in mapping.items()
            if key not in _KNOWN_TRANSPORT_BASE_URL_KEYS
            or key in allowed
            or minted.get(key) != value
        }

    if agent_config.env:
        agent_config.env = _filtered(agent_config.env)
    kwargs = agent_config.kwargs or {}
    extra_env = kwargs.get("extra_env")
    if isinstance(extra_env, Mapping):
        kwargs = dict(kwargs)
        kwargs["extra_env"] = _filtered(extra_env)
        agent_config.kwargs = kwargs


def _resolved_agent_profile_env(
    agent_config: HarborAgentConfig,
) -> dict[str, str]:
    """Resolve both env channels consumed by the selected agent instance."""
    resolved = resolve_env_vars(agent_config.env) if agent_config.env else {}
    extra_env = (agent_config.kwargs or {}).get("extra_env")
    if not isinstance(extra_env, Mapping):
        return resolved
    resolved_extra = resolve_env_vars(dict(extra_env))
    conflicts = sorted(
        key
        for key, value in resolved_extra.items()
        if key in resolved and resolved[key] != value
    )
    if conflicts:
        raise RestrictedNetworkProfileError(
            "Restricted agent transport received conflicting env and extra_env "
            f"settings for: {', '.join(conflicts)}."
        )
    return {**resolved, **resolved_extra}


def _runtime_transport_redactions(
    runtime_env: dict[str, str],
    *,
    runtime_model: str | None = None,
    public_model: str | None = None,
) -> dict[str, str]:
    """Build exact worker-only replacements for persisted textual output."""
    replacements: dict[str, str] = {}
    # "deployment" covers the worker-private Azure deployment id
    # (AZURE_OPENAI_DEPLOYMENT). It is redacted in this agent-independent first
    # pass so its value is scrubbed even for agents that skip the runtime-model
    # swap (e.g. Cursor, which keeps the public model and never registers a
    # deployment->public replacement); for agents that do swap, that later
    # replacement simply overrides this one.
    sensitive_fragments = (
        "key",
        "token",
        "secret",
        "password",
        "credential",
        "deployment",
    )
    route_fragments = ("url", "base", "endpoint")
    for key, value in runtime_env.items():
        if not value:
            continue
        lowered = key.lower()
        if any(fragment in lowered for fragment in sensitive_fragments):
            replacements[value] = "[REDACTED]"
        elif any(fragment in lowered for fragment in route_fragments):
            replacements[value] = "https://runtime-model-endpoint.invalid"
            host = urlparse(value).hostname
            if host:
                replacements[host] = "runtime-model-endpoint.invalid"
    if runtime_model and runtime_model != public_model:
        replacements[runtime_model] = public_model or "runtime-model"
    return replacements


def _redact_trial_hook_event(
    event: TrialHookEvent,
    replacements: dict[str, str],
) -> TrialHookEvent:
    """Redact an event copy before any Oddish lifecycle callback observes it."""
    if not replacements:
        return event
    updates = {
        name: redact_exact_value(getattr(event, name), replacements, _depth=1)
        for name in type(event).model_fields
        if name != "environment"
    }
    # The live environment is an opaque active handle. Never traverse or copy
    # it; only the serializable lifecycle payload needs exact-value redaction.
    updates["environment"] = event.environment
    return event.model_copy(update=updates)


def _redacting_hook_callback(
    callback: HookCallback | None,
    replacements: dict[str, str],
) -> HookCallback | None:
    if callback is None or not replacements:
        return callback

    async def redacted_callback(event: TrialHookEvent) -> None:
        await callback(_redact_trial_hook_event(event, replacements))

    return redacted_callback


def _replace_safe_binary_prefix(
    data: bytes,
    process_before: int,
    replacements: tuple[tuple[bytes, bytes], ...],
) -> tuple[bytes, int, bool]:
    """Replace matches starting before a safe raw-byte boundary."""
    output = bytearray()
    cursor = 0
    changed = False
    while cursor < process_before:
        match: tuple[int, bytes, bytes] | None = None
        for needle, replacement in replacements:
            index = data.find(needle, cursor)
            if index < 0 or index >= process_before:
                continue
            candidate = (index, needle, replacement)
            if (
                match is None
                or index < match[0]
                or (index == match[0] and len(needle) > len(match[1]))
            ):
                match = candidate
        if match is None:
            output.extend(data[cursor:process_before])
            cursor = process_before
            break
        index, needle, replacement = match
        output.extend(data[cursor:index])
        output.extend(replacement)
        cursor = index + len(needle)
        changed = True
    return bytes(output), cursor, changed


def _redact_runtime_transport_file(
    path: Path,
    replacements: dict[str, str],
) -> None:
    """Atomically redact one artifact with bounded memory, including binaries."""
    byte_replacements = tuple(
        sorted(
            (
                (value.encode("utf-8"), replacement.encode("utf-8"))
                for value, replacement in replacements.items()
                if value
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    if not byte_replacements:
        return
    overlap = max(len(needle) for needle, _ in byte_replacements) - 1
    temporary_path: Path | None = None
    changed = False
    try:
        with (
            path.open("rb") as source,
            tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".oddish-redact-", delete=False
            ) as target,
        ):
            temporary_path = Path(target.name)
            pending = b""
            chunk = source.read(_ARTIFACT_REDACTION_CHUNK_BYTES)
            while chunk:
                next_chunk = source.read(_ARTIFACT_REDACTION_CHUNK_BYTES)
                pending += chunk
                process_before = (
                    len(pending) if not next_chunk else max(0, len(pending) - overlap)
                )
                output, consumed, replaced = _replace_safe_binary_prefix(
                    pending, process_before, byte_replacements
                )
                target.write(output)
                pending = pending[consumed:]
                changed = changed or replaced
                chunk = next_chunk
            if pending:
                output, consumed, replaced = _replace_safe_binary_prefix(
                    pending, len(pending), byte_replacements
                )
                target.write(output)
                if consumed != len(pending):
                    raise RuntimeError("artifact redaction did not consume its input")
                changed = changed or replaced
        if changed and temporary_path is not None:
            shutil.copystat(path, temporary_path)
            os.replace(temporary_path, path)
            temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _scrub_runtime_transport_files(root: Path, replacements: dict[str, str]) -> None:
    """Remove worker-only routes from trial output before artifact upload."""
    if not replacements or not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            _redact_runtime_transport_file(path, replacements)
        except OSError:
            continue


def _resource_bounds(
    value: int | None,
    mode: ResourceMode,
    *,
    default_request: float,
    auto_is_request: bool,
) -> tuple[float | None, float | None]:
    if value is None or mode == ResourceMode.IGNORE:
        return None, None
    if mode == ResourceMode.REQUEST or (mode == ResourceMode.AUTO and auto_is_request):
        return float(value), None
    if mode == ResourceMode.LIMIT:
        return min(default_request, float(value)), float(value)
    return float(value), float(value)


def _unknown_sandbox_resources() -> SpanResources:
    return SpanResources(
        cpu_request=None,
        cpu_limit=None,
        mem_request_mb=None,
        mem_limit_mb=None,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=Decimal(1),
        container_class="sandbox",
        spec_source="unknown",
    )


def _resources_from_environment_config(
    env: Any, overrides: Any, provider: str = "modal"
) -> SpanResources:
    env = env.model_copy(deep=True)
    if overrides.override_cpus is not None:
        env.cpus = overrides.override_cpus
    if overrides.override_memory_mb is not None:
        env.memory_mb = overrides.override_memory_mb
    if overrides.override_gpus is not None:
        env.gpus = overrides.override_gpus

    # The LIMIT-enforcement request floor is the provider's minimum request,
    # not Modal's -- a Daytona sandbox reserves 1 vCPU / 1 GiB, not Modal's
    # 0.125 core / 128 MiB, so a hardcoded Modal floor underprices it.
    default_cpu, default_mem = provider_default_request(provider)
    cpu_mode = ResourceMode(overrides.cpu_enforcement_policy)
    mem_mode = ResourceMode(overrides.memory_enforcement_policy)
    cpu_request, cpu_limit = _resource_bounds(
        env.cpus,
        cpu_mode,
        default_request=default_cpu,
        auto_is_request=False,
    )
    mem_request, mem_limit = _resource_bounds(
        env.memory_mb,
        mem_mode,
        default_request=default_mem,
        auto_is_request=True,
    )
    has_override = any(
        value is not None
        for value in (
            overrides.override_cpus,
            overrides.override_memory_mb,
            overrides.override_gpus,
        )
    )
    pinned = any(value is not None for value in (env.cpus, env.memory_mb, env.gpus))
    return SpanResources(
        cpu_request=cpu_request,
        cpu_limit=cpu_limit,
        mem_request_mb=int(mem_request) if mem_request is not None else None,
        mem_limit_mb=int(mem_limit) if mem_limit is not None else None,
        gpu_type=normalize_gpu_type(env.gpu_types[0] if env.gpu_types else None),
        gpu_count=env.gpus or 0,
        price_multiplier=Decimal(1),
        container_class="sandbox",
        spec_source=(
            "override" if has_override else "pinned" if pinned else "provider_default"
        ),
        cpu_enforcement_mode=cpu_mode.value,
        mem_enforcement_mode=mem_mode.value,
    )


def capture_sandbox_resources(
    task_path: Path, harbor_config: dict[str, Any] | None, provider: str = "modal"
) -> SpanResources:
    """Snapshot the effective agent resources before an ephemeral fork."""
    try:
        task = HarborTaskConfig.model_validate_toml(
            (task_path / "task.toml").read_text()
        )
        hc = HarborConfig.model_validate(harbor_config or {})
        return _resources_from_environment_config(
            task.environment, hc.environment, provider
        )
    except Exception:
        return _unknown_sandbox_resources()


def capture_verifier_resources(
    task_path: Path, harbor_config: dict[str, Any] | None, provider: str = "modal"
) -> SpanResources | None:
    """Return the separate verifier's effective resources, if it has one."""
    try:
        task = HarborTaskConfig.model_validate_toml(
            (task_path / "task.toml").read_text()
        )
        hc = HarborConfig.model_validate(harbor_config or {})
        for step in task.steps or [None]:
            env = resolve_effective_verifier_env_config(task, step)
            if env is not None:
                return _resources_from_environment_config(env, hc.environment, provider)
        return None
    except Exception:
        return None


def capture_live_sandbox_resources(
    environment: Any | None, fallback: SpanResources, provider: str = "modal"
) -> SpanResources:
    """Prefer the live Harbor environment's merged resource configuration.

    This reads Modal-only accessors (``_cpu_config`` / ``_memory_config``), so
    it applies only to Modal sandboxes. For any other provider we keep the
    ``fallback`` (the provider-aware pre-fork snapshot) rather than relying on
    an AttributeError to bail out -- otherwise a provider whose env happened to
    expose those names could overwrite a correct Daytona floor with Modal's.
    """
    if environment is None or provider != "modal":
        return fallback
    try:
        env = environment.task_env_config
        cpu_mode = ResourceMode(environment._cpu_resource_mode)
        mem_mode = ResourceMode(environment._memory_resource_mode)

        def split(value: Any) -> tuple[float | None, float | None]:
            if value is None:
                return None, None
            if isinstance(value, tuple):
                return float(value[0]), float(value[1])
            return float(value), None

        cpu_request, cpu_limit = split(environment._cpu_config())
        mem_request, mem_limit = split(environment._memory_config())
        has_override = any(
            value is not None
            for value in (
                environment._override_cpus,
                environment._override_memory_mb,
                environment._override_gpus,
            )
        )
        pinned = any(value is not None for value in (env.cpus, env.memory_mb, env.gpus))
        return SpanResources(
            cpu_request=cpu_request,
            cpu_limit=cpu_limit,
            mem_request_mb=int(mem_request) if mem_request is not None else None,
            mem_limit_mb=int(mem_limit) if mem_limit is not None else None,
            gpu_type=normalize_gpu_type(env.gpu_types[0] if env.gpu_types else None),
            gpu_count=env.gpus or 0,
            price_multiplier=Decimal(1),
            container_class="sandbox",
            spec_source=(
                "override"
                if has_override
                else "pinned"
                if pinned
                else "provider_default"
            ),
            cpu_enforcement_mode=cpu_mode.value,
            mem_enforcement_mode=mem_mode.value,
        )
    except Exception:
        return fallback


def _sized_environment_build_timeout_multiplier(
    *,
    environment: EnvironmentType,
    environment_build_timeout_multiplier: float | None,
    timeout_multiplier: float | None,
    pod_ready_timeout_sec: int,
    base_sec: float,
) -> float | None:
    """Grow the environment-build timeout multiplier to cover a GKE Pod's
    capacity/pod-ready wait; a no-op for every other environment.

    A GKE Pod can sit Pending through a DWS flex-start capacity wait for up to
    ``pod_ready_timeout_sec`` before it reports ready. Harbor wraps
    ``environment.start()`` in an outer ``wait_for`` of the effective multiplier
    times the task's own ``build_timeout_sec`` (``base_sec``). Sizing against a
    fixed base would fall short whenever a task sets ``build_timeout_sec`` below
    the default, so the multiplier is computed against the task's real base and
    the outer wait clears ``pod_ready_timeout_sec`` plus build overhead
    regardless of how small that base is. Never lower a larger caller value;
    Harbor resolves an unset env-build multiplier to the general
    ``timeout_multiplier``, so that is honoured as the floor too.

    Returns the multiplier to store -- unchanged (possibly ``None``) off GKE.
    """
    if environment != EnvironmentType.GKE:
        return environment_build_timeout_multiplier
    effective_current = (
        environment_build_timeout_multiplier
        if environment_build_timeout_multiplier is not None
        else (timeout_multiplier if timeout_multiplier is not None else 1.0)
    )
    needed = math.ceil((pod_ready_timeout_sec + _GKE_ENV_BUILD_OVERHEAD_SEC) / base_sec)
    return float(max(effective_current, needed))


def _effective_pod_ready_timeout_sec(
    env_kwargs: dict[str, Any], default_sec: int
) -> int:
    """The pod-ready timeout Harbor will actually enforce for a GKE Pod.

    ``GkeBackend.harbor_env_kwargs`` seeds ``pod_ready_timeout_sec`` from the
    platform default but lets a submission override it (caller-wins), and that
    override can arrive as a string via ``--environment-kwarg``. Read the merged
    value, coercing to int, and fall back to ``default_sec`` when it is absent or
    unparseable, so the outer build wait is sized to the timeout the Pod is
    really given rather than the smaller raw setting.
    """
    raw = env_kwargs.get("pod_ready_timeout_sec")
    if raw is None:
        return default_sec
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default_sec


def _effective_task_build_timeout_sec(task_path: Path) -> float:
    """The task's own environment ``build_timeout_sec`` -- the base Harbor
    multiplies by ``environment_build_timeout_multiplier`` for the outer build
    wait.

    Read from the task's task.toml so the GKE outer wait is sized against the
    real base even when a task sets a sub-default ``build_timeout_sec``. Falls
    back to Harbor's ``EnvironmentConfig`` default when the config cannot be read
    or is non-positive -- Harbor would fail such a trial at load anyway, so the
    fallback only needs to keep the sizing arithmetic safe.
    """
    config_path = task_path / "task.toml"
    try:
        task_config = HarborTaskConfig.model_validate_toml(config_path.read_text())
        base = float(task_config.environment.build_timeout_sec)
    except Exception:
        return _ENV_BUILD_TIMEOUT_BASE_SEC
    return base if base > 0 else _ENV_BUILD_TIMEOUT_BASE_SEC


def _task_has_dynamic_restricted_agent_phase(task_path: Path) -> bool:
    """True when env starts public but the agent phase is restricted.

    That is the swe-marathon closed-internet shape (public setup → allowlist /
    no-network agent → no-network verifier) that needs run-specific model hosts
    and web-tool disable applied transparently.
    """
    try:
        task_config = HarborTaskConfig.model_validate_toml(
            (task_path / "task.toml").read_text()
        )
    except Exception:
        return False

    baseline = task_config.environment.resolve_baseline()
    if baseline.network_mode != NetworkMode.PUBLIC:
        return False

    task_policy = task_config.agent.explicit_phase_policy()
    effective_policies = []
    for step in task_config.steps or [None]:
        step_policy = step.agent.explicit_phase_policy() if step is not None else None
        effective_policies.append(step_policy or task_policy or baseline)
    return any(
        policy.network_mode != NetworkMode.PUBLIC for policy in effective_policies
    )


# Oddish's restricted-Compose classification MUST mirror Harbor's own Compose
# detection so the two never disagree about whether a task runs as Compose.
# Harbor's trial-level check keys exclusively on
# ``environment/docker-compose.yaml`` (harbor/trial/trial.py), and the backends
# Oddish targets (Daytona, Modal) stage the task's Compose only under that name
# and pass an explicit ``-f docker-compose.yaml`` (e.g.
# environments/daytona/environment.py), so Docker Compose never auto-discovers
# ``docker-compose.yml`` / ``compose.yaml``. Recognising more names here would
# classify a task as Compose that Harbor runs single-container -- diverging from
# (not hardening) the egress boundary. Broader Compose-filename support must land
# in Harbor first, then be mirrored here.
_HARBOR_COMPOSE_FILENAME = "docker-compose.yaml"


def _daytona_compose_restriction_kind(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
) -> str:
    """Classify the one provider/shape this bridge is allowed to change."""
    if environment_config.import_path is not None:
        return "none"
    if environment_config.type != EnvironmentType.DAYTONA:
        return "none"

    environment_dir = task_path / "environment"
    uses_compose = (environment_dir / _HARBOR_COMPOSE_FILENAME).exists() or bool(
        environment_config.extra_docker_compose
    )
    if not uses_compose:
        return "none"
    if kube_chart_present(environment_dir, environment_config.kwargs):
        return "none"

    try:
        task_config = HarborTaskConfig.model_validate_toml(
            (task_path / "task.toml").read_text()
        )
    except Exception as exc:
        # Fail closed. We have already established this is a Daytona Compose,
        # non-kube task, so a task.toml we cannot parse may still declare a
        # restricted agent phase. Returning "none" here would silently disable
        # caller-route rejection, capability attestation, runtime-only host
        # injection, and transport redaction for a trial Harbor may still run
        # restricted. Refuse the trial instead of running it unprotected.
        raise RestrictedNetworkProfileError(
            "Cannot classify a Daytona Compose trial's restricted-agent network "
            f"policy: task.toml is unreadable ({type(exc).__name__}). Refusing to "
            "run with egress controls disabled."
        ) from exc

    baseline = task_config.environment.resolve_baseline()
    task_policy = task_config.agent.explicit_phase_policy()
    effective_policies = []
    for step in task_config.steps or [None]:
        step_policy = step.agent.explicit_phase_policy() if step is not None else None
        effective_policies.append(step_policy or task_policy or baseline)
    if not any(
        policy.network_mode != NetworkMode.PUBLIC for policy in effective_policies
    ):
        return "none"
    if baseline.network_mode == NetworkMode.PUBLIC:
        return "dynamic"
    return "static"


def _supports_auto_restricted_agent_network(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
) -> bool:
    """Whether the existing single-container phase bridge applies."""
    if environment_config.import_path is not None:
        return False
    if environment_config.type not in (EnvironmentType.DAYTONA, EnvironmentType.MODAL):
        return False

    environment_dir = task_path / "environment"
    if (environment_dir / _HARBOR_COMPOSE_FILENAME).exists():
        return False
    if environment_config.extra_docker_compose:
        return False
    if kube_chart_present(environment_dir, environment_config.kwargs):
        return False
    return _task_has_dynamic_restricted_agent_phase(task_path)


def _supports_daytona_compose_restricted_agent_network(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
) -> bool:
    """Whether the Daytona Compose/DinD bridge owned by this PR applies."""
    return (
        _daytona_compose_restriction_kind(
            task_path=task_path,
            environment_config=environment_config,
        )
        == "dynamic"
    )


def _apply_restricted_agent_web_tool_defaults(
    agent_config: HarborAgentConfig,
) -> None:
    """Preserve the existing web-tool defaults outside Compose."""
    from oddish.cli.closed_internet import web_tool_kwargs_for_agent

    defaults = web_tool_kwargs_for_agent(
        agent_name=agent_config.name,
        import_path=agent_config.import_path,
    )
    if not defaults:
        return
    kwargs = dict(agent_config.kwargs or {})
    for key, value in defaults.items():
        kwargs.setdefault(key, value)
    agent_config.kwargs = kwargs


def _inject_restricted_agent_model_hosts(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
    agent_config: HarborAgentConfig,
) -> None:
    """Preserve the existing single-container model-host injection."""
    if not _supports_auto_restricted_agent_network(
        task_path=task_path,
        environment_config=environment_config,
    ):
        return

    agent_kwargs = dict(agent_config.kwargs or {})
    resolved_env = resolve_env_vars(agent_config.env) if agent_config.env else {}
    if resolved_env:
        agent_kwargs["extra_env"] = resolved_env
    inferred_hosts = normalize_allowed_hosts(
        [
            *outbound_hosts_for_model(
                agent_config.model_name,
                agent_env=resolved_env,
                agent_kwargs=agent_kwargs,
            ),
            # An agent that fronts its own service dials a host the model id
            # does not name; without this the allowlist holds only the model
            # API and the harness cannot reach its own endpoint.
            *agent_runtime_hosts(
                agent_name=agent_config.name,
                import_path=agent_config.import_path,
                agent_kwargs=agent_kwargs,
                agent_env=resolved_env,
            ),
        ]
    )
    agent_config.extra_allowed_hosts = list(
        dict.fromkeys([*agent_config.extra_allowed_hosts, *inferred_hosts])
    )


def _apply_daytona_compose_restricted_network_profile(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
    agent_config: HarborAgentConfig,
    runtime_transport_env: dict[str, str] | None = None,
    worker_minted_env: Mapping[str, str] | None = None,
) -> RestrictedNetworkProfile | None:
    """Apply the class capability contract only to Daytona Compose/DinD."""
    if not _supports_daytona_compose_restricted_agent_network(
        task_path=task_path,
        environment_config=environment_config,
    ):
        return None

    # These wrappers exist solely to enforce restricted-Compose capabilities.
    # Public and non-Compose trials retain the stock Harbor agent classes.
    _apply_gemini_cli_oddish_wrapper(agent_config)
    _apply_cursor_cli_oddish_wrapper(agent_config)
    # Drop non-consumed routes only once the EFFECTIVE class is final. The
    # wrapper above swaps stock ``GeminiCli`` -- which is absent from the
    # compatibility registry, so consumption resolves to ``None`` and the drop
    # is a no-op -- for ``OddishGeminiCli``. Filtering before the swap would
    # therefore leave worker-minted routes in the agent env and fail closed
    # here on routes Gemini does not consume, the exact failure the drop exists
    # to prevent.
    _drop_nonconsumed_agent_transport_routes(agent_config, worker_minted_env)
    resolved_env = _resolved_agent_profile_env(agent_config)
    resolved_env.update(
        _resolved_runtime_transport_env(
            runtime_transport_env,
            agent_config=agent_config,
        )
    )
    return apply_restricted_network_profile(
        agent_config=agent_config,
        resolved_env=resolved_env,
        runtime_only_hosts=True,
    )


def _apply_restricted_agent_network_defaults(
    *,
    task_path: Path,
    environment_config: HarborEnvironmentConfig,
    agent_config: HarborAgentConfig,
    runtime_transport_env: dict[str, str] | None = None,
    worker_minted_env: Mapping[str, str] | None = None,
) -> RestrictedNetworkProfile | None:
    """Apply the Compose bridge without changing existing phase behavior."""
    profile = _apply_daytona_compose_restricted_network_profile(
        task_path=task_path,
        environment_config=environment_config,
        agent_config=agent_config,
        runtime_transport_env=runtime_transport_env,
        worker_minted_env=worker_minted_env,
    )
    if profile is not None:
        return profile

    if not _supports_auto_restricted_agent_network(
        task_path=task_path,
        environment_config=environment_config,
    ):
        return None
    _inject_restricted_agent_model_hosts(
        task_path=task_path,
        environment_config=environment_config,
        agent_config=agent_config,
    )
    _apply_restricted_agent_web_tool_defaults(agent_config)
    return None


def _claude_code_environment_hosts(agent_config: HarborAgentConfig) -> list[str]:
    """Hosts the claude-code CLI needs across install *and* run.

    Harbor derives the agent-phase allowlist from the provider prefix on
    ``model_name``, but force-direct-API routing strips that prefix to the bare
    Anthropic id the CLI requires -- leaving Harbor nothing to resolve, so a
    closed-internet trial reaches the installer CDN and then dies on ECONNRESET
    at its first API call. Resolve the model endpoint here instead.
    """
    return [
        *_CLAUDE_CODE_INSTALLER_HOSTS,
        *outbound_hosts_for_model(agent_config.model_name, agent_env=agent_config.env),
    ]


def _read_query_cli_text() -> str:
    """Thin wrapper so tests can monkeypatch without reaching into probe_staging."""
    from oddish.worker.probe_staging import read_query_cli_text

    return read_query_cli_text()


def _probe_modal_kwargs(is_probe: bool, environment: EnvironmentType) -> dict[str, Any]:
    """Return extra env_config.kwargs to inject when running a probe on Modal.

    Passes the oddish-query CLI source to the Modal image so the harbor fork
    can bake it in at instantiation. Returns empty dict for non-probe or
    non-Modal trials so the caller can unconditionally merge.
    """
    if not is_probe or environment != EnvironmentType.MODAL:
        return {}
    return {
        "probe_cli_content": _read_query_cli_text(),
        "probe_cli_path": "/probe-harness/oddish-query",
    }


def _check_local_storage_preflight(
    jobs_dir: Path,
    *,
    include_temp_root: bool,
    min_required_gb: float = _MIN_REQUIRED_FREE_GB,
    min_required_inodes: int = _MIN_REQUIRED_FREE_INODES,
) -> str | None:
    """Return a user-facing error when Harbor scratch space is not viable.

    Kept as a local wrapper so tests and callers that monkeypatch
    ``harbor_runner._probe_storage_root`` still affect this facade.
    """
    for root in _storage_probe_paths(jobs_dir, include_temp_root=include_temp_root):
        try:
            error = _probe_storage_root(
                root,
                min_required_gb=min_required_gb,
                min_required_inodes=min_required_inodes,
            )
        except OSError as exc:
            return (
                f"Local storage preflight failed at {root}: {type(exc).__name__}: {exc}"
            )
        if error is not None:
            return error
    return None


def _patch_task_toml(task_dir: Path, hc: HarborConfig) -> None:
    """Patch task.toml with ``docker_image`` and ``mcp_servers`` from *hc*.

    These fields are read by Harbor from the task's task.toml rather than
    the job/trial config, so we patch the file before execution.
    """
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        return

    try:
        task_config = HarborTaskConfig.model_validate_toml(config_path.read_text())
    except Exception:
        return

    changed = False

    if hc.docker_image:
        task_config.environment.docker_image = str(hc.docker_image)
        changed = True

    if hc.mcp_servers:
        task_config.environment.mcp_servers = [
            (
                MCPServerConfig.model_validate(s.model_dump())
                if not isinstance(s, MCPServerConfig)
                else s
            )
            for s in hc.mcp_servers
        ]
        changed = True

    if changed:
        config_path.write_text(task_config.model_dump_toml())


def _assert_tpu_backend(environment, backend, override_tpu) -> None:
    """Fail fast when a TPU-requesting trial is routed to a TPU-less backend.

    Without this, the trial runs WITHOUT the accelerator and dies minutes later
    on its own device asserts -- a confusing failure that looks like a workload
    bug. Only the override_tpu channel is visible here; a TPU declared solely
    in task.toml is parsed by Harbor after this point.
    """
    if override_tpu is None:
        return
    if backend is not None and backend.capabilities().tpu is not None:
        return
    raise RuntimeError(
        f"TPU trials must run on the GKE backend: this trial requests a TPU "
        f"({override_tpu.type}) but is routed to environment "
        f"'{environment.value}', which has no TPU support. Resubmit with "
        f"environment=gke ('oddish run' auto-routes TPU tasks)."
    )


async def run_harbor_trial_async(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    model: str | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
    hook_callback: HookCallback | None = None,
    trial_id: str | None = None,
    harbor_config: dict[str, Any] | None = None,
    org_id: str | None = None,
    extra_agent_env: dict[str, str] | None = None,
) -> HarborOutcome:
    """
    Execute a Harbor trial using Harbor's Python API with lifecycle hooks.

    ``extra_agent_env`` is merged into the built AgentConfig env (probe trials
    use this for the minted read-only oddish CLI creds). It is never persisted.

    Returns a HarborOutcome with reward, error, tokens, cost, timing,
    trajectory presence, and artifact paths.
    """
    apply_harbor_patches()

    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)

    # Size the environment-build timeout multiplier BEFORE the dispatch fork so
    # EVERY path that runs a GKE environment carries it -- the in-process blessed
    # variant AND the out-of-process ephemeral child. pod_ready is read from the
    # raw submission kwargs (the same override channel the GKE backend merges
    # under its platform default), so it matches the value the Pod is actually
    # given; the task's own build_timeout_sec is the base Harbor multiplies. This
    # is a no-op (returns the caller's value, possibly None) off GKE.
    env_build_multiplier = _sized_environment_build_timeout_multiplier(
        environment=environment,
        environment_build_timeout_multiplier=hc.environment_build_timeout_multiplier,
        timeout_multiplier=hc.timeout_multiplier,
        pod_ready_timeout_sec=_effective_pod_ready_timeout_sec(
            hc.environment.kwargs, settings.gke_pod_ready_timeout_sec
        ),
        base_sec=_effective_task_build_timeout_sec(task_path),
    )

    # The TPU gate runs BEFORE the ephemeral early-return so BOTH engines get
    # the fast-fail: an out-of-process trial with override_tpu on a TPU-less
    # backend would otherwise skip it entirely.
    _assert_tpu_backend(
        environment,
        get_backend(environment.value),
        getattr(hc.environment, "override_tpu", None),
    )

    dispatch_env_config = hc.environment.model_copy()
    dispatch_env_config.type = environment
    try:
        restricted_compose_kind = _daytona_compose_restriction_kind(
            task_path=task_path,
            environment_config=dispatch_env_config,
        )
    except RestrictedNetworkProfileError as exc:
        # Classifier failed closed (see _daytona_compose_restriction_kind). This
        # call is before the main try/except, so surface a well-formed outcome
        # rather than letting the trial crash the worker.
        return HarborOutcome(
            reward=None,
            error=str(exc),
            exit_code=-1,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
            exception_type="RestrictedNetworkProfileError",
        )

    # An allowlisted override that is neither the locked default nor a blessed
    # image variant runs out-of-process against its own Harbor: a different
    # Harbor than the one baked into this container cannot be swapped in-process
    # (sys.modules caches it), so route to the child-interpreter engine.
    if hc.variant_id == "ephemeral":
        if restricted_compose_kind != "none":
            return HarborOutcome(
                reward=None,
                error=(
                    "Restricted Daytona Docker Compose trials require Oddish's "
                    "capability-attested Harbor runtime; ephemeral Harbor variants "
                    "are not supported."
                ),
                exit_code=-1,
                duration_sec=0.0,
                job_result_path=None,
                job_dir=None,
                exception_type="RestrictedNetworkProfileError",
            )
        from .ephemeral import run_ephemeral_harbor_trial

        return await run_ephemeral_harbor_trial(
            task_path=task_path,
            agent=agent,
            jobs_dir=jobs_dir,
            model=model,
            environment=environment,
            hook_callback=hook_callback,
            trial_id=trial_id,
            harbor_config=harbor_config,
            org_id=org_id,
            extra_agent_env=extra_agent_env,
            environment_build_timeout_multiplier=env_build_multiplier,
        )

    # Probes attach to an existing task and inherit its task.toml, which may
    # predate the timeout requirement. Rather than hard-fail, skip strict
    # validation and hand the probe a capped default agent timeout below.
    is_probe = raw.get("mode") == "probe"
    if not is_probe:
        validate_task_timeout_config(task_path)

    needs_task_patch = bool(hc.docker_image or hc.mcp_servers)
    preflight_error = _check_local_storage_preflight(
        jobs_dir,
        include_temp_root=needs_task_patch,
    )
    if preflight_error is not None:
        return HarborOutcome(
            reward=None,
            error=preflight_error,
            exit_code=-1,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
            exception_type="LocalStoragePreflightError",
        )

    unique_suffix = trial_id if trial_id else uuid.uuid4().hex[:8]
    unique_parent = jobs_dir / f"{task_path.name}.{agent}.{unique_suffix}"
    unique_parent.mkdir(parents=True, exist_ok=True)

    task_tmpdir: tempfile.TemporaryDirectory | None = None
    effective_task_path = task_path

    if needs_task_patch:
        task_tmpdir = tempfile.TemporaryDirectory(prefix="oddish-task-")
        patched_task = Path(task_tmpdir.name) / task_path.name
        shutil.copytree(task_path, patched_task)
        _patch_task_toml(patched_task, hc)
        effective_task_path = patched_task

    actual_job_dir = unique_parent
    start = time.time()
    modal_debug_log_path: Path | None = None
    runtime_transport_replacements: dict[str, str] = {}
    # Worker-private Azure deployment id when the model was swapped to its public
    # identity (set below); used to keep the deployment->public substitution as
    # the final word across later redaction passes.
    runtime_deployment_model: str | None = None

    try:
        # Build Harbor configs inside the try: model normalization and
        # Job.create can both fail and should return a well-formed outcome.
        # Caller-supplied routes are rejected for every restricted Compose
        # shape -- static (nop/oracle) trials must not widen egress either.
        if restricted_compose_kind in ("dynamic", "static"):
            reject_submitted_restricted_routes(raw)
        env_config = hc.environment.model_copy()
        env_config.type = environment

        backend = get_backend(environment.value)
        if backend is not None:
            env_config.kwargs = backend.harbor_env_kwargs(env_config.kwargs)
        probe_modal = _probe_modal_kwargs(is_probe, environment)
        if probe_modal:
            env_config.kwargs = {**probe_modal, **env_config.kwargs}
        uses_openai_provider = _trial_uses_openai_provider(
            agent=agent,
            model=model,
            raw_harbor_config=raw,
        )
        _, openai_model = _trial_requested_model(
            agent=agent,
            model=model,
            raw_harbor_config=raw,
        )
        openai_env = (
            settings.get_openai_agent_env(model=openai_model)
            if uses_openai_provider
            else {}
        )
        runtime_transport_env = _resolved_runtime_transport_env(openai_env)
        if restricted_compose_kind == "dynamic":
            runtime_transport_replacements = _runtime_transport_redactions(
                runtime_transport_env
            )
        # A BYOK user key arrives in the agent env, but claude-code's
        # direct-vs-Bedrock routing reads os.environ -- both to pick the model
        # id (in _build_agent_config) and to blank Bedrock creds (below). Surface
        # the user key as ambient for the build + run so the trial routes to the
        # direct Anthropic API even on a worker with no platform key; the agent
        # then authenticates with the user's key.
        #
        # ``anthropic-hdo/<model>`` is the same idea with the platform
        # ANTHROPIC_HDO_API_KEY: overwrite ambient ANTHROPIC_API_KEY so routing
        # and auth both use the HDO credential instead of Bedrock / the default
        # Anthropic key. HDO wins over BYOK when the model prefix opts in.
        # ``anthropic/<model>`` does the same with the platform
        # ANTHROPIC_API_KEY so an explicit platform trial routes direct even on
        # a Bedrock-equipped worker.
        byok_anthropic_env: dict[str, str] = {}
        if is_anthropic_hdo_model(model):
            byok_anthropic_env["ANTHROPIC_API_KEY"] = _resolve_anthropic_hdo_api_key()
        elif is_anthropic_platform_model(model):
            byok_anthropic_env["ANTHROPIC_API_KEY"] = (
                _resolve_anthropic_platform_api_key()
            )
        elif "claude-code" in (agent or "").strip().lower():
            _byok_key = (extra_agent_env or {}).get("ANTHROPIC_API_KEY")
            if _byok_key:
                byok_anthropic_env["ANTHROPIC_API_KEY"] = _byok_key

        with _temporary_env(byok_anthropic_env):
            agent_config = _build_agent_config(
                agent=agent,
                model=model,
                raw_harbor_config=raw,
                is_probe=is_probe,
                probe_oddish_env=extra_agent_env,
            )
            # Early no-serialized-routes checkpoint, symmetric with the
            # post-defaults assert below; both restricted kinds are covered.
            if restricted_compose_kind in ("dynamic", "static"):
                assert_no_serialized_restricted_routes(agent_config)

            if (
                restricted_compose_kind == "dynamic"
                and uses_openai_provider
                and not agent_keeps_public_model_identity(agent_config)
                and settings.get_openai_route_for_model(openai_model)
                == OPENAI_PROVIDER_AZURE
                and agent_config.model_name
                and openai_model
            ):
                # The provider deployment is needed by the running agent but is
                # worker-private. Keep the submitted model in all serialized
                # Harbor configs/results and hand the deployment to AgentFactory
                # through a non-Pydantic runtime attribute. This mirrors the gate
                # on _build_agent_config's deployment rewrite (its source): an
                # agent that fronts models through its own service (e.g. Cursor)
                # never had its model rewritten to the deployment, so it is
                # excluded here too and keeps the public model identity.
                set_runtime_model_name(agent_config, agent_config.model_name)
                runtime_deployment_model = agent_config.model_name
                runtime_transport_replacements.update(
                    _runtime_transport_redactions(
                        openai_env,
                        runtime_model=agent_config.model_name,
                        public_model=openai_model,
                    )
                )
                agent_config.model_name = openai_model

            # Resolve the worker transport env AFTER any deployment->public model
            # swap so consumption/host selection is computed against the public
            # model, never the private (bare) Azure deployment id. Running it
            # before the swap would derive an empty consumed-key set for a
            # mini-swe Azure/bare-openai trial and wrongly drop OPENAI_BASE_URL
            # before host selection, leaving the agent phase with no model egress.
            if restricted_compose_kind == "dynamic":
                runtime_transport_env = _resolved_runtime_transport_env(
                    openai_env,
                    agent_config=agent_config,
                )
            if restricted_compose_kind == "static" and not (
                is_static_restricted_agent_supported(agent_config)
            ):
                raise RestrictedNetworkProfileError(
                    "Model-backed Daytona Docker Compose trials cannot start with "
                    "a restricted environment baseline because agent installation "
                    "needs the public setup phase. Use a public environment baseline "
                    "and a restricted [agent] phase."
                )
            if restricted_compose_kind == "dynamic":
                # Include the selected agent's fully resolved route and
                # credential values before Job.create can emit a lifecycle
                # event or a live tail can persist output. Values remain
                # trial-scoped and are never attached to Harbor config models.
                resolved_agent_env = _resolved_agent_profile_env(agent_config)
                runtime_transport_replacements.update(
                    _runtime_transport_redactions(
                        {
                            **resolved_agent_env,
                            **runtime_transport_env,
                        },
                        # Re-assert the deployment->public substitution so this
                        # full-env pass (which re-redacts AZURE_OPENAI_DEPLOYMENT
                        # to [REDACTED]) does not clobber it; agent_config.model_name
                        # is now the public model after the swap above.
                        runtime_model=runtime_deployment_model,
                        public_model=agent_config.model_name,
                    )
                )
            _apply_restricted_agent_network_defaults(
                task_path=effective_task_path,
                environment_config=env_config,
                agent_config=agent_config,
                runtime_transport_env=runtime_transport_env,
                # Routes the WORKER minted for this trial: the provider env it
                # resolved, plus any job-scoped token env it injected into the
                # agent config. Only these may be dropped as non-consumed; any
                # other transport key keeps failing closed.
                worker_minted_env={
                    **(openai_env or {}),
                    **(extra_agent_env or {}),
                },
            )
            # Neither restricted kind serializes extra_allowed_hosts: the
            # dynamic Compose profile grants hosts via the runtime-only
            # attribute (runtime_only_hosts=True), and static (nop/oracle)
            # trials skip the profile entirely and inject no routes.
            if restricted_compose_kind in ("dynamic", "static"):
                assert_no_serialized_restricted_routes(agent_config)

        # Claude Code downloads its CLI at agent-setup and calls its model
        # endpoint during agent.run(). On closed-internet tasks, installer CDN
        # hosts and custom model routes are not always in the task allowlist, so
        # allow both via the environment baseline (which spans install + run).
        # Model API hosts are also injected automatically for restricted agent
        # phases via _apply_restricted_agent_network_defaults.
        #
        # This preserves the existing setup lifecycle for every non-Compose
        # shape, and is independent from the class-profile boundary that owns
        # the restricted Daytona Compose agent phase -- which is why that shape
        # is excluded here rather than having both paths widen the baseline.
        if "claude-code" in (agent or "").strip().lower() and not (
            _supports_daytona_compose_restricted_agent_network(
                task_path=effective_task_path,
                environment_config=env_config,
            )
        ):
            hosts = _claude_code_environment_hosts(agent_config)
            env_config.extra_allowed_hosts = [
                *env_config.extra_allowed_hosts,
                *[h for h in hosts if h not in env_config.extra_allowed_hosts],
            ]

        # Stage the org's shared skills (+ global seeds) into a root under the
        # job dir and hand it to Harbor via ``AgentConfig.skills``. Best-effort;
        # failure never blocks a trial run.
        if org_id is not None:
            skills_root = unique_parent / "agent_skills"
            skill_ids = raw.get("skill_ids")
            n_skills = await stage_org_skills(
                skills_root, org_id=org_id, skill_ids=skill_ids
            )
            if n_skills:
                agent_config.skills = [*agent_config.skills, skills_root]

        job_config_kwargs: dict[str, Any] = {
            "tasks": [TaskConfig(path=effective_task_path)],
            "agents": [agent_config],
            "environment": env_config,
            "verifier": hc.verifier,
            "artifacts": hc.artifacts,
            "jobs_dir": unique_parent,
        }
        if hc.timeout_multiplier is not None:
            job_config_kwargs["timeout_multiplier"] = hc.timeout_multiplier
        if hc.agent_timeout_multiplier is not None:
            job_config_kwargs["agent_timeout_multiplier"] = hc.agent_timeout_multiplier
        if hc.verifier_timeout_multiplier is not None:
            job_config_kwargs["verifier_timeout_multiplier"] = (
                hc.verifier_timeout_multiplier
            )
        if hc.agent_setup_timeout_multiplier is not None:
            job_config_kwargs["agent_setup_timeout_multiplier"] = (
                hc.agent_setup_timeout_multiplier
            )
        # Reuse the multiplier sized before the dispatch fork (identical inputs:
        # pod_ready from the same submission kwargs, base from the same task).
        if env_build_multiplier is not None:
            job_config_kwargs["environment_build_timeout_multiplier"] = (
                env_build_multiplier
            )
        if hc.retry is not None:
            job_config_kwargs["retry"] = hc.retry

        config = JobConfig(**job_config_kwargs)

        runtime_env = dict(openai_env)
        # Keep the BYOK key ambient for Job.create/run too, so Harbor's own
        # os.environ-based Bedrock-mode check agrees with the direct model id.
        runtime_env.update(byok_anthropic_env)
        is_claude_code = "claude-code" in (agent or "").strip().lower()
        if is_claude_code and (
            byok_anthropic_env or _claude_code_forces_direct_api(is_probe)
        ):
            # Harbor's _is_bedrock_mode() reads os.environ, and the Modal image
            # bakes in Bedrock credentials. Blank them when claude-code runs
            # against the direct Anthropic API -- platform force-direct, or a
            # BYOK user key.
            runtime_env.update({var: "" for var in BEDROCK_ENV_VARS})
        with _temporary_env(runtime_env):
            job = await Job.create(config)
            actual_job_dir = job.job_dir

            safe_hook_callback = _redacting_hook_callback(
                hook_callback, runtime_transport_replacements
            )
            redaction_trial_id = (
                trial_id if restricted_compose_kind == "dynamic" and trial_id else None
            )
            if redaction_trial_id:
                # Import lazily: live_tail imports the queue package, whose
                # trial handler imports this runner.
                from . import live_tail as harbor_live_tail

                harbor_live_tail.configure_runtime_redactions(
                    redaction_trial_id, runtime_transport_replacements
                )
            try:
                if safe_hook_callback:
                    job.on_trial_started(safe_hook_callback)
                    job.on_environment_started(safe_hook_callback)
                    job.on_agent_started(safe_hook_callback)
                    job.on_agent_ended(safe_hook_callback)
                    job.on_verification_started(safe_hook_callback)
                    job.on_trial_ended(safe_hook_callback)
                    job.on_trial_cancelled(safe_hook_callback)

                with _capture_modal_output(
                    actual_job_dir, environment
                ) as captured_log_path:
                    modal_debug_log_path = captured_log_path
                    job_result = await job.run()
            finally:
                if redaction_trial_id:
                    harbor_live_tail.clear_runtime_redactions(redaction_trial_id)
        if restricted_compose_kind == "dynamic":
            _scrub_runtime_transport_files(
                actual_job_dir,
                runtime_transport_replacements,
            )
        duration = time.time() - start

        job_dir = job.job_dir
        job_result_path = job_dir / "result.json"

        if not job_result_path.exists():
            return HarborOutcome(
                reward=None,
                error="Job result.json not found",
                exit_code=0,
                duration_sec=duration,
                job_result_path=None,
                job_dir=job_dir,
                exception_type="JobResultMissingError",
            )

        outcome = _extract_outcome_from_job_result(
            job_result=job_result,
            job_result_path=job_result_path,
            job_dir=job_dir,
            duration_sec=duration,
        )
        if outcome.error:
            outcome = replace(
                outcome,
                error=redact_exact_text(
                    _maybe_add_modal_debug_hint(outcome.error, modal_debug_log_path),
                    runtime_transport_replacements,
                ),
            )
        return outcome

    except asyncio.CancelledError:
        duration = time.time() - start
        error_message = (
            "Harbor trial cancelled by the runtime. This usually means the worker "
            "was restarted or the sandbox failed during startup. Check worker logs."
        )
        error_message = redact_exact_text(
            _maybe_add_modal_debug_hint(error_message, modal_debug_log_path),
            runtime_transport_replacements,
        )
        if restricted_compose_kind == "dynamic":
            _scrub_runtime_transport_files(
                actual_job_dir,
                runtime_transport_replacements,
            )
        debug_result_path = _write_debug_result_json(
            job_dir=actual_job_dir,
            duration_sec=duration,
            exception_type="CancelledError",
            exception_message=error_message,
            debug_log_path=modal_debug_log_path,
        )
        return HarborOutcome(
            reward=None,
            error=error_message,
            exit_code=-1,
            duration_sec=duration,
            job_result_path=debug_result_path,
            job_dir=actual_job_dir,
            exception_type="CancelledError",
        )
    except Exception as e:
        duration = time.time() - start
        error_message = f"Harbor job execution failed: {_format_exception_message(e)}"
        error_message = redact_exact_text(
            _maybe_add_modal_debug_hint(error_message, modal_debug_log_path),
            runtime_transport_replacements,
        )
        if restricted_compose_kind == "dynamic":
            _scrub_runtime_transport_files(
                actual_job_dir,
                runtime_transport_replacements,
            )
        debug_result_path = _write_debug_result_json(
            job_dir=actual_job_dir,
            duration_sec=duration,
            exception_type=type(e).__name__,
            exception_message=error_message,
            debug_log_path=modal_debug_log_path,
        )
        return HarborOutcome(
            reward=None,
            error=error_message,
            exit_code=-1,
            duration_sec=duration,
            job_result_path=debug_result_path,
            job_dir=actual_job_dir,
            exception_type=type(e).__name__,
        )
    finally:
        if task_tmpdir is not None:
            task_tmpdir.cleanup()


__all__ = [
    "HarborOutcome",
    "log_local_storage_snapshot",
    "run_harbor_trial_async",
]
