"""Fail-closed runtime capabilities for restricted agent phases.

The network-policy orchestrator must not guess from a submitted agent or model
name.  It resolves the *effective* class that Harbor will instantiate and asks
that class for one complete capability: model transport plus the settings that
disable server-side web tools.  Custom agents can participate by implementing
``restricted_network_profile`` with the structural signature documented by
``RestrictedNetworkProfileProvider`` below.

The compatibility adapters in this module cover Oddish's current stock
operational agents while the same hook is adopted upstream.  They are keyed by
the exact resolved Python class, never by inheritance, trial-facing aliases, or
model-name branches in the runner.  An unrecognised class is rejected before
``Job.create`` whenever the task requests a restricted agent phase; public
tasks do not consult this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from harbor.agents.factory import AgentFactory
from harbor.agents.installed.acp_registry import is_acp_registry_shorthand
from harbor.models.agent.name import AgentName
from harbor.models.task.config import normalize_allowed_hosts
from harbor.models.trial.config import AgentConfig
from harbor.utils.import_path import import_class

from oddish.config import infer_model_provider_prefix
from oddish.workers.agents.network import normalize_domain_or_url

from .model_hosts import (
    _ANTHROPIC_HOSTS as _ANTHROPIC_RUNTIME_HOSTS,
    _CURSOR_RUNTIME_HOSTS,
    _GEMINI_HOSTS as _GEMINI_RUNTIME_HOSTS,
    _OPENAI_HOSTS as _OPENAI_RUNTIME_HOSTS,
    _XAI_HOSTS as _XAI_RUNTIME_HOSTS,
    AZURE_BASE_URL_KEYS as _AZURE_BASE_URL_KEYS,
    CURSOR_BASE_URL_KEYS as _CURSOR_BASE_URL_KEYS,
    GEMINI_BASE_URL_KEYS as _GEMINI_BASE_URL_KEYS,
    GEMINI_OAUTH_ENV_KEYS as _GEMINI_OAUTH_ENV_KEYS,
    KNOWN_TRANSPORT_BASE_URL_KEYS as _KNOWN_TRANSPORT_BASE_URL_KEYS,
    OPENAI_BASE_URL_KEYS as _STOCK_OPENAI_BASE_URL_KEYS,
    outbound_hosts_for_model,
)


class RestrictedNetworkProfileError(ValueError):
    """The effective agent cannot safely run inside a restricted phase."""


@dataclass(frozen=True)
class RestrictedNetworkProfile:
    """Complete runtime contract for one effective agent implementation.

    ``outbound_hosts`` is the exact union required by the harness and its model
    transport.  A deliberately transport-free implementation declares an
    empty tuple.  Security overrides are force-applied for restricted trials;
    a submitted config cannot turn server-side web tools back on.

    """

    outbound_hosts: tuple[str, ...] = ()
    env_overrides: Mapping[str, str] = field(default_factory=dict)
    kwarg_overrides: Mapping[str, Any] = field(default_factory=dict)
    server_web_disabled: bool = False


@runtime_checkable
class RestrictedNetworkProfileProvider(Protocol):
    """Structural hook available to any custom Harbor agent class.

    The return value may be ``RestrictedNetworkProfile`` or a mapping with the
    same field names.  Returning ``None`` (or omitting the hook) is an explicit
    inability to satisfy the boundary and therefore fails closed.
    """

    @classmethod
    def restricted_network_profile(
        cls,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        kwargs: Mapping[str, Any],
    ) -> RestrictedNetworkProfile | Mapping[str, Any] | None: ...


# Both the runtime allowlist host tuples AND the transport base-URL key groups
# (``_KNOWN_TRANSPORT_BASE_URL_KEYS`` / ``_STOCK_OPENAI_BASE_URL_KEYS`` /
# ``_AZURE_BASE_URL_KEYS`` / ``_GEMINI_BASE_URL_KEYS`` /
# ``_CURSOR_BASE_URL_KEYS``) are the single source in model_hosts and imported
# above (aliased). Filtering here and host discovery there therefore read the
# same keys and cannot drift. Only the non-host web-tool constant lives here.
_CLAUDE_WEB_TOOLS = "WebSearch WebFetch"

# The OpenAI-family transport an agent actually consumes. Oddish routes
# OpenAI-family jobs through Azure OpenAI by default, and
# ``get_openai_agent_env`` emits the Azure aliases for the same endpoint next to
# ``OPENAI_BASE_URL`` -- so an OpenAI-provider agent consumes all of them. They
# must be listed here as well as in the fail-closed known set: a key that is
# known but not consumed trips the "does not consume" guard, which would reject
# every Azure-routed restricted-Compose trial.
_OPENAI_BASE_URL_KEYS = (*_STOCK_OPENAI_BASE_URL_KEYS, *_AZURE_BASE_URL_KEYS)

# These are deliberately ordinary underscore attributes rather than Pydantic
# fields. Harbor receives the values in memory, while JobConfig/TrialConfig
# serialization ignores them. This keeps worker-only routes out of config.json,
# result.json, lock files, and uploaded artifacts.
RUNTIME_ALLOWED_HOSTS_ATTR = "_oddish_runtime_allowed_hosts"
RUNTIME_MODEL_NAME_ATTR = "_oddish_runtime_model_name"

# The transport base URLs a safe profile may carry are exactly the known
# transport keys; the extras are Gemini's non-route OAuth toggles, which select
# credentials rather than widen egress. Both groups are single-sourced in
# model_hosts (imported above), so this allowlist cannot drift from the runner's
# Gemini fold or the host boundary.
_SAFE_PROFILE_ENV_KEYS = _KNOWN_TRANSPORT_BASE_URL_KEYS | frozenset(
    _GEMINI_OAUTH_ENV_KEYS
)

_CURSOR_ENV_OVERRIDES: dict[str, str] = {
    "CURSOR_FORCED_SHELL_EGRESS": "1",
    "CURSOR_FORCED_SHELL_EGRESS_ALLOW_WEB_TOOLS": "0",
    # Harbor owns the outer process/network boundary.  Preserve normal task
    # filesystem and inner-network semantics in Cursor's nested shell sandbox.
    "CURSOR_FORCED_SHELL_EGRESS_NETWORK_DEFAULT": "allow",
    "CURSOR_FORCED_SHELL_EGRESS_WRITABLE_PATHS": "/",
}


def _class_path(agent_class: type[Any]) -> str:
    return f"{agent_class.__module__}:{agent_class.__qualname__}"


def set_runtime_allowed_hosts(
    agent_config: AgentConfig, hosts: tuple[str, ...]
) -> None:
    """Attach worker-only network routes without serializing them."""
    object.__setattr__(agent_config, RUNTIME_ALLOWED_HOSTS_ATTR, hosts)


def set_runtime_model_name(agent_config: AgentConfig, model_name: str) -> None:
    """Attach a worker-only provider deployment without serializing it."""
    object.__setattr__(agent_config, RUNTIME_MODEL_NAME_ATTR, model_name)


def reject_submitted_restricted_routes(
    raw_harbor_config: Mapping[str, Any],
) -> None:
    """Reject caller-controlled routes for a restricted Compose agent phase.

    Runtime model routes are a worker capability, not a trial input.  Inspect
    both the current ``agent_config`` payload and the legacy
    ``agent_overrides`` shape before building Harbor's effective AgentConfig.
    The error deliberately names only fixed field paths; submitted values may
    contain private endpoints or credentials and must never be reflected.
    """

    rejected_fields: list[str] = []
    # ``environment`` / ``environment_overrides`` are inspected alongside the
    # agent shapes: a caller-submitted environment allowlist (or transport base
    # URL) must not widen a restricted trial's egress either, and a legitimate
    # restricted Compose trial's environment phase never needs a caller-supplied
    # model route in the submitted config.
    for root_key in (
        "agent_config",
        "agent_overrides",
        "environment",
        "environment_overrides",
    ):
        raw_agent = raw_harbor_config.get(root_key)
        if not isinstance(raw_agent, Mapping):
            continue

        if raw_agent.get("extra_allowed_hosts"):
            rejected_fields.append(f"{root_key}.extra_allowed_hosts")

        raw_env = raw_agent.get("env")
        if isinstance(raw_env, Mapping):
            rejected_fields.extend(
                f"{root_key}.env.{key}"
                for key in sorted(
                    set(raw_env).intersection(_KNOWN_TRANSPORT_BASE_URL_KEYS)
                )
            )

        raw_kwargs = raw_agent.get("kwargs")
        raw_extra_env = (
            raw_kwargs.get("extra_env") if isinstance(raw_kwargs, Mapping) else None
        )
        if isinstance(raw_extra_env, Mapping):
            rejected_fields.extend(
                f"{root_key}.kwargs.extra_env.{key}"
                for key in sorted(
                    set(raw_extra_env).intersection(_KNOWN_TRANSPORT_BASE_URL_KEYS)
                )
            )

    if rejected_fields:
        raise RestrictedNetworkProfileError(
            "Restricted Daytona Compose agent phases do not accept "
            "caller-supplied network routes in: "
            f"{', '.join(rejected_fields)}. Routes must come from the "
            "worker-attested runtime profile."
        )


def assert_no_serialized_restricted_routes(agent_config: AgentConfig) -> None:
    """Ensure the built config cannot widen the restricted route boundary."""

    if agent_config.extra_allowed_hosts:
        raise RestrictedNetworkProfileError(
            "Restricted Daytona Compose agent config contains non-attested "
            "extra_allowed_hosts after build; refusing to create the job."
        )


def is_static_restricted_agent_supported(agent_config: AgentConfig) -> bool:
    """Static closed environments are safe only for setup-free stock agents."""
    return _class_path(resolve_effective_agent_class(agent_config)) in {
        "harbor.agents.nop:NopAgent",
        "harbor.agents.oracle:OracleAgent",
    }


def resolve_effective_agent_class(agent_config: AgentConfig) -> type[Any]:
    """Resolve exactly the class Harbor will instantiate, without constructing it."""
    import_path = agent_config.import_path
    name = agent_config.name

    # Mirror AgentFactory's unified --agent handling for custom import paths.
    if (
        import_path is None
        and name is not None
        and ":" in name
        and not is_acp_registry_shorthand(name)
    ):
        import_path, name = name, None

    if import_path is not None:
        return cast(type[Any], import_class(import_path, label="agent"))
    if name is None:
        raise RestrictedNetworkProfileError(
            "Restricted agent phase requires an agent name or import_path."
        )
    try:
        agent_name = AgentName(name)
    except ValueError as exc:
        # ACP registry shorthand and future aliases cannot be safely inferred.
        # They can opt in through a concrete import_path with the structural
        # capability hook instead of silently receiving public egress.
        raise RestrictedNetworkProfileError(
            f"Restricted agent phase cannot resolve effective agent class for {name!r}; "
            "use an import_path whose class declares restricted_network_profile()."
        ) from exc
    return cast(type[Any], AgentFactory.get_agent_class(agent_name))


def _coerce_profile(
    raw: RestrictedNetworkProfile | Mapping[str, Any] | None,
    *,
    agent_class: type[Any],
) -> RestrictedNetworkProfile:
    if raw is None:
        raise RestrictedNetworkProfileError(
            f"{_class_path(agent_class)} does not declare a safe restricted-network "
            "profile. Add restricted_network_profile() or run a public agent phase."
        )
    if isinstance(raw, RestrictedNetworkProfile):
        profile = raw
    elif isinstance(raw, Mapping):
        allowed_keys = {
            "outbound_hosts",
            "env_overrides",
            "kwarg_overrides",
            "server_web_disabled",
        }
        unexpected = set(raw) - allowed_keys
        if unexpected:
            raise RestrictedNetworkProfileError(
                f"{_class_path(agent_class)} returned unknown restricted-network "
                f"profile fields: {sorted(unexpected)}"
            )
        try:
            profile = RestrictedNetworkProfile(
                outbound_hosts=tuple(raw.get("outbound_hosts") or ()),
                env_overrides=dict(raw.get("env_overrides") or {}),
                kwarg_overrides=dict(raw.get("kwarg_overrides") or {}),
                server_web_disabled=raw.get("server_web_disabled") is True,
            )
        except (TypeError, ValueError) as exc:
            raise RestrictedNetworkProfileError(
                f"{_class_path(agent_class)} returned an invalid restricted-network profile."
            ) from exc
    else:
        raise RestrictedNetworkProfileError(
            f"{_class_path(agent_class)} returned unsupported restricted-network "
            f"profile type {type(raw).__name__}."
        )

    if not profile.server_web_disabled:
        raise RestrictedNetworkProfileError(
            f"{_class_path(agent_class)} did not attest that server-side web tools "
            "are disabled; refusing to start a restricted agent phase."
        )
    try:
        outbound_hosts = tuple(normalize_allowed_hosts(list(profile.outbound_hosts)))
    except (TypeError, ValueError) as exc:
        raise RestrictedNetworkProfileError(
            f"{_class_path(agent_class)} returned invalid outbound hosts."
        ) from exc
    return RestrictedNetworkProfile(
        outbound_hosts=outbound_hosts,
        env_overrides=dict(profile.env_overrides),
        kwarg_overrides=dict(profile.kwarg_overrides),
        server_web_disabled=True,
    )


ProfileFactory = Callable[
    [type[Any], AgentConfig, Mapping[str, str]], RestrictedNetworkProfile
]


def _selected_transport_hosts(
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
    *,
    base_url_keys: tuple[str, ...],
    default_hosts: tuple[str, ...] = (),
    infer_model: bool = True,
) -> tuple[str, ...]:
    """Resolve one restricted-Compose transport without widening by union.

    The legacy host helper intentionally unions every base URL it sees. That is
    useful for existing runners whose setup and model transports share one
    allowlist, but it is unsafe for the agent-only Compose boundary: an
    unrelated worker ``*_BASE_URL`` would become public egress. The effective
    agent adapter therefore declares exactly which aliases it consumes. Any
    other known route, or two conflicting aliases for the selected route,
    fails closed before Harbor constructs the job.
    """
    selected_keys = frozenset(base_url_keys)
    configured = {
        key: value.strip()
        for key, value in resolved_env.items()
        if key in _KNOWN_TRANSPORT_BASE_URL_KEYS and value.strip()
    }
    irrelevant = sorted(set(configured) - selected_keys)
    if irrelevant:
        raise RestrictedNetworkProfileError(
            "Restricted agent transport received base URL settings that its "
            f"effective agent does not consume: {', '.join(irrelevant)}."
        )

    selected = {key: configured[key] for key in base_url_keys if key in configured}
    if selected:
        # Compare each alias by its resolved ``(scheme, host)`` rather than by
        # the raw URL string. Legitimate aliases for one transport can differ by
        # PATH -- Azure emits AZURE_OPENAI_ENDPOINT as the bare resource
        # endpoint while OPENAI_BASE_URL / AZURE_API_BASE carry the
        # ``/openai/v1`` suffix -- and rejecting that would fail every
        # Azure-routed trial while granting no additional egress (the allowlist
        # is host-based, so a differing path grants nothing).
        #
        # Scheme deliberately stays in the comparison so a DIVERGENT scheme
        # (one alias downgraded to ``http`` while its siblings stay ``https``)
        # still fails closed -- host-only comparison would silently accept it,
        # and that alias would ship the API key in cleartext. This is a
        # disagreement check, not a TLS policy: aliases that are uniformly
        # ``http`` agree and are accepted, exactly as before this change. An
        # alias that resolves to no host at all is invalid rather than silently
        # ignored -- dropping it could leave one apparently-unanimous host and
        # mask a misconfigured route.
        resolved = {
            key: (urlparse(value).scheme, normalize_domain_or_url(value))
            for key, value in selected.items()
        }
        if any(host is None for _scheme, host in resolved.values()):
            raise RestrictedNetworkProfileError(
                "Restricted agent transport base URL is invalid."
            )
        if len(set(resolved.values())) > 1:
            raise RestrictedNetworkProfileError(
                "Restricted agent transport received conflicting aliases for its "
                f"selected base URL: {', '.join(sorted(selected))}."
            )
        hosts = {host for _scheme, host in resolved.values()}
        if len(hosts) != 1:
            raise RestrictedNetworkProfileError(
                "Restricted agent transport base URL is invalid."
            )
        return tuple(hosts)

    inferred = (
        tuple(
            outbound_hosts_for_model(agent_config.model_name, infer_bare_provider=True)
        )
        if infer_model
        else ()
    )
    return inferred or default_hosts


def _model_transport_base_url_keys(model_name: str | None) -> tuple[str, ...]:
    """Base URL aliases consumed by LiteLLM-style model transports.

    Provider inference is bare-id safe: ``openai/gpt-x`` and a bare ``gpt-x`` /
    ``o3`` both resolve to ``openai`` (via litellm + heuristic fallback), so a
    mini-swe trial on an unprefixed OpenAI model still consumes -- and is granted
    -- its OpenAI transport rather than an empty key set.

    A provider missing from this map resolves to an EMPTY key set, which makes
    the trial look deliberately transport-free and grants it no model egress at
    all -- a silent failure rather than a fail-closed one. ``azure`` /
    ``azure_openai`` are therefore listed explicitly: they are canonical
    providers (harbor ``PROVIDER_KEYS``, and ``job_tokens`` treats them as
    OpenAI-family), and an ``azure/<deployment>`` id names the same
    OpenAI-family transport as ``openai/``.
    """
    provider = (infer_model_provider_prefix(model_name) or "").strip().lower()
    return {
        "anthropic": ("ANTHROPIC_BASE_URL",),
        "anthropic-hdo": ("ANTHROPIC_BASE_URL",),
        "openai": _OPENAI_BASE_URL_KEYS,
        "azure": _OPENAI_BASE_URL_KEYS,
        "azure_openai": _OPENAI_BASE_URL_KEYS,
        "meta": ("META_BASE_URL", *_OPENAI_BASE_URL_KEYS),
        "openrouter": ("OPENROUTER_BASE_URL",),
        "fireworks": ("FIREWORKS_BASE_URL",),
        "zai": ("ZAI_BASE_URL",),
        "minimax": ("MINIMAX_BASE_URL",),
        "moonshot": ("MOONSHOT_BASE_URL",),
        "google": _GEMINI_BASE_URL_KEYS,
        "gemini": _GEMINI_BASE_URL_KEYS,
    }.get(provider, ())


def _no_base_url_keys(_agent_config: AgentConfig) -> tuple[str, ...]:
    return ()


def _anthropic_base_url_keys(_agent_config: AgentConfig) -> tuple[str, ...]:
    return ("ANTHROPIC_BASE_URL",)


def _openai_base_url_keys(_agent_config: AgentConfig) -> tuple[str, ...]:
    return _OPENAI_BASE_URL_KEYS


def _cursor_base_url_keys(_agent_config: AgentConfig) -> tuple[str, ...]:
    return _CURSOR_BASE_URL_KEYS


def _gemini_base_url_keys(_agent_config: AgentConfig) -> tuple[str, ...]:
    return _GEMINI_BASE_URL_KEYS


def _mini_swe_base_url_keys(agent_config: AgentConfig) -> tuple[str, ...]:
    return _model_transport_base_url_keys(agent_config.model_name)


BaseUrlKeyResolver = Callable[[AgentConfig], tuple[str, ...]]


@dataclass(frozen=True)
class _RestrictedAgentSpec:
    """One effective-agent contract: its restricted-network profile factory and
    the transport base-URL aliases that factory consumes.

    Pairing both in a single registry entry is the single source of truth: the
    factory selects transport hosts for exactly these keys, and the worker
    runtime-env filter drops any *other* known transport base URL using the same
    keys. Neither can drift from the other because there is one entry, not two
    parallel maps.
    """

    factory: ProfileFactory
    base_url_keys: BaseUrlKeyResolver
    # True for harnesses that front the model through their own service (e.g.
    # Cursor): such agents never talk to the model provider's API directly, so
    # the worker-private provider/Azure deployment id must not be swapped onto
    # their running model -- they need the public model identity.
    fronts_own_model_service: bool = False
    # True for harnesses whose profile PINS egress to one provider
    # (``infer_model=False``): cursor -> its own API, gemini-cli -> Gemini,
    # grok-build -> xAI. Distinct from fronts_own_model_service, which is the
    # narrower "this harness's service fronts arbitrary models" (only Cursor).
    # Both imply the model identity must survive: an agent that cannot reach the
    # OpenAI/Azure endpoint must not carry the worker-private Azure deployment
    # id, which its pinned transport could never resolve.
    pins_own_transport: bool = False


def _consumed_base_url_keys_for_class(
    agent_class: type[Any], agent_config: AgentConfig
) -> tuple[str, ...]:
    """Consumed transport base-URL aliases for an already-resolved agent class.

    Used by the profile factories, which are only ever dispatched for a class
    that is present in the registry; an absent class yields an empty tuple so
    the shared ``_selected_transport_hosts`` guard still fails closed.
    """
    spec = _COMPATIBILITY_PROFILES.get(_class_path(agent_class))
    return spec.base_url_keys(agent_config) if spec is not None else ()


def _transport_free_profile(
    _agent_class: type[Any],
    _agent_config: AgentConfig,
    _resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    return RestrictedNetworkProfile(server_web_disabled=True)


def _claude_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    hosts = _selected_transport_hosts(
        agent_config,
        resolved_env,
        base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
        default_hosts=_ANTHROPIC_RUNTIME_HOSTS,
    )
    return RestrictedNetworkProfile(
        outbound_hosts=hosts,
        kwarg_overrides={"disallowed_tools": _CLAUDE_WEB_TOOLS},
        server_web_disabled=True,
    )


def _codex_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    hosts = _selected_transport_hosts(
        agent_config,
        resolved_env,
        base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
        default_hosts=_OPENAI_RUNTIME_HOSTS,
    )
    return RestrictedNetworkProfile(
        outbound_hosts=hosts,
        kwarg_overrides={"web_search": "disabled"},
        server_web_disabled=True,
    )


def _grok_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    return RestrictedNetworkProfile(
        outbound_hosts=_selected_transport_hosts(
            agent_config,
            resolved_env,
            base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
            # grok-build is transport-authoritative: it always fronts xAI, so pin
            # its host to xAI and do NOT let model-id inference substitute another
            # provider's host (infer_model=False, mirroring _cursor_profile). The
            # default is used whenever no explicit xAI route is configured.
            default_hosts=_XAI_RUNTIME_HOSTS,
            infer_model=False,
        ),
        kwarg_overrides={"disable_web_search": True},
        server_web_disabled=True,
    )


def _mini_swe_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    # mini-swe-agent has no provider-side web search/fetch tool. Its shell is
    # still constrained by Harbor's network namespace policy.
    return RestrictedNetworkProfile(
        outbound_hosts=_selected_transport_hosts(
            agent_config,
            resolved_env,
            base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
        ),
        server_web_disabled=True,
    )


def _cursor_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    selected = _selected_transport_hosts(
        agent_config,
        resolved_env,
        base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
        infer_model=False,
    )
    # Cursor is transport-authoritative: its service fronts the selected model.
    # This list is complete, rather than being OR'd with an unrelated provider
    # fallback. Custom subclasses can instead return their own full union from
    # restricted_network_profile().
    hosts = tuple(dict.fromkeys([*_CURSOR_RUNTIME_HOSTS, *selected]))
    return RestrictedNetworkProfile(
        outbound_hosts=tuple(hosts),
        env_overrides=_CURSOR_ENV_OVERRIDES,
        server_web_disabled=True,
    )


def _gemini_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    env = dict(resolved_env)
    uses_oauth = bool(env.get("GEMINI_OAUTH_CREDS_PATH")) or env.get(
        "GEMINI_FORCE_OAUTH", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    uses_vertex = env.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    has_custom_base_url = any(env.get(key, "").strip() for key in _GEMINI_BASE_URL_KEYS)
    if uses_oauth:
        raise RestrictedNetworkProfileError(
            "Restricted Gemini CLI phases do not support OAuth transport because "
            "its runtime service hosts are not bounded. Use API-key auth with an "
            "explicit GOOGLE_GEMINI_BASE_URL or a public agent phase."
        )
    if uses_vertex and not has_custom_base_url:
        raise RestrictedNetworkProfileError(
            "Restricted Gemini CLI phases require an explicit "
            "GOOGLE_GEMINI_BASE_URL for Vertex transport."
        )
    hosts = _selected_transport_hosts(
        agent_config,
        resolved_env,
        base_url_keys=_consumed_base_url_keys_for_class(agent_class, agent_config),
        # gemini-cli is transport-authoritative: it always fronts the Gemini API
        # (or the explicit base URL above), so pin its host and do NOT let
        # model-id inference substitute another provider's host -- mirroring
        # _cursor_profile and _grok_profile. Without this, a gemini-cli trial
        # carrying a non-Gemini model id resolved that provider's hosts instead
        # (for OpenAI-family: api.openai.com plus the worker's private Azure
        # endpoint), granting egress the CLI never dials while never granting
        # the Gemini host it does.
        default_hosts=_GEMINI_RUNTIME_HOSTS,
        infer_model=False,
    )
    return RestrictedNetworkProfile(
        outbound_hosts=hosts,
        env_overrides={
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/etc/gemini-cli/settings.json"
        },
        kwarg_overrides={"disable_web_tools": True},
        server_web_disabled=True,
    )


def _unattested_stock_harness_profile(
    agent_class: type[Any],
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    """Registered for transport IDENTITY only -- never attested for a profile.

    Oddish swaps these stock harnesses for their Oddish wrapper, but not until
    after ``_build_agent_config`` has already run the deployment-swap gate,
    which reads ``fronts_own_model_service`` off the class resolved at that
    moment. Absent from the registry, the gate saw no spec and treated a
    transport-authoritative harness as one that talks to the provider directly,
    substituting the worker-private Azure deployment id onto a model whose
    egress is pinned to xAI/Gemini.

    Registering the stock class fixes that window while deliberately refusing to
    attest it: the Oddish wrapper is what disables provider-side web tools, so a
    trial pinned to the stock class by ``import_path`` (which the wrapper skips)
    must keep failing closed exactly as it did before.
    """
    raise RestrictedNetworkProfileError(
        f"{_class_path(agent_class)} does not declare a safe restricted-network "
        "profile; run this harness through its Oddish wrapper instead."
    )


# Temporary exact-class adapters for the stock implementations Oddish operates.
# A subclass is new executable code and must either appear here explicitly or
# define its own local capability hook; inheriting a trusted profile is unsafe.
_COMPATIBILITY_PROFILES: dict[str, _RestrictedAgentSpec] = {
    # Identity-only entries; see _unattested_stock_harness_profile.
    "harbor.agents.installed.gemini_cli:GeminiCli": _RestrictedAgentSpec(
        _unattested_stock_harness_profile,
        _gemini_base_url_keys,
        pins_own_transport=True,
    ),
    "harbor.agents.installed.grok_build:GrokBuild": _RestrictedAgentSpec(
        _unattested_stock_harness_profile,
        _no_base_url_keys,
        pins_own_transport=True,
    ),
    "harbor.agents.nop:NopAgent": _RestrictedAgentSpec(
        _transport_free_profile, _no_base_url_keys
    ),
    "harbor.agents.oracle:OracleAgent": _RestrictedAgentSpec(
        _transport_free_profile, _no_base_url_keys
    ),
    "harbor.agents.installed.claude_code:ClaudeCode": _RestrictedAgentSpec(
        _claude_profile, _anthropic_base_url_keys
    ),
    "harbor.agents.installed.codex:Codex": _RestrictedAgentSpec(
        _codex_profile, _openai_base_url_keys
    ),
    "harbor.agents.installed.cursor_cli:CursorCli": _RestrictedAgentSpec(
        _cursor_profile,
        _cursor_base_url_keys,
        fronts_own_model_service=True,
        pins_own_transport=True,
    ),
    "harbor.agents.installed.mini_swe_agent:MiniSweAgent": _RestrictedAgentSpec(
        _mini_swe_profile, _mini_swe_base_url_keys
    ),
    "oddish.workers.agents.claude_code:OddishClaudeCode": _RestrictedAgentSpec(
        _claude_profile, _anthropic_base_url_keys
    ),
    "oddish.workers.agents.claude_code:OddishProbeClaudeCode": _RestrictedAgentSpec(
        _claude_profile, _anthropic_base_url_keys
    ),
    "oddish.workers.agents.codex:OddishCodex": _RestrictedAgentSpec(
        _codex_profile, _openai_base_url_keys
    ),
    "oddish.workers.agents.codex:AzureCompatibleCodex": _RestrictedAgentSpec(
        _codex_profile, _openai_base_url_keys
    ),
    # grok-build and gemini-cli are transport-authoritative in exactly the sense
    # Cursor is: their profiles pin egress to xAI / Gemini (infer_model=False),
    # so the worker-private Azure deployment id must not be substituted for the
    # running model -- the sandbox would carry that identity while only being
    # able to reach a transport that has never heard of it. For their own
    # providers this changes nothing, since the deployment swap is gated on the
    # trial using the OpenAI provider in the first place.
    "oddish.workers.agents.grok_build:OddishGrokBuild": _RestrictedAgentSpec(
        _grok_profile, _no_base_url_keys, pins_own_transport=True
    ),
    "oddish.workers.agents.gemini_cli:OddishGeminiCli": _RestrictedAgentSpec(
        _gemini_profile, _gemini_base_url_keys, pins_own_transport=True
    ),
    "oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent": _RestrictedAgentSpec(
        _mini_swe_profile, _mini_swe_base_url_keys
    ),
    "oddish.workers.agents.mini_swe_agent:OddishMetaMiniSweAgent": _RestrictedAgentSpec(
        _mini_swe_profile, _mini_swe_base_url_keys
    ),
}


def _compatibility_factory(agent_class: type[Any]) -> ProfileFactory | None:
    spec = _COMPATIBILITY_PROFILES.get(_class_path(agent_class))
    return spec.factory if spec is not None else None


def consumed_transport_base_url_keys(
    agent_config: AgentConfig,
) -> tuple[str, ...] | None:
    """Transport base-URL aliases the effective agent's restricted profile consumes.

    Single source of truth, shared with the profile factories via
    ``_COMPATIBILITY_PROFILES``: a factory grants restricted-Compose egress for
    exactly the keys returned here. The worker runtime-env assembly uses this to
    drop any *other* known transport base URL before it can reach a profile's
    fail-closed "does not consume" guard.

    Returns ``None`` when the effective agent is a custom-hook or unrecognised
    class whose consumption cannot be attested from this registry. Such agents
    never reach ``_selected_transport_hosts`` (their own hook declares hosts and
    only sees ``_SAFE_PROFILE_ENV_KEYS``), so the caller leaves their env
    untouched rather than guessing -- an empty tuple, by contrast, means the
    agent is known to consume no transport base URL at all (e.g. nop/oracle,
    grok).
    """
    agent_class = resolve_effective_agent_class(agent_config)
    spec = _COMPATIBILITY_PROFILES.get(_class_path(agent_class))
    if spec is None:
        return None
    return spec.base_url_keys(agent_config)


def agent_fronts_own_model_service(agent_config: AgentConfig) -> bool:
    """Whether the effective agent routes the model through its own service.

    Such harnesses (Cursor, gemini-cli, grok-build) select the model on their
    side and talk to their own API, never the model provider's endpoint, so the
    worker-private provider/Azure deployment id must not be substituted for the
    running model -- they need the submitted public model identity. This is the
    same property their profiles express as ``infer_model=False``: an agent that
    pins its transport must also keep its model identity, or the sandbox ends up
    carrying a deployment id that its pinned transport cannot resolve. Agents
    that talk to the provider directly (codex, mini-swe) return False here,
    regardless of whether the model id is written ``openai/gpt-x`` or the bare
    ``gpt-x`` form.

    Only known stock harnesses can be attested as self-fronting; a custom import
    path that cannot be resolved (e.g. not importable at build time) is treated
    as not self-fronting so callers keep their existing behavior rather than
    crash. This intentionally never imports beyond what resolution requires.
    """
    try:
        agent_class = resolve_effective_agent_class(agent_config)
    except Exception:
        return False
    spec = _COMPATIBILITY_PROFILES.get(_class_path(agent_class))
    return bool(spec and spec.fronts_own_model_service)


def agent_keeps_public_model_identity(agent_config: AgentConfig) -> bool:
    """Whether the worker-private deployment id must NOT be swapped onto the model.

    True for a harness that fronts its own model service (Cursor) and for any
    harness whose profile pins egress to one provider (gemini-cli, grok-build).
    Both cases share the same consequence: the running agent cannot reach the
    OpenAI/Azure endpoint, so giving it the private Azure deployment id hands a
    worker-only identity to a sandbox that can never resolve it, while the
    submitted public model is what the harness actually needs.

    Agents that talk to the OpenAI/Azure endpoint directly (codex, mini-swe)
    return False and still receive the deployment rewrite.
    """
    try:
        agent_class = resolve_effective_agent_class(agent_config)
    except Exception:
        return False
    spec = _COMPATIBILITY_PROFILES.get(_class_path(agent_class))
    if spec is None:
        return False
    return bool(spec.fronts_own_model_service or spec.pins_own_transport)


def _safe_hook_context(
    resolved_env: Mapping[str, str], kwargs: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Expose routing metadata, never worker credentials, to custom hooks."""
    safe_env = {
        key: value
        for key, value in resolved_env.items()
        if key in _SAFE_PROFILE_ENV_KEYS
    }
    sensitive_fragments = ("key", "token", "secret", "password", "credential")

    def without_credentials(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: without_credentials(item)
                for key, item in value.items()
                if not any(
                    fragment in str(key).lower() for fragment in sensitive_fragments
                )
            }
        if isinstance(value, list):
            return [without_credentials(item) for item in value]
        if isinstance(value, tuple):
            return tuple(without_credentials(item) for item in value)
        return value

    safe_kwargs = without_credentials(kwargs)
    return safe_env, safe_kwargs


def restricted_network_profile_for_config(
    agent_config: AgentConfig,
    *,
    resolved_env: Mapping[str, str],
) -> RestrictedNetworkProfile:
    """Resolve and validate one complete profile for the effective agent class."""
    agent_class = resolve_effective_agent_class(agent_config)
    # Only a hook defined by this exact class is trusted. ``getattr`` alone
    # would let an arbitrary subclass inherit a stock agent's attestation.
    local_hook = agent_class.__dict__.get("restricted_network_profile")
    hook = getattr(agent_class, "restricted_network_profile", None)
    if local_hook is not None and callable(hook):
        safe_env, safe_kwargs = _safe_hook_context(
            resolved_env, dict(agent_config.kwargs or {})
        )
        raw = hook(
            model_name=agent_config.model_name,
            env=safe_env,
            kwargs=safe_kwargs,
        )
    else:
        factory = _compatibility_factory(agent_class)
        raw = (
            factory(agent_class, agent_config, resolved_env)
            if factory is not None
            else None
        )
    return _coerce_profile(raw, agent_class=agent_class)


def apply_restricted_network_profile(
    *,
    agent_config: AgentConfig,
    resolved_env: Mapping[str, str],
    runtime_only_hosts: bool = False,
) -> RestrictedNetworkProfile:
    """Force-apply a validated profile and return it for setup host handling."""
    profile = restricted_network_profile_for_config(
        agent_config,
        resolved_env=resolved_env,
    )
    if runtime_only_hosts:
        set_runtime_allowed_hosts(agent_config, profile.outbound_hosts)
    else:
        agent_config.extra_allowed_hosts = list(
            dict.fromkeys([*agent_config.extra_allowed_hosts, *profile.outbound_hosts])
        )
    kwargs = dict(agent_config.kwargs or {})
    kwargs.update(profile.kwarg_overrides)
    agent_config.kwargs = kwargs
    env = dict(agent_config.env or {})
    env.update(profile.env_overrides)
    agent_config.env = env
    return profile
