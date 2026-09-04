"""Operator model catalog and direct provider completion checks."""

from __future__ import annotations

import logging
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from oddish.config import (
    OPENAI_PROVIDER_AZURE,
    anthropic_hdo_bare_model_id,
    fireworks_api_model_id,
    fireworks_bare_model_id,
    infer_model_provider_prefix,
    meta_bare_model_id,
    settings,
    to_anthropic_api_model_id,
)
from oddish.core.endpoints import browse_task_facets_core
from oddish.core.llm_key_fingerprint import provider_key_var
from oddish.db import get_session
from pydantic import BaseModel, Field, field_validator

from auth import AuthContext, AuthMethod, require_auth
from auth.permissions import is_operator_org, require_operator_org
from models import APIKeyScope

router = APIRouter(prefix="/models", tags=["Models"])
logger = logging.getLogger(__name__)

_MODEL_CATALOG_TTL_SECONDS = 30.0
_MODEL_CHECK_COOLDOWN_SECONDS = 5.0


class ModelEndpointSummary(BaseModel):
    model: str
    provider: str
    route: str
    credential: str | None
    testable: bool


class ModelEndpointCatalogResponse(BaseModel):
    allowed: bool
    models: list[ModelEndpointSummary]


class ModelEndpointCheckRequest(BaseModel):
    model: str = Field(min_length=1, max_length=512)
    route: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("model", "route")
    @classmethod
    def strip_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value.lower()


class ModelEndpointCheckResponse(BaseModel):
    ok: bool
    model: str
    resolved_model: str
    provider: str
    route: str
    credential: str | None
    transport: Literal["litellm_completion"] = "litellm_completion"
    failure_kind: Literal["provider", "configuration"] | None = None
    status_code: int | None = None
    latency_ms: int
    response: str | None = None
    error: str | None = None
    request_id: str | None = None


_model_catalog_cache: dict[str, tuple[float, tuple[ModelEndpointSummary, ...]]] = {}
_model_check_cache: dict[
    tuple[str, str, str, str], tuple[float, ModelEndpointCheckResponse | None]
] = {}


def _provider_route(provider: str, model: str) -> str:
    if model.startswith("vertex_ai/"):
        return "vertex_ai"
    if provider == "openai":
        return settings.get_openai_provider()
    return provider


def _check_route(provider: str, model: str, requested_route: str | None) -> str:
    default_route = _provider_route(provider, model)
    route = requested_route or default_route
    if route != default_route:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Route {route!r} is not valid for {provider!r}; "
                f"expected {default_route}"
            ),
        )
    return route


def _direct_completion_model(model: str) -> str:
    """Remove agent transport wrappers from model IDs stored in old facets."""
    for prefix in ("dsh/", "grok-build/"):
        if model.startswith(prefix):
            return settings.normalize_queue_key(model.removeprefix(prefix))
    return model


async def _model_endpoint_catalog(org_id: str) -> tuple[ModelEndpointSummary, ...]:
    """Return the same bounded model catalog used by both listing and checks."""
    now = monotonic()
    cached = _model_catalog_cache.get(org_id)
    if cached and now - cached[0] < _MODEL_CATALOG_TTL_SECONDS:
        return cached[1]

    async with get_session() as session:
        facets = await browse_task_facets_core(session, org_id=org_id)

    model_ids = {
        _direct_completion_model(settings.normalize_queue_key(model))
        for model in (*settings.get_known_queue_keys(), *facets.models)
    }
    models: list[ModelEndpointSummary] = []
    for model in model_ids:
        provider = infer_model_provider_prefix(model)
        if provider:
            route = _provider_route(provider, model)
            models.append(
                ModelEndpointSummary(
                    model=model,
                    provider=provider,
                    route=route,
                    credential=provider_key_var(route),
                    testable=route != "cursor",
                )
            )
    models.sort(key=lambda endpoint: (endpoint.route, endpoint.model))
    result = tuple(models)
    _model_catalog_cache[org_id] = (now, result)
    return result


def _require_interactive_operator(auth: AuthContext) -> tuple[str, str]:
    require_operator_org(auth)
    if auth.method != AuthMethod.CLERK_JWT:
        raise HTTPException(
            status_code=403,
            detail="Model checks require an interactive signed-in user",
        )
    identity = auth.user_id or auth.user_email
    if not auth.org_id or not identity:
        raise HTTPException(status_code=403, detail="User identity is required")
    return auth.org_id, identity


def _begin_model_check(
    *, org_id: str, identity: str, model: str, route: str
) -> ModelEndpointCheckResponse | None:
    """Reuse a fresh result and reject only a duplicate request still in flight."""
    now = monotonic()
    cutoff = now - _MODEL_CHECK_COOLDOWN_SECONDS
    stale = [
        key for key, (started, _) in _model_check_cache.items() if started < cutoff
    ]
    for key in stale:
        del _model_check_cache[key]

    key = (org_id, identity, model, route)
    previous = _model_check_cache.get(key)
    if previous is not None:
        started, result = previous
        if result is not None:
            return result
        retry_after = max(1, round(_MODEL_CHECK_COOLDOWN_SECONDS - (now - started)))
        raise HTTPException(
            status_code=429,
            detail="This model route is already being tested",
            headers={"Retry-After": str(retry_after)},
        )
    _model_check_cache[key] = (now, None)
    return None


def _complete_model_check(
    *,
    org_id: str,
    identity: str,
    model: str,
    route: str,
    result: ModelEndpointCheckResponse,
) -> None:
    key = (org_id, identity, model, route)
    started = _model_check_cache.get(key, (monotonic(), None))[0]
    _model_check_cache[key] = (started, result)


def _safe_failure_message(
    failure: Exception, failure_kind: Literal["provider", "configuration"]
) -> str:
    """Describe a failure without copying provider-controlled exception text."""
    label = (
        "Provider request failed"
        if failure_kind == "provider"
        else "Provider configuration failed"
    )
    return f"{label} ({type(failure).__name__})"


def _audit_model_check(
    *,
    org_id: str,
    identity: str,
    result: ModelEndpointCheckResponse,
    cached: bool = False,
) -> None:
    logger.info(
        "model endpoint check org_id=%s user_id=%s model=%s route=%s ok=%s "
        "status_code=%s latency_ms=%s request_id=%s cached=%s",
        org_id,
        identity,
        result.model,
        result.route,
        result.ok,
        result.status_code,
        result.latency_ms,
        result.request_id,
        cached,
    )


@router.get("", response_model=ModelEndpointCatalogResponse)
async def list_model_endpoints(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ModelEndpointCatalogResponse:
    """List configured and previously used models for the operator org."""
    auth.require_scope(APIKeyScope.READ)
    allowed = is_operator_org(auth)
    if not allowed:
        return ModelEndpointCatalogResponse(allowed=False, models=[])
    assert auth.org_id is not None
    models = await _model_endpoint_catalog(auth.org_id)
    return ModelEndpointCatalogResponse(allowed=True, models=list(models))


@router.post("/check", response_model=ModelEndpointCheckResponse)
async def check_model_endpoint(
    request: ModelEndpointCheckRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ModelEndpointCheckResponse:
    """Send one small provider request without creating a trial or worker job."""
    org_id, identity = _require_interactive_operator(auth)
    model = settings.normalize_queue_key(request.model)
    provider = infer_model_provider_prefix(model)
    if not provider:
        raise HTTPException(status_code=422, detail="Queue key is not an LLM model")
    route = _check_route(provider, model, request.route)
    if route == "cursor":
        raise HTTPException(
            status_code=422,
            detail="Cursor models require the Cursor agent CLI and cannot be checked with a direct completion request",
        )

    catalog = await _model_endpoint_catalog(org_id)
    catalog_endpoint = next(
        (endpoint for endpoint in catalog if endpoint.model == model), None
    )
    if catalog_endpoint is None or route != catalog_endpoint.route:
        raise HTTPException(
            status_code=422,
            detail="Model route is not available in the operator catalog",
        )
    cached_result = _begin_model_check(
        org_id=org_id,
        identity=identity,
        model=model,
        route=route,
    )
    if cached_result is not None:
        _audit_model_check(
            org_id=org_id,
            identity=identity,
            result=cached_result,
            cached=True,
        )
        return cached_result
    credential = provider_key_var(route)

    started = monotonic()
    resolved_model = model
    try:
        kwargs = (
            {"max_completion_tokens": 32}
            if route in {OPENAI_PROVIDER_AZURE, "openai"}
            else {"max_tokens": 32}
        )
        if provider == "bedrock":
            resolved_model = f"bedrock/{model}"
        elif provider == "anthropic-hdo":
            bare_model = anthropic_hdo_bare_model_id(model)
            api_model = to_anthropic_api_model_id(bare_model) or bare_model
            hdo_api_key = (settings.anthropic_hdo_api_key or "").strip()
            if not hdo_api_key:
                raise RuntimeError("ANTHROPIC_HDO_API_KEY is missing")
            resolved_model = f"anthropic/{api_model}"
            kwargs["api_key"] = hdo_api_key
        elif provider == "fireworks":
            fireworks_api_key = (settings.fireworks_api_key or "").strip()
            if not fireworks_api_key:
                raise RuntimeError("FIREWORKS_API_KEY is missing")
            api_model = fireworks_api_model_id(fireworks_bare_model_id(model))
            resolved_model = f"fireworks_ai/{api_model}"
            kwargs["api_key"] = fireworks_api_key
        elif provider == "meta":
            meta_api_key = (settings.meta_api_key or "").strip()
            if not meta_api_key:
                raise RuntimeError("META_API_KEY is missing")
            resolved_model = f"openai/{meta_bare_model_id(model)}"
            kwargs.update(
                {
                    "api_key": meta_api_key,
                    "api_base": settings.meta_base_url.rstrip("/"),
                }
            )
        elif provider == "gemini" and model.startswith("google/"):
            resolved_model = f"gemini/{model.split('/', 1)[1]}"
        elif route == OPENAI_PROVIDER_AZURE:
            azure = settings.require_azure_openai_config()
            deployment = (
                model.split("/", 1)[1]
                if provider in {"azure", "azure_openai"} and "/" in model
                else settings.resolve_azure_openai_deployment(model)
            )
            resolved_model = f"azure/{deployment}"
            kwargs.update(
                {
                    "api_key": azure["api_key"],
                    "api_base": azure["endpoint"]
                    .rstrip("/")
                    .removesuffix("/openai/v1"),
                    "api_version": azure["api_version"],
                }
            )
    except (ValueError, RuntimeError) as caught:
        failure = caught
        failure_kind: Literal["provider", "configuration"] = "configuration"
    else:
        # This is deliberately narrower than a trial: it exercises LiteLLM's
        # completion transport, not an agent CLI or sandbox startup.
        import litellm
        from openai import OpenAIError

        try:
            completion = await litellm.acompletion(
                model=resolved_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Reply with one short sentence naming the model you are.",
                    }
                ],
                timeout=15,
                **kwargs,
            )
        except OpenAIError as caught:
            failure = caught
            failure_kind = "provider"
        else:
            # Unexpected response shapes are integration defects and remain 500s.
            content = completion.choices[0].message.content
            result = ModelEndpointCheckResponse(
                ok=True,
                model=model,
                resolved_model=resolved_model,
                provider=provider,
                route=route,
                credential=credential,
                latency_ms=round((monotonic() - started) * 1000),
                response=content if isinstance(content, str) else str(content or ""),
                request_id=(
                    str(completion.id) if getattr(completion, "id", None) else None
                ),
            )
            _complete_model_check(
                org_id=org_id,
                identity=identity,
                model=model,
                route=route,
                result=result,
            )
            _audit_model_check(org_id=org_id, identity=identity, result=result)
            return result

    raw_status = getattr(failure, "status_code", None)
    status_code = (
        int(raw_status)
        if isinstance(raw_status, int | str) and str(raw_status).isdigit()
        else None
    )
    request_id = str(getattr(failure, "request_id", "") or "").strip()[:200] or None
    result = ModelEndpointCheckResponse(
        ok=False,
        model=model,
        resolved_model=resolved_model,
        provider=provider,
        route=route,
        credential=credential,
        failure_kind=failure_kind,
        status_code=status_code,
        latency_ms=round((monotonic() - started) * 1000),
        error=_safe_failure_message(failure, failure_kind),
        request_id=request_id,
    )
    _complete_model_check(
        org_id=org_id,
        identity=identity,
        model=model,
        route=route,
        result=result,
    )
    _audit_model_check(org_id=org_id, identity=identity, result=result)
    return result
