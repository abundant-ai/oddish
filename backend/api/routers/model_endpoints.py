"""Operator model catalog and direct provider completion checks."""

from __future__ import annotations

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
from oddish.db import get_session
from pydantic import BaseModel, Field, field_validator

from auth import AuthContext, require_auth
from auth.permissions import is_operator_org, require_operator_org
from models import APIKeyScope

router = APIRouter(prefix="/models", tags=["Models"])


class ModelEndpointSummary(BaseModel):
    model: str
    provider: str


class ModelEndpointCatalogResponse(BaseModel):
    allowed: bool
    models: list[ModelEndpointSummary]


class ModelEndpointCheckRequest(BaseModel):
    model: str = Field(min_length=1, max_length=512)

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model must not be blank")
        return value


class ModelEndpointCheckResponse(BaseModel):
    ok: bool
    model: str
    resolved_model: str
    provider: str
    transport: Literal["litellm_completion"] = "litellm_completion"
    failure_kind: Literal["provider", "configuration"] | None = None
    status_code: int | None = None
    latency_ms: int
    response: str | None = None
    error: str | None = None
    request_id: str | None = None


@router.get("", response_model=ModelEndpointCatalogResponse)
async def list_model_endpoints(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ModelEndpointCatalogResponse:
    """List configured and previously used models for the operator org."""
    auth.require_scope(APIKeyScope.READ)
    allowed = is_operator_org(auth)
    if not allowed:
        return ModelEndpointCatalogResponse(allowed=False, models=[])

    async with get_session() as session:
        facets = await browse_task_facets_core(session, org_id=auth.org_id)

    model_ids = {
        settings.normalize_queue_key(model)
        for model in (*settings.get_known_queue_keys(), *facets.models)
    }
    models = []
    for model in sorted(model_ids):
        provider = infer_model_provider_prefix(model)
        if provider:
            models.append(ModelEndpointSummary(model=model, provider=provider))
    return ModelEndpointCatalogResponse(allowed=True, models=models)


@router.post("/check", response_model=ModelEndpointCheckResponse)
async def check_model_endpoint(
    request: ModelEndpointCheckRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ModelEndpointCheckResponse:
    """Send one small provider request without creating a trial or worker job."""
    auth.require_scope(APIKeyScope.TASKS)
    require_operator_org(auth)
    model = settings.normalize_queue_key(request.model)
    provider = infer_model_provider_prefix(model)
    if not provider:
        raise HTTPException(status_code=422, detail="Queue key is not an LLM model")

    started = monotonic()
    resolved_model = model
    try:
        kwargs = (
            {"max_completion_tokens": 32}
            if provider == "openai"
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
        elif (
            provider == "openai"
            and settings.get_openai_provider() == OPENAI_PROVIDER_AZURE
        ):
            azure = settings.require_azure_openai_config()
            resolved_model = f"azure/{settings.resolve_azure_openai_deployment(model)}"
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
            return ModelEndpointCheckResponse(
                ok=True,
                model=model,
                resolved_model=resolved_model,
                provider=provider,
                latency_ms=round((monotonic() - started) * 1000),
                response=content if isinstance(content, str) else str(content or ""),
                request_id=(
                    str(completion.id) if getattr(completion, "id", None) else None
                ),
            )

    raw_status = getattr(failure, "status_code", None)
    status_code = (
        int(raw_status)
        if isinstance(raw_status, int | str) and str(raw_status).isdigit()
        else None
    )
    message = " ".join(str(failure).split())[:500]
    return ModelEndpointCheckResponse(
        ok=False,
        model=model,
        resolved_model=resolved_model,
        provider=provider,
        failure_kind=failure_kind,
        status_code=status_code,
        latency_ms=round((monotonic() - started) * 1000),
        error=f"{type(failure).__name__}: {message}",
        request_id=str(getattr(failure, "request_id", "") or "") or None,
    )
