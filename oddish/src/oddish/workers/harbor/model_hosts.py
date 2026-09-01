"""Agent/model → outbound API hosts for restricted-network trials.

Oddish routes many providers through the same agent harness (notably
``claude-code``), so the model id usually decides which API host must be
reachable -- including harnesses that front models through their own service
(e.g. the ``cursor/`` model prefix maps to Cursor's API host). Prefer hosts
already present in the trial's agent env, then fall back to Oddish's
classifiers and default base URLs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from oddish.config import (
    DEEPSEEK_DEFAULT_BASE_URL,
    FIREWORKS_DEFAULT_BASE_URL,
    GEOMETRIC_DEFAULT_BASE_URL,
    META_DEFAULT_BASE_URL,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    ZAI_DEFAULT_BASE_URL,
    infer_model_provider_prefix,
    is_anthropic_hdo_model,
    is_deepseek_model,
    is_fireworks_model,
    is_geometric_model,
    is_meta_model,
    is_minimax_model,
    is_moonshot_model,
    is_xai_model,
    is_zai_model,
    looks_like_bedrock_model_id,
    settings,
)
from oddish.workers.agents.network import normalize_domain_or_url

# Transport base-URL env keys, grouped by provider. These are the SINGLE SOURCE
# for both host discovery (here) and the restricted-egress fail-closed filter
# (``restricted_network`` imports the derived tuple/set below and never spells
# the keys out again). A provider key added here is therefore filtered and
# discovered in both places at once, so the two boundaries can never drift apart
# -- the failure mode Bugbot flagged when the host tuples, but not the key
# lists, were deduplicated into this module.
ANTHROPIC_BASE_URL_KEYS = ("ANTHROPIC_BASE_URL",)
OPENAI_BASE_URL_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
META_BASE_URL_KEYS = ("META_BASE_URL",)
GEOMETRIC_BASE_URL_KEYS = ("GEOMETRIC_BASE_URL",)
OPENROUTER_BASE_URL_KEYS = ("OPENROUTER_BASE_URL",)
FIREWORKS_BASE_URL_KEYS = ("FIREWORKS_BASE_URL",)
ZAI_BASE_URL_KEYS = ("ZAI_BASE_URL",)
MINIMAX_BASE_URL_KEYS = ("MINIMAX_BASE_URL",)
MOONSHOT_BASE_URL_KEYS = ("MOONSHOT_BASE_URL",)
DEEPSEEK_BASE_URL_KEYS = ("DEEPSEEK_BASE_URL",)
GEMINI_BASE_URL_KEYS = (
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_API_BASE_URL",
    "GOOGLE_API_BASE_URL",
)
CURSOR_BASE_URL_KEYS = ("CURSOR_API_BASE_URL", "CURSOR_API_ENDPOINT")

# Gemini's non-route OAuth toggles. They select credentials rather than widen
# egress, so they are NOT transport base-URL keys and never enter the discovery
# tuple / KNOWN_TRANSPORT_BASE_URL_KEYS below. They do travel with the Gemini
# base-URL keys through the safe-profile allowlist (restricted_network) and the
# runtime credential fold (runner), both of which import this single source
# instead of re-listing the names.
GEMINI_OAUTH_ENV_KEYS = (
    "GEMINI_FORCE_OAUTH",
    "GEMINI_OAUTH_CREDS_PATH",
    "GOOGLE_GENAI_USE_VERTEXAI",
)

# Ordered discovery tuple: host discovery iterates the trial env in this order.
_BASE_URL_ENV_KEYS = (
    *ANTHROPIC_BASE_URL_KEYS,
    *OPENAI_BASE_URL_KEYS,
    *META_BASE_URL_KEYS,
    *GEOMETRIC_BASE_URL_KEYS,
    *OPENROUTER_BASE_URL_KEYS,
    *FIREWORKS_BASE_URL_KEYS,
    *ZAI_BASE_URL_KEYS,
    *MINIMAX_BASE_URL_KEYS,
    *MOONSHOT_BASE_URL_KEYS,
    *DEEPSEEK_BASE_URL_KEYS,
    *GEMINI_BASE_URL_KEYS,
    *CURSOR_BASE_URL_KEYS,
)

# Azure aliases for the SAME OpenAI-family transport that ``OPENAI_BASE_URL``
# already carries: ``get_openai_agent_env`` emits ``AZURE_API_BASE`` (LiteLLM
# route) and ``AZURE_OPENAI_ENDPOINT`` (Azure SDK route) alongside it. They are
# deliberately kept out of the discovery tuple above -- discovery would gain no
# host from them (``OPENAI_BASE_URL`` holds the same URL) and adding them would
# change public-path allowlists. They belong in the fail-closed set below so a
# caller cannot submit an unattested route under an alias, and so the
# consumed-route filter drops the worker's private Azure endpoint for agents
# that front their own transport.
AZURE_BASE_URL_KEYS = ("AZURE_API_BASE", "AZURE_OPENAI_ENDPOINT")

# Full known-transport key set for the restricted-egress fail-closed filter.
KNOWN_TRANSPORT_BASE_URL_KEYS = frozenset((*_BASE_URL_ENV_KEYS, *AZURE_BASE_URL_KEYS))

_ANTHROPIC_HOSTS = ("api.anthropic.com", "mcp-proxy.anthropic.com")
_OPENAI_HOSTS = ("api.openai.com", "ab.chatgpt.com")
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)
_XAI_HOSTS = ("api.x.ai",)
# Cursor CLI fronts every selectable model through Cursor's own API. Its
# bootstrap endpoint returns the agent-stream URL at runtime (currently under
# api5), and the installer is intentionally unpinned. Use Cursor's official
# domain boundary instead of encoding ephemeral transport hostnames.
_CURSOR_RUNTIME_HOSTS = ("*.cursor.sh",)
# tbh (published as ``muse``) reaches Meta through its own service rather than
# the OpenAI-compatible model API, so its host does not follow from the model
# id the way every other agent's does: a ``meta/`` model resolves
# ``api.ai.meta.com`` while the harness dials ``api.meta.ai`` for its model
# catalog and inference. Keyed on the AGENT, unlike the cursor arm below, which
# is keyed on a ``cursor/`` model prefix.
TBH_BASE_URL_KEYS = ("TBH_BASE_URL",)
_TBH_RUNTIME_HOSTS = ("api.meta.ai",)
_DSH_INSTALL_HOSTS: tuple[str, ...] = (
    "raw.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "nodejs.org",
    "registry.npmjs.org",
)
_DSH_DEEPSEEK_RUNTIME_HOSTS = ("api.deepseek.com",)
# opencode has no pre-baked worker image: Harbor's ``OpenCode.install``
# bootstraps nvm, a Node runtime, and the ``opencode-ai`` npm package during
# agent SETUP -- which runs under the ENVIRONMENT baseline, before the
# agent-phase allowlist ever applies. The runner's opencode arm therefore
# merges these hosts into the environment baseline (mirroring the claude-code
# installer arm), NOT into ``_AGENT_RUNTIME_HOSTS`` -- an agent-phase
# registration could never cover the setup-phase install, and would widen the
# agent-run phase of modern swe-marathon-shaped tasks for no benefit. This is
# the narrowest set that lets the documented install script complete; the
# model transport host comes from ``outbound_hosts_for_model`` as usual.
OPENCODE_INSTALL_HOSTS: tuple[str, ...] = (
    "raw.githubusercontent.com",  # nvm install.sh
    "github.com",  # nvm git source + opencode-ai release redirect
    "objects.githubusercontent.com",  # GitHub release-asset CDN
    "codeload.github.com",  # GitHub tarball fetch
    "nodejs.org",  # Node runtime downloaded by nvm
    "registry.npmjs.org",  # npm metadata + package tarballs
    # On images without curl the install script first runs
    # ``apt-get update && apt-get install -y curl``, which needs the apt
    # mirrors -- and ``apt-get update`` fails outright when any configured
    # source (nodesource on Node-based images) is unreachable.
    "archive.ubuntu.com",  # apt packages (curl bootstrap)
    "security.ubuntu.com",  # apt security pocket
    "deb.nodesource.com",  # apt source preconfigured on Node-based images
)
# Gemini CLI uses Harbor's same nvm/Node bootstrap as OpenCode, then installs
# ``@google/gemini-cli`` with npm during agent setup.  Setup runs under the
# environment baseline, before a restricted agent-phase profile exists, so the
# runner must add this chain to that baseline.
GEMINI_CLI_INSTALL_HOSTS: tuple[str, ...] = OPENCODE_INSTALL_HOSTS
# agy has no pre-baked worker image: Harbor's ``AntigravityCli.install``
# fetches install.sh from antigravity.google, which resolves a manifest on
# the auto-updater Cloud Run host and downloads the binary tarball from GCS.
# Installs run during agent SETUP (environment baseline) — the runner's
# antigravity arm merges these, mirroring OPENCODE_INSTALL_HOSTS above.
ANTIGRAVITY_INSTALL_HOSTS: tuple[str, ...] = (
    "antigravity.google",  # install.sh
    "antigravity-cli-auto-updater-974169037036.us-central1.run.app",  # manifest
    "storage.googleapis.com",  # binary tarball (antigravity-public bucket)
)
# Non-transport endpoints the agy binary dials on every invocation, captured
# live from agy 1.1.19 through a CONNECT-logging proxy. The Unleash
# feature-flag host is contacted before any model call, and an egress filter
# that silently DROPS its SYNs stalls startup through kernel retries until the
# agent timeout — allowing it is an availability requirement, not a nicety. The
# Playwright CDNs back agy's bundled browser tooling and play.googleapis.com
# carries its telemetry; both are probed at startup with the same stall risk.
# None of them follow from the model endpoint, so they must survive a custom
# Gemini base URL: that setting replaces the MODEL host and nothing else.
ANTIGRAVITY_STARTUP_HOSTS: tuple[str, ...] = (
    "antigravity-unleash.goog",
    "play.googleapis.com",
    "playwright.azureedge.net",
    "playwright-akamai.azureedge.net",
    "playwright-verizon.azureedge.net",
)
# Default agent-phase set: the Gemini transport plus those startup probes.
ANTIGRAVITY_RUNTIME_HOSTS: tuple[str, ...] = (
    *_GEMINI_HOSTS,
    *ANTIGRAVITY_STARTUP_HOSTS,
)
_AGENT_RUNTIME_HOSTS: dict[str, tuple[str, ...]] = {
    "tbh": _TBH_RUNTIME_HOSTS,
    "dsh": _DSH_INSTALL_HOSTS + _DSH_DEEPSEEK_RUNTIME_HOSTS,
    "antigravity-cli": ANTIGRAVITY_RUNTIME_HOSTS,
}
# Import-path-shaped configs reach the registry through the lowercased class
# basename (wrappers null ``name``); map those spellings back to the agent key.
_CLASS_BASENAME_RUNTIME_KEYS: dict[str, str] = {
    "oddishantigravitycli": "antigravity-cli",
    "antigravitycli": "antigravity-cli",
}

_DEFAULT_BEDROCK_REGION = "us-east-1"
_BEDROCK_STS_DOMAINS = ("sts.amazonaws.com",)


def _looks_like_bedrock_model(model_name: str | None) -> bool:
    if looks_like_bedrock_model_id(model_name):
        return True
    if not model_name:
        return False
    head, _, tail = model_name.strip().lower().partition("/")
    return head == "bedrock" and bool(tail)


def bedrock_domains_for_model(
    *,
    model_name: str | None,
    region: str | None = None,
    small_model_region: str | None = None,
) -> list[str]:
    region = (region or _DEFAULT_BEDROCK_REGION).strip().lower()
    domains = [
        f"bedrock-runtime.{region}.amazonaws.com",
        f"bedrock.{region}.amazonaws.com",
        *_BEDROCK_STS_DOMAINS,
    ]
    if small_model_region and small_model_region.lower() != region:
        small = small_model_region.strip().lower()
        domains.extend(
            [f"bedrock-runtime.{small}.amazonaws.com", f"bedrock.{small}.amazonaws.com"]
        )

    tail = (model_name or "").split("/", 1)[-1].lower()
    extras: set[str] = set()
    regions: tuple[str, ...]
    if tail.startswith(("us.", "global.")):
        regions = ("us-east-1", "us-west-2")
    elif tail.startswith("eu."):
        regions = ("eu-central-1", "eu-west-1")
    elif tail.startswith(("apac.", "apn.")):
        regions = ("ap-northeast-1", "ap-southeast-2")
    else:
        regions = ()
    for extra_region in regions:
        extras.add(f"bedrock-runtime.{extra_region}.amazonaws.com")
        extras.add(f"bedrock.{extra_region}.amazonaws.com")
    return sorted(set(domains) | extras)


def _host_from_url(value: str | None) -> str | None:
    return normalize_domain_or_url(value)


def _hosts_from_env(
    env: Mapping[str, str] | None,
    *,
    keys: tuple[str, ...] = _BASE_URL_ENV_KEYS,
) -> list[str]:
    if not env:
        return []
    hosts: list[str] = []
    for key in keys:
        host = _host_from_url(env.get(key))
        if host:
            hosts.append(host)
    return hosts


def gemini_cli_transport_hosts(agent_env: Mapping[str, str] | None = None) -> list[str]:
    """Return the Gemini CLI transport hosts, with an explicit route replacing Google.

    Gemini CLI always calls the Gemini API; its submitted model name is not a
    transport decision.  A configured Gemini base URL is therefore the whole
    transport allowlist, matching the restricted-network Gemini profile.
    """
    return _hosts_from_env(agent_env, keys=GEMINI_BASE_URL_KEYS) or list(_GEMINI_HOSTS)


def _default_host(url: str) -> str | None:
    return _host_from_url(url)


def agent_runtime_hosts(
    *,
    agent_name: str | None,
    import_path: str | None = None,
    agent_kwargs: Mapping[str, Any] | None = None,
    agent_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Hosts an agent's OWN service needs, independent of the model provider.

    Model-derived hosts cover every agent that talks to the provider endpoint
    the model id names. An agent that fronts its own service needs its host
    added on top, or a restricted trial's allowlist is missing the only host
    the harness actually dials. A caller-supplied endpoint (the ``base_url``
    kwarg or ``TBH_BASE_URL``) is added alongside rather than replacing the
    default, so pointing at a staging endpoint cannot silently drop the
    production one a fallback might still use.
    """
    key = (agent_name or "").strip().lower()
    if not key and import_path:
        # Oddish wrappers null the name and set an import path instead.
        key = import_path.rsplit(":", 1)[-1].strip().lower()
        key = _CLASS_BASENAME_RUNTIME_KEYS.get(key, key)
    hosts = list(_AGENT_RUNTIME_HOSTS.get(key, ()))
    if not hosts:
        return []

    override: Any = None
    if isinstance(agent_kwargs, Mapping):
        override = agent_kwargs.get("base_url")
        extra_env = agent_kwargs.get("extra_env")
        if not override and isinstance(extra_env, Mapping):
            override = next(
                (
                    extra_env.get(k)
                    for k in (*TBH_BASE_URL_KEYS, *DEEPSEEK_BASE_URL_KEYS)
                    if extra_env.get(k)
                ),
                None,
            )
    if not override and isinstance(agent_env, Mapping):
        override = next(
            (
                agent_env.get(k)
                for k in (*TBH_BASE_URL_KEYS, *DEEPSEEK_BASE_URL_KEYS)
                if agent_env.get(k)
            ),
            None,
        )
    if isinstance(override, str):
        host = _host_from_url(override)
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def outbound_hosts_for_model(
    model_name: str | None,
    *,
    agent_env: Mapping[str, str] | None = None,
    agent_kwargs: dict[str, Any] | None = None,
    infer_bare_provider: bool = False,
) -> list[str]:
    """Return API hosts the trial must reach for *model_name*.

    Precedence:
    1. Base URLs already on the agent env / kwargs ``extra_env`` (set by Oddish
       provider routing after model normalization).
    2. Oddish model classifiers + default provider base URLs.
    3. Generic provider-prefix / Bedrock heuristics.
    """
    hosts: list[str] = []
    hosts.extend(_hosts_from_env(agent_env))

    extra_env = (agent_kwargs or {}).get("extra_env")
    if isinstance(extra_env, dict):
        hosts.extend(_hosts_from_env(extra_env))

    # Canonicalize a bare id to ``provider/model`` (opt-in; restricted-Compose
    # host inference only) so the provider classifiers and prefix switch below
    # resolve a bare id to its host. This covers providers that have a bare-id
    # heuristic in infer_model_provider_prefix (e.g. xai/grok-*, zai/glm-*,
    # minimax, moonshot/kimi-*) plus bare Bedrock ids; prefix-only providers
    # (meta, geometric, fireworks, anthropic-hdo) still require an explicit prefix,
    # as before.
    # The single-container union path leaves bare ids untouched (infer_bare_provider
    # is False there), so it does not widen beyond the routed transport.
    if infer_bare_provider and model_name:
        if "/" not in model_name:
            _bare_provider = infer_model_provider_prefix(model_name)
            if _bare_provider:
                model_name = f"{_bare_provider}/{model_name}"
        else:
            # An ALIAS prefix must resolve the same host as the canonical
            # provider it normalizes to. The transport-key map keys off the
            # canonical name (``claude/`` -> anthropic, ``palm/`` -> gemini,
            # ``glm/`` -> zai), so without this the classifiers and prefix
            # switch below -- which match the raw head -- resolve transport
            # KEYS but no HOST. On restricted Compose that is an empty
            # allowlist for an agent with no default hosts (mini-swe): the
            # agent phase silently cannot reach the model API. Rewriting to the
            # canonical prefix keeps the two boundaries in agreement.
            _head, _, _tail = model_name.partition("/")
            _canonical = infer_model_provider_prefix(model_name)
            if _canonical and _canonical != _head.strip().lower():
                model_name = f"{_canonical}/{_tail}"

    if is_fireworks_model(model_name):
        host = _default_host(
            os.environ.get("FIREWORKS_BASE_URL") or FIREWORKS_DEFAULT_BASE_URL
        )
        if host:
            hosts.append(host)
    elif is_zai_model(model_name):
        host = _default_host(os.environ.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL)
        if host:
            hosts.append(host)
    elif is_minimax_model(model_name):
        host = _default_host(
            os.environ.get("MINIMAX_BASE_URL") or MINIMAX_DEFAULT_BASE_URL
        )
        if host:
            hosts.append(host)
    elif is_moonshot_model(model_name):
        host = _default_host(
            os.environ.get("MOONSHOT_BASE_URL") or MOONSHOT_DEFAULT_BASE_URL
        )
        if host:
            hosts.append(host)
    elif is_deepseek_model(model_name):
        host = _default_host(
            os.environ.get("DEEPSEEK_BASE_URL") or DEEPSEEK_DEFAULT_BASE_URL
        )
        if host:
            hosts.append(host)
    elif is_xai_model(model_name):
        hosts.extend(_XAI_HOSTS)
    elif is_meta_model(model_name):
        host = _default_host(settings.meta_base_url or META_DEFAULT_BASE_URL)
        if host:
            hosts.append(host)
    elif is_geometric_model(model_name):
        host = _default_host(settings.geometric_base_url or GEOMETRIC_DEFAULT_BASE_URL)
        if host:
            hosts.append(host)
    elif is_anthropic_hdo_model(model_name):
        # Direct Anthropic API with the HDO key — same hosts as anthropic/.
        hosts.extend(_ANTHROPIC_HOSTS)
    elif _looks_like_bedrock_model(model_name):
        hosts.extend(bedrock_domains_for_model(model_name=model_name))
    elif model_name:
        raw = model_name.strip().lower()
        head = raw.split("/", 1)[0] if "/" in raw else ""
        if head == "openrouter":
            hosts.append(
                _default_host(
                    os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api"
                )
                or "openrouter.ai"
            )
        elif head in ("anthropic",):
            hosts.extend(_ANTHROPIC_HOSTS)
        elif head == "openai":
            hosts.extend(_OPENAI_HOSTS)
            if settings.get_openai_provider() == OPENAI_PROVIDER_AZURE:
                azure_host = _host_from_url(settings.azure_openai_endpoint)
                if azure_host:
                    hosts.append(azure_host)
        elif head in ("azure", "azure_openai"):
            # An explicit ``azure/`` id names the Azure OpenAI transport, so
            # resolve the configured Azure endpoint rather than the public
            # OpenAI hosts -- granting api.openai.com here would widen egress to
            # a service this trial never calls. Like every other arm this UNIONS
            # with any routed base URL discovered from the env above; only the
            # bare-``claude-`` arm is guarded on ``not hosts``.
            azure_host = _host_from_url(settings.azure_openai_endpoint)
            if azure_host:
                hosts.append(azure_host)
        elif head in ("gemini", "google", "vertex_ai"):
            # ``vertex_ai`` is spelled exactly as litellm spells it, which is
            # also the key _MODEL_PROVIDER_ALIASES folds into ``gemini`` for
            # transport-key selection. Adding a spelling that the alias map does
            # NOT carry (e.g. a hyphenated ``vertex-ai``) would recreate the
            # drift this arm exists to prevent: the id would resolve a host here
            # while _model_transport_base_url_keys returned an empty key set,
            # dropping the operator's real route and granting the public one.
            hosts.extend(_GEMINI_HOSTS)
        elif head == "cursor":
            hosts.extend(_CURSOR_RUNTIME_HOSTS)
        elif head == "bedrock":
            hosts.extend(bedrock_domains_for_model(model_name=model_name))
        elif not head and not hosts and raw.startswith("claude-"):
            # Force-direct-API routing strips the provider prefix so claude-code
            # gets the bare Anthropic id it requires, which otherwise leaves the
            # restricted-network allowlist with no model host to resolve. A
            # routed base URL above still wins.
            hosts.extend(_ANTHROPIC_HOSTS)

    # Dedupe, drop empties, stable order.
    return list(dict.fromkeys(h for h in hosts if h))
