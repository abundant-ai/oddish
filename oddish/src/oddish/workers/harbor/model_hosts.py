"""Model/provider → outbound API hosts for restricted-network trials.

Oddish routes many providers through the same agent harness (notably
``claude-code``). The model id — not the agent name — decides which API host
must be reachable. Prefer hosts already present in the trial's agent env
(base URLs set by provider routing), then fall back to Oddish's model
classifiers and default base URLs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from harbor.environments.modal_network import (
    bedrock_domains_for_model,
    looks_like_bedrock_model,
    normalize_domain_or_url,
)

from oddish.config import (
    FIREWORKS_DEFAULT_BASE_URL,
    META_DEFAULT_BASE_URL,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    ZAI_DEFAULT_BASE_URL,
    is_fireworks_model,
    is_meta_model,
    is_minimax_model,
    is_moonshot_model,
    is_xai_model,
    is_zai_model,
    settings,
)

_BASE_URL_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "META_BASE_URL",
    "OPENROUTER_BASE_URL",
    "FIREWORKS_BASE_URL",
    "ZAI_BASE_URL",
    "MINIMAX_BASE_URL",
    "MOONSHOT_BASE_URL",
)

_ANTHROPIC_HOSTS = ("api.anthropic.com", "mcp-proxy.anthropic.com")
_OPENAI_HOSTS = ("api.openai.com", "ab.chatgpt.com")
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)


def _host_from_url(value: str | None) -> str | None:
    return normalize_domain_or_url(value)


def _hosts_from_env(env: Mapping[str, str] | None) -> list[str]:
    if not env:
        return []
    hosts: list[str] = []
    for key in _BASE_URL_ENV_KEYS:
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
        hosts.append("api.x.ai")
    elif is_meta_model(model_name):
        host = _default_host(settings.meta_base_url or META_DEFAULT_BASE_URL)
        if host:
            hosts.append(host)
    elif looks_like_bedrock_model(model_name):
        hosts.extend(bedrock_domains_for_model(model_name=model_name))
    elif model_name:
        raw = model_name.strip().lower()
        head = raw.split("/", 1)[0] if "/" in raw else ""
        if head == "openrouter":
            hosts.append(
                _default_host(
                    os.environ.get("OPENROUTER_BASE_URL")
                    or "https://openrouter.ai/api"
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
        elif head == "bedrock":
            hosts.extend(bedrock_domains_for_model(model_name=model_name))

    # Dedupe, drop empties, stable order.
    return list(dict.fromkeys(h for h in hosts if h))
