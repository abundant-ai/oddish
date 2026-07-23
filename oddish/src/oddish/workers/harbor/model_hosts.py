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
    FIREWORKS_DEFAULT_BASE_URL,
    META_DEFAULT_BASE_URL,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    ZAI_DEFAULT_BASE_URL,
    infer_model_provider_prefix,
    is_anthropic_hdo_model,
    is_fireworks_model,
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
OPENROUTER_BASE_URL_KEYS = ("OPENROUTER_BASE_URL",)
FIREWORKS_BASE_URL_KEYS = ("FIREWORKS_BASE_URL",)
ZAI_BASE_URL_KEYS = ("ZAI_BASE_URL",)
MINIMAX_BASE_URL_KEYS = ("MINIMAX_BASE_URL",)
MOONSHOT_BASE_URL_KEYS = ("MOONSHOT_BASE_URL",)
GEMINI_BASE_URL_KEYS = (
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_API_BASE_URL",
    "GOOGLE_API_BASE_URL",
)
CURSOR_BASE_URL_KEYS = ("CURSOR_API_BASE_URL", "CURSOR_API_ENDPOINT")

# Ordered discovery tuple: host discovery iterates the trial env in this order.
_BASE_URL_ENV_KEYS = (
    *ANTHROPIC_BASE_URL_KEYS,
    *OPENAI_BASE_URL_KEYS,
    *META_BASE_URL_KEYS,
    *OPENROUTER_BASE_URL_KEYS,
    *FIREWORKS_BASE_URL_KEYS,
    *ZAI_BASE_URL_KEYS,
    *MINIMAX_BASE_URL_KEYS,
    *MOONSHOT_BASE_URL_KEYS,
    *GEMINI_BASE_URL_KEYS,
    *CURSOR_BASE_URL_KEYS,
)

# Full known-transport key set for the restricted-egress fail-closed filter.
KNOWN_TRANSPORT_BASE_URL_KEYS = frozenset(_BASE_URL_ENV_KEYS)

_ANTHROPIC_HOSTS = ("api.anthropic.com", "mcp-proxy.anthropic.com")
_OPENAI_HOSTS = ("api.openai.com", "ab.chatgpt.com")
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)
_XAI_HOSTS = ("api.x.ai",)
# Cursor CLI fronts every selectable model through Cursor's own API. Its
# bootstrap endpoint returns the agent-stream URL at runtime (currently under
# api5), and the installer is intentionally unpinned. Use Cursor's official
# domain boundary instead of encoding ephemeral transport hostnames.
_CURSOR_RUNTIME_HOSTS = ("*.cursor.sh",)

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


def _default_host(url: str) -> str | None:
    return _host_from_url(url)


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
    # (meta, fireworks, anthropic-hdo) still require an explicit prefix, as before.
    # The single-container union path leaves bare ids untouched (infer_bare_provider
    # is False there), so it does not widen beyond the routed transport.
    if infer_bare_provider and model_name and "/" not in model_name:
        _bare_provider = infer_model_provider_prefix(model_name)
        if _bare_provider:
            model_name = f"{_bare_provider}/{model_name}"

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
    elif is_xai_model(model_name):
        hosts.extend(_XAI_HOSTS)
    elif is_meta_model(model_name):
        host = _default_host(settings.meta_base_url or META_DEFAULT_BASE_URL)
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
        elif head in ("gemini", "google"):
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
