"""Known text models for the provider connections installed in this API process.

The bundled LiteLLM catalog describes models, not account entitlements. Loading
it does not contact providers or import LiteLLM (which can fetch pricing at import).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib.metadata import distribution

from oddish.config import settings
from oddish.core.llm_key_fingerprint import provider_key_var

# Only routes whose credentials/check transport Oddish understands belong here.
_REGISTRY_ROUTES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "bedrock": "bedrock",
    "bedrock_converse": "bedrock",
    "gemini": "gemini",
    "fireworks_ai": "fireworks",
    "moonshot": "moonshot",
    "minimax": "minimax",
    "zai": "zai",
    "xai": "xai",
    "openrouter": "openrouter",
    "meta_llama": "meta",
    "deepseek": "deepseek",
}


def credential_variable(route: str) -> str | None:
    if route == "xai-swem":
        return "XAI_SWEM_API_KEY"
    return provider_key_var(route)


def credential_configured(route: str) -> bool | None:
    """Presence only; None means the SDK can authenticate without a static key."""
    variable = credential_variable(route)
    if not variable:
        return None
    raw = getattr(settings, variable.lower(), None) or os.environ.get(variable)
    if raw and raw.strip():
        return True
    if route in {"bedrock", "vertex_ai"}:
        return None
    return False


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict]:
    path = distribution("litellm").locate_file(
        "litellm/model_prices_and_context_window_backup.json"
    )
    return json.loads(path.read_text())


def provider_models() -> set[tuple[str, str, str]]:
    """Return (model, provider, route), retaining case-sensitive provider IDs."""
    models: set[tuple[str, str, str]] = set()
    for identifier, info in _registry().items():
        registry_provider = info.get("litellm_provider")
        route = _REGISTRY_ROUTES.get(registry_provider)
        if route is None or info.get("mode") != "chat":
            continue
        bare = identifier.removeprefix(f"{registry_provider}/")
        # These are pricing records, not invokable model IDs. Regional Bedrock
        # records duplicate the unqualified ID and don't select our AWS region.
        if bare.startswith("ft:") or bare == "container":
            continue
        if route == "bedrock" and "/" in bare:
            continue
        if route == "fireworks":
            bare = bare.removeprefix("accounts/fireworks/models/")
        model = (
            bare
            if route == "bedrock"
            else f"google/{bare}" if route == "gemini" else f"{route}/{bare}"
        )
        routes = [route]
        if route == "anthropic":
            routes.append("anthropic-hdo")
        elif route == "xai":
            routes.append("xai-swem")
        for connection in routes:
            if credential_configured(connection) is not True:
                continue
            connection_model = (
                f"anthropic-hdo/{bare}" if connection == "anthropic-hdo" else model
            )
            provider = "anthropic-hdo" if connection == "anthropic-hdo" else route
            models.add((connection_model, provider, connection))

    # Azure deployment names belong to this account; the generic Azure pricing
    # catalog cannot tell us which deployments exist or what they are called.
    for deployment in settings.azure_openai_deployments.values():
        models.add((f"azure/{deployment}", "azure", "azure"))
    return models
