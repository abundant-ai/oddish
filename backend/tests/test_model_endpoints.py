"""Tests for the model catalog and direct provider completion checks."""

from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from openai import OpenAIError

from api.app import create_app
from api.routers import model_endpoints as model_endpoints_router
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole


_TEST_MODELS = {
    "anthropic-hdo/claude-sonnet-4-6",
    "cursor/composer-2.5",
    "deepseek/deepseek-v4-flash",
    "fireworks/minimax-m3",
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-sonnet-5",
    "google/gemini-3.5-flash",
    "google/gemini-3.7-flash",
    "meta/super_nova_ext",
    "minimax/minimax-m3",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.6-sol",
    "vertex_ai/gemini-3-pro-preview",
    "xai/grok-code-fast-1",
    "xai/v9m-rl-learnability-tp8",
}


def _app(auth: AuthContext | None = None):
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: (
        auth
        or AuthContext(
            method=AuthMethod.CLERK_JWT,
            org_id="org-1",
            user_id="user-1",
            user_role=UserRole.MEMBER,
        )
    )
    return app


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-1")
    settings_type = type(model_endpoints_router.settings)
    monkeypatch.setattr(
        settings_type, "get_known_queue_keys", lambda _self: _TEST_MODELS
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def empty_facets(_session, *, org_id):
        assert org_id == "org-1"
        return SimpleNamespace(models=[])

    monkeypatch.setattr(model_endpoints_router, "get_session", fake_get_session)
    monkeypatch.setattr(model_endpoints_router, "browse_task_facets_core", empty_facets)
    model_endpoints_router._model_catalog_cache.clear()
    model_endpoints_router._model_check_cache.clear()
    yield
    model_endpoints_router._model_catalog_cache.clear()
    model_endpoints_router._model_check_cache.clear()


@pytest.mark.asyncio
async def test_model_catalog_unions_configured_and_previously_used_models(monkeypatch):
    settings_type = type(model_endpoints_router.settings)
    monkeypatch.setattr(settings_type, "get_openai_provider", lambda _self: "openai")
    monkeypatch.setattr(
        settings_type,
        "get_known_queue_keys",
        lambda _self: {
            "global.anthropic.claude-opus-5",
            "openai/gpt-5.4-mini",
            "task_expand",
        },
    )

    session = object()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_browse_task_facets_core(received_session, *, org_id):
        assert received_session is session
        assert org_id == "org-1"
        return SimpleNamespace(
            models=[
                "global.anthropic.claude-opus-5",
                "cursor/composer-2.5",
                "dsh/deepseek-v4-flash",
                "google/gemini-3.7-flash",
                "grok-build/xai/v9m-rl-learnability-tp8",
                "openai/gpt-5.6-sol",
                "vertex_ai/gemini-3-pro-preview",
                "nop_oracle",
            ]
        )

    monkeypatch.setattr(model_endpoints_router, "get_session", fake_get_session)
    monkeypatch.setattr(
        model_endpoints_router,
        "browse_task_facets_core",
        fake_browse_task_facets_core,
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/models")

    assert response.status_code == 200
    assert response.json() == {
        "allowed": True,
        "models": [
            {
                "credential": "AWS_BEARER_TOKEN_BEDROCK",
                "model": "global.anthropic.claude-opus-5",
                "provider": "bedrock",
                "route": "bedrock",
                "testable": True,
            },
            {
                "credential": "CURSOR_API_KEY",
                "model": "cursor/composer-2.5",
                "provider": "cursor",
                "route": "cursor",
                "testable": False,
            },
            {
                "credential": "DEEPSEEK_API_KEY",
                "model": "deepseek/deepseek-v4-flash",
                "provider": "deepseek",
                "route": "deepseek",
                "testable": True,
            },
            {
                "credential": "GEMINI_API_KEY",
                "model": "google/gemini-3.7-flash",
                "provider": "gemini",
                "route": "gemini",
                "testable": True,
            },
            {
                "credential": "OPENAI_API_KEY",
                "model": "openai/gpt-5.4-mini",
                "provider": "openai",
                "route": "openai",
                "testable": True,
            },
            {
                "credential": "OPENAI_API_KEY",
                "model": "openai/gpt-5.6-sol",
                "provider": "openai",
                "route": "openai",
                "testable": True,
            },
            {
                "credential": "VERTEXAI_PROJECT",
                "model": "vertex_ai/gemini-3-pro-preview",
                "provider": "gemini",
                "route": "vertex_ai",
                "testable": True,
            },
            {
                "credential": "XAI_API_KEY",
                "model": "xai/v9m-rl-learnability-tp8",
                "provider": "xai",
                "route": "xai",
                "testable": True,
            },
        ],
    }


@pytest.mark.asyncio
async def test_model_catalog_hides_models_outside_operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/models")

    assert response.status_code == 200
    assert response.json() == {"allowed": False, "models": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", [APIKeyScope.READ, APIKeyScope.TASKS, APIKeyScope.FULL]
)
async def test_model_check_rejects_api_key_auth(scope):
    api_key_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        scope=scope,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(api_key_auth)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "openai/gpt-5.4-mini"}
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Model checks require an interactive signed-in user"
    )


@pytest.mark.asyncio
async def test_model_endpoint_returns_provider_response(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "gemini/gemini-3.5-flash"
        assert kwargs["max_tokens"] == 32
        return SimpleNamespace(
            id="request-123",
            choices=[SimpleNamespace(message=SimpleNamespace(content="I am Gemini."))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": " Google/Gemini-3.5-Flash "}
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["latency_ms"], int)
    assert payload["ok"] is True
    assert payload["model"] == "google/gemini-3.5-flash"
    assert payload["resolved_model"] == "gemini/gemini-3.5-flash"
    assert payload["provider"] == "gemini"
    assert payload["transport"] == "litellm_completion"
    assert payload["failure_kind"] is None
    assert payload["response"] == "I am Gemini."
    assert payload["request_id"] == "request-123"


@pytest.mark.asyncio
async def test_model_endpoint_adds_litellm_bedrock_provider_prefix(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == ("bedrock/global.anthropic.claude-sonnet-5")
        return SimpleNamespace(
            id="bedrock-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Claude."))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={"model": "global.anthropic.claude-sonnet-5"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["model"] == "global.anthropic.claude-sonnet-5"
    assert payload["resolved_model"] == ("bedrock/global.anthropic.claude-sonnet-5")
    assert payload["provider"] == "bedrock"


@pytest.mark.asyncio
async def test_model_endpoint_rejects_hidden_provider_override():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={
                "model": "global.anthropic.claude-sonnet-5",
                "route": "anthropic",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Route 'anthropic' is not valid for 'bedrock'; expected bedrock"
    )


@pytest.mark.asyncio
async def test_model_endpoint_rejects_incompatible_provider_route():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={"model": "xai/grok-code-fast-1", "route": "azure"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Route 'azure' is not valid for 'xai'; expected xai"
    )


@pytest.mark.asyncio
async def test_model_endpoint_rejects_cursor_cli_model():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={"model": "cursor/composer-2.5", "route": "cursor"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Cursor models require the Cursor agent CLI and cannot be checked with a direct completion request"
    )


@pytest.mark.asyncio
async def test_model_endpoint_remaps_anthropic_hdo_and_uses_hdo_key(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "anthropic/claude-sonnet-4-6"
        assert kwargs["api_key"] == "hdo-key"
        assert kwargs["max_tokens"] == 32
        return SimpleNamespace(
            id="hdo-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Claude."))],
        )

    monkeypatch.setattr(
        model_endpoints_router.settings, "anthropic_hdo_api_key", "hdo-key"
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={"model": "anthropic-hdo/claude-sonnet-4-6"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "anthropic/claude-sonnet-4-6"
    assert payload["provider"] == "anthropic-hdo"


@pytest.mark.asyncio
async def test_model_endpoint_remaps_fireworks_for_litellm(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == ("fireworks_ai/accounts/fireworks/models/minimax-m3")
        assert kwargs["api_key"] == "fireworks-key"
        assert kwargs["max_tokens"] == 32
        return SimpleNamespace(
            id="fireworks-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="MiniMax."))],
        )

    monkeypatch.setattr(
        model_endpoints_router.settings, "fireworks_api_key", "fireworks-key"
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "fireworks/minimax-m3"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == (
        "fireworks_ai/accounts/fireworks/models/minimax-m3"
    )
    assert payload["provider"] == "fireworks"


@pytest.mark.asyncio
async def test_model_endpoint_remaps_meta_to_compatible_openai_api(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "openai/super_nova_ext"
        assert kwargs["api_key"] == "meta-key"
        assert kwargs["api_base"] == "https://meta.example/v1"
        assert kwargs["max_tokens"] == 32
        return SimpleNamespace(
            id="meta-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Meta."))],
        )

    monkeypatch.setattr(model_endpoints_router.settings, "meta_api_key", "meta-key")
    monkeypatch.setattr(
        model_endpoints_router.settings,
        "meta_base_url",
        "https://meta.example/v1/",
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "meta/super_nova_ext"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "openai/super_nova_ext"
    assert payload["provider"] == "meta"


@pytest.mark.asyncio
async def test_model_endpoint_uses_azure_resource_root_for_litellm(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "azure/oddish-gpt"
        assert kwargs["max_completion_tokens"] == 32
        assert "max_tokens" not in kwargs
        assert kwargs["api_key"] == "azure-key"
        assert kwargs["api_base"] == "https://example.openai.azure.com"
        assert kwargs["api_version"] == "2025-01-01-preview"
        assert "base_url" not in kwargs
        return SimpleNamespace(
            id="azure-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="GPT."))],
        )

    settings_type = type(model_endpoints_router.settings)
    monkeypatch.setattr(settings_type, "get_openai_provider", lambda _self: "azure")
    monkeypatch.setattr(
        settings_type,
        "require_azure_openai_config",
        lambda _self: {
            "api_key": "azure-key",
            "endpoint": "https://example.openai.azure.com/openai/v1/",
            "api_version": "2025-01-01-preview",
        },
    )
    monkeypatch.setattr(
        settings_type,
        "resolve_azure_openai_deployment",
        lambda _self, model: "oddish-gpt" if model == "openai/gpt-5.4-mini" else None,
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "openai/gpt-5.4-mini"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "azure/oddish-gpt"
    assert payload["provider"] == "openai"
    assert payload["route"] == "azure"
    assert payload["credential"] == "AZURE_OPENAI_API_KEY"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_model_endpoint_surfaces_upstream_http_status(monkeypatch, status_code):
    class BadRequestError(OpenAIError):
        pass

    error = BadRequestError("The model is unavailable")
    error.status_code = status_code
    error.request_id = f"provider-request-{status_code}"

    async def completion(**_kwargs):
        raise error

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "xai/grok-code-fast-1"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["latency_ms"], int)
    assert payload["ok"] is False
    assert payload["failure_kind"] == "provider"
    assert payload["transport"] == "litellm_completion"
    assert payload["status_code"] == status_code
    assert payload["error"] == "Provider request failed (BadRequestError)"
    assert payload["request_id"] == f"provider-request-{status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["Timeout", "APIConnectionError"])
async def test_model_endpoint_surfaces_transport_failures(monkeypatch, error_name):
    class Timeout(OpenAIError):
        pass

    class APIConnectionError(OpenAIError):
        pass

    error_type = {
        "Timeout": Timeout,
        "APIConnectionError": APIConnectionError,
    }[error_name]

    async def completion(**_kwargs):
        raise error_type("The provider did not respond")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "xai/grok-code-fast-1"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["failure_kind"] == "provider"
    assert payload["status_code"] is None
    assert payload["error"] == f"Provider request failed ({error_name})"


@pytest.mark.asyncio
async def test_model_endpoint_never_returns_or_logs_provider_exception_secrets(
    monkeypatch, caplog
):
    leaked_secret = "sk-provider-secret-that-must-not-reach-the-browser"
    caplog.set_level("INFO", logger="api.routers.model_endpoints")

    class ProviderError(OpenAIError):
        pass

    async def completion(**_kwargs):
        raise ProviderError(f"Authorization: Bearer {leaked_secret}")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "xai/grok-code-fast-1"}
        )

    assert response.status_code == 200
    assert response.json()["error"] == "Provider request failed (ProviderError)"
    assert leaked_secret not in response.text
    assert "model endpoint check" in caplog.text
    assert leaked_secret not in caplog.text


@pytest.mark.asyncio
async def test_model_endpoint_does_not_hide_internal_errors(monkeypatch):
    async def completion(**_kwargs):
        raise RuntimeError("unexpected integration defect")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "minimax/minimax-m3"}
        )

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_model_endpoint_reports_configuration_errors(monkeypatch):
    def missing_config(_settings):
        raise RuntimeError("AZURE_OPENAI_API_KEY is missing")

    settings_type = type(model_endpoints_router.settings)
    monkeypatch.setattr(settings_type, "get_openai_provider", lambda _self: "azure")
    monkeypatch.setattr(settings_type, "require_azure_openai_config", missing_config)

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "openai/gpt-5.4-mini"}
        )

    assert response.status_code == 200
    assert response.json()["failure_kind"] == "configuration"
    assert response.json()["error"] == "Provider configuration failed (RuntimeError)"


@pytest.mark.asyncio
async def test_model_endpoint_rejects_model_outside_catalog(monkeypatch):
    async def completion(**_kwargs):
        raise AssertionError("unlisted models must not reach the provider")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "openai/unlisted-expensive-model"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Model route is not available in the operator catalog"
    )


@pytest.mark.asyncio
async def test_model_endpoint_reuses_recent_model_route_result(monkeypatch):
    calls = 0

    async def completion(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            id="request-123",
            choices=[SimpleNamespace(message=SimpleNamespace(content="I am Grok."))],
        )

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        first = await client.post(
            "/models/check", json={"model": "xai/grok-code-fast-1"}
        )
        repeated = await client.post(
            "/models/check", json={"model": "xai/grok-code-fast-1"}
        )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert calls == 1


def test_model_endpoint_rejects_duplicate_while_first_check_is_running():
    check = {
        "org_id": "org-1",
        "identity": "user-1",
        "model": "xai/grok-code-fast-1",
        "route": "xai",
    }
    assert model_endpoints_router._begin_model_check(**check) is None

    with pytest.raises(HTTPException) as caught:
        model_endpoints_router._begin_model_check(**check)

    assert caught.value.status_code == 429
    assert caught.value.detail == "This model route is already being tested"
    assert caught.value.headers == {"Retry-After": "20"}


def test_model_endpoint_result_ttl_starts_when_provider_check_completes():
    check = {
        "org_id": "org-1",
        "identity": "user-1",
        "model": "xai/grok-code-fast-1",
        "route": "xai",
    }
    key = (
        check["org_id"],
        check["identity"],
        check["model"],
        check["route"],
    )
    model_endpoints_router._model_check_cache[key] = (
        model_endpoints_router.monotonic() - 10,
        None,
    )
    result = model_endpoints_router.ModelEndpointCheckResponse(
        ok=True,
        model=check["model"],
        resolved_model=check["model"],
        provider="xai",
        route=check["route"],
        credential="XAI_API_KEY",
        latency_ms=10_000,
        response="I am Grok.",
    )

    model_endpoints_router._complete_model_check(**check, result=result)

    assert model_endpoints_router._begin_model_check(**check) is result


@pytest.mark.asyncio
async def test_model_endpoint_rejects_non_model_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post("/models/check", json={"model": "task_expand"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Queue key is not an LLM model"


@pytest.mark.asyncio
async def test_model_endpoint_requires_operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "minimax/minimax-m3"}
        )

    assert response.status_code == 403
