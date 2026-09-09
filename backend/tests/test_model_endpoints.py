"""Tests for the model catalog and direct provider completion checks."""

import asyncio
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

# Synthetic model IDs retain only the prefixes needed to exercise provider routing.
_TEST_MODELS = {
    "anthropic-hdo/test-model-01",
    "cursor/test-model-02",
    "deepseek/deepseek-test-model-03",
    "fireworks/test-model-04",
    "global.anthropic.test-model-05",
    "global.anthropic.test-model-06",
    "google/test-model-07",
    "google/test-model-08",
    "meta/test-model-09",
    "minimax/test-model-04",
    "openai/test-model-10",
    "openai/test-model-11",
    "vertex_ai/test-model-12",
    "xai/test-model-13",
    "xai/test-model-14",
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
    monkeypatch.setattr(model_endpoints_router, "provider_models", lambda: set())
    monkeypatch.setattr(
        model_endpoints_router, "credential_configured", lambda _route: False
    )
    monkeypatch.setattr(model_endpoints_router.settings, "model_catalog", [])
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
            "global.anthropic.test-model-05",
            "openai/test-model-10",
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
                "global.anthropic.test-model-05",
                "cursor/test-model-02",
                "dsh/deepseek-test-model-03",
                "google/test-model-08",
                "grok-build/xai/test-model-14",
                "openai/test-model-11",
                "vertex_ai/test-model-12",
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
                "source": "deployment",
                "credential_configured": False,
                "credential": "AWS_BEARER_TOKEN_BEDROCK",
                "model": "global.anthropic.test-model-05",
                "provider": "bedrock",
                "route": "bedrock",
                "testable": True,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "CURSOR_API_KEY",
                "model": "cursor/test-model-02",
                "provider": "cursor",
                "route": "cursor",
                "testable": False,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "DEEPSEEK_API_KEY",
                "model": "deepseek/deepseek-test-model-03",
                "provider": "deepseek",
                "route": "deepseek",
                "testable": True,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "GEMINI_API_KEY",
                "model": "google/test-model-08",
                "provider": "gemini",
                "route": "gemini",
                "testable": True,
            },
            {
                "source": "deployment",
                "credential_configured": False,
                "credential": "OPENAI_API_KEY",
                "model": "openai/test-model-10",
                "provider": "openai",
                "route": "openai",
                "testable": True,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "OPENAI_API_KEY",
                "model": "openai/test-model-11",
                "provider": "openai",
                "route": "openai",
                "testable": True,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "VERTEXAI_PROJECT",
                "model": "vertex_ai/test-model-12",
                "provider": "gemini",
                "route": "vertex_ai",
                "testable": True,
            },
            {
                "source": "previously_used",
                "credential_configured": False,
                "credential": "XAI_API_KEY",
                "model": "xai/test-model-14",
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
            "/models/check", json={"model": "openai/test-model-10"}
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Model checks require an interactive signed-in user"
    )


@pytest.mark.asyncio
async def test_model_endpoint_returns_provider_response(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "gemini/test-model-07"
        assert kwargs["max_tokens"] == 1024
        return SimpleNamespace(
            id="request-123",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": " Google/Test-Model-07 "}
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["latency_ms"], int)
    assert payload["ok"] is True
    assert payload["model"] == "google/test-model-07"
    assert payload["resolved_model"] == "gemini/test-model-07"
    assert payload["provider"] == "gemini"
    assert payload["transport"] == "litellm_completion"
    assert payload["failure_kind"] is None
    assert payload["response"] == "Test completion."
    assert payload["request_id"] == "request-123"


@pytest.mark.asyncio
async def test_model_endpoint_adds_litellm_bedrock_provider_prefix(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == ("bedrock/global.anthropic.test-model-06")
        return SimpleNamespace(
            id="bedrock-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={"model": "global.anthropic.test-model-06"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["model"] == "global.anthropic.test-model-06"
    assert payload["resolved_model"] == ("bedrock/global.anthropic.test-model-06")
    assert payload["provider"] == "bedrock"


@pytest.mark.asyncio
async def test_model_endpoint_rejects_hidden_provider_override():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check",
            json={
                "model": "global.anthropic.test-model-06",
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
            json={"model": "xai/test-model-13", "route": "azure"},
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
            json={"model": "cursor/test-model-02", "route": "cursor"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Cursor models require the Cursor agent CLI and cannot be checked with a direct completion request"
    )


@pytest.mark.asyncio
async def test_model_endpoint_remaps_anthropic_hdo_and_uses_hdo_key(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "anthropic/test-model-01"
        assert kwargs["api_key"] == "hdo-key"
        assert kwargs["max_tokens"] == 1024
        return SimpleNamespace(
            id="hdo-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
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
            json={"model": "anthropic-hdo/test-model-01"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "anthropic/test-model-01"
    assert payload["provider"] == "anthropic-hdo"


@pytest.mark.asyncio
async def test_model_endpoint_remaps_fireworks_for_litellm(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == (
            "fireworks_ai/accounts/fireworks/models/test-model-04"
        )
        assert kwargs["api_key"] == "fireworks-key"
        assert kwargs["max_tokens"] == 1024
        return SimpleNamespace(
            id="fireworks-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
        )

    monkeypatch.setattr(
        model_endpoints_router.settings, "fireworks_api_key", "fireworks-key"
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "fireworks/test-model-04"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == (
        "fireworks_ai/accounts/fireworks/models/test-model-04"
    )
    assert payload["provider"] == "fireworks"


@pytest.mark.asyncio
async def test_model_endpoint_remaps_meta_to_compatible_openai_api(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "openai/test-model-09"
        assert kwargs["api_key"] == "meta-key"
        assert kwargs["api_base"] == "https://meta.example/v1"
        assert kwargs["max_tokens"] == 1024
        return SimpleNamespace(
            id="meta-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
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
            "/models/check", json={"model": "meta/test-model-09"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "openai/test-model-09"
    assert payload["provider"] == "meta"


@pytest.mark.asyncio
async def test_model_endpoint_uses_azure_resource_root_for_litellm(monkeypatch):
    async def completion(**kwargs):
        assert kwargs["model"] == "azure/test-deployment"
        assert kwargs["max_completion_tokens"] == 1024
        assert "max_tokens" not in kwargs
        assert kwargs["api_key"] == "azure-key"
        assert kwargs["api_base"] == "https://example.openai.azure.com"
        assert kwargs["api_version"] == "2025-01-01-preview"
        assert "base_url" not in kwargs
        return SimpleNamespace(
            id="azure-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
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
        lambda _self, model: (
            "test-deployment" if model == "openai/test-model-10" else None
        ),
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "openai/test-model-10"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "azure/test-deployment"
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
            "/models/check", json={"model": "xai/test-model-13"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["latency_ms"], int)
    assert payload["ok"] is False
    assert payload["failure_kind"] == "provider"
    assert payload["transport"] == "litellm_completion"
    assert payload["status_code"] == status_code
    assert f"HTTP {status_code}" in payload["error"]
    assert "credential" in payload["error"]
    if status_code == 404:
        assert "does not establish a provider outage" in payload["error"]
    assert payload["request_id"] == f"provider-request-{status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_name", "exception_attribute"),
    [
        ("APIError", "LiteLLMAPIError"),
        ("Timeout", "LiteLLMTimeout"),
        ("APIConnectionError", "LiteLLMAPIConnectionError"),
    ],
)
async def test_model_endpoint_surfaces_litellm_failures(
    monkeypatch, error_name, exception_attribute
):
    error_type = type(error_name, (Exception,), {})
    monkeypatch.setattr(model_endpoints_router, exception_attribute, error_type)

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
            "/models/check", json={"model": "xai/test-model-13"}
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
            "/models/check", json={"model": "xai/test-model-13"}
        )

    assert response.status_code == 200
    assert response.json()["error"] == "Provider request failed (ProviderError)"
    assert leaked_secret not in response.text
    assert "model endpoint check" in caplog.text
    assert leaked_secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "response", "cancellation"])
async def test_model_endpoint_releases_failed_check_for_immediate_retry(
    monkeypatch, failure
):
    calls = 0

    async def completion(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "exception":
                raise RuntimeError("unexpected integration defect")
            if failure == "cancellation":
                raise asyncio.CancelledError()
            return SimpleNamespace(choices=[])
        return SimpleNamespace(
            id="retry-request",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Retry succeeded"))
            ],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        if failure == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await model_endpoints_router.check_model_endpoint(
                    model_endpoints_router.ModelEndpointCheckRequest(
                        model="minimax/test-model-04"
                    ),
                    AuthContext(
                        method=AuthMethod.CLERK_JWT,
                        org_id="org-1",
                        user_id="user-1",
                        user_role=UserRole.MEMBER,
                    ),
                )
        else:
            response = await client.post(
                "/models/check", json={"model": "minimax/test-model-04"}
            )
            assert response.status_code == 500
        retry = await client.post(
            "/models/check", json={"model": "minimax/test-model-04"}
        )
        cached = await client.post(
            "/models/check", json={"model": "minimax/test-model-04"}
        )

    assert retry.status_code == 200
    assert retry.json()["response"] == "Retry succeeded"
    assert cached.json() == retry.json()
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("old_request_fails", [True, False])
async def test_expired_model_check_preserves_newer_result(
    monkeypatch, old_request_fails
):
    now = 100.0
    monkeypatch.setattr(model_endpoints_router, "monotonic", lambda: now)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def completion(**_kwargs):
        nonlocal calls
        calls += 1
        request_id = f"request-{calls}"
        if calls == 1:
            first_started.set()
            await release_first.wait()
            if old_request_fails:
                raise RuntimeError("old request failed")
        return SimpleNamespace(
            id=request_id,
            choices=[SimpleNamespace(message=SimpleNamespace(content=request_id))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    request = model_endpoints_router.ModelEndpointCheckRequest(
        model="minimax/test-model-04"
    )
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org-1",
        user_id="user-1",
        user_role=UserRole.MEMBER,
    )
    first = asyncio.create_task(
        model_endpoints_router.check_model_endpoint(request, auth)
    )
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        now += model_endpoints_router._MODEL_CHECK_IN_FLIGHT_TTL_SECONDS
        newer = await model_endpoints_router.check_model_endpoint(request, auth)
        release_first.set()
        if old_request_fails:
            with pytest.raises(RuntimeError, match="old request failed"):
                await first
        else:
            assert (await first).request_id == "request-1"
        cached = await model_endpoints_router.check_model_endpoint(request, auth)
        assert cached == newer
        assert cached.request_id == "request-2"
        assert calls == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


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
            "/models/check", json={"model": "openai/test-model-10"}
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
    now = 100.0
    monkeypatch.setattr(model_endpoints_router, "monotonic", lambda: now)

    async def completion(**_kwargs):
        nonlocal calls, now
        calls += 1
        now += 10.0
        return SimpleNamespace(
            id="request-123",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Test completion."))
            ],
        )

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=completion),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        first = await client.post("/models/check", json={"model": "xai/test-model-13"})
        repeated = await client.post(
            "/models/check", json={"model": "xai/test-model-13"}
        )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert calls == 1


def test_model_endpoint_rejects_duplicate_while_first_check_is_running():
    check = {
        "org_id": "org-1",
        "identity": "user-1",
        "model": "xai/test-model-13",
        "route": "xai",
    }
    assert model_endpoints_router._begin_model_check(**check) is None

    with pytest.raises(HTTPException) as caught:
        model_endpoints_router._begin_model_check(**check)

    assert caught.value.status_code == 429
    assert caught.value.detail == "This model route is already being tested"
    assert caught.value.headers == {"Retry-After": "20"}


@pytest.mark.asyncio
async def test_model_endpoint_rejects_non_model_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post("/models/check", json={"model": "task_expand"})

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Model route is not available in the operator catalog"
    )


@pytest.mark.asyncio
async def test_model_endpoint_requires_operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "minimax/test-model-04"}
        )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator_org_id, allowed", [("org-1", True), ("org-2", False), ("", False)]
)
async def test_model_access_does_not_load_catalog(
    monkeypatch, operator_org_id, allowed
):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", operator_org_id)

    async def unexpected_catalog(_org_id):
        pytest.fail("Access discovery must not load the catalog")

    monkeypatch.setattr(
        model_endpoints_router, "_model_endpoint_catalog", unexpected_catalog
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/models/access")
    assert response.status_code == 200
    assert response.json() == {"allowed": allowed}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", " \n\t"])
async def test_model_endpoint_requires_visible_text(monkeypatch, content):
    calls = 0

    async def completion(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["max_tokens"] == 1024
        assert kwargs["messages"] == [
            {
                "role": "user",
                "content": "Reply with exactly this text: Hello from Oddish.",
            }
        ]
        return SimpleNamespace(
            id="empty-text-request",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content, reasoning_content="Internal reasoning"
                    ),
                    finish_reason="length",
                )
            ],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "xai/test-model-13"}
        )
        cached = await client.post("/models/check", json={"model": "xai/test-model-13"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["response"] == ""
    assert payload["failure_kind"] == "provider"
    assert payload["error"] == "Provider returned no text response."
    assert payload["request_id"] == "empty-text-request"
    assert cached.json() == payload
    assert calls == 1


@pytest.mark.asyncio
async def test_model_endpoint_accepts_nonblank_text_without_exact_prompt_match(
    monkeypatch,
):
    async def completion(**_kwargs):
        return SimpleNamespace(
            id="text-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="  Hello! \n"))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": "xai/test-model-13"}
        )

    payload = response.json()
    assert payload["ok"] is True
    assert payload["response"] == "Hello!"
    assert payload["error"] is None
    assert payload["failure_kind"] is None


@pytest.mark.asyncio
async def test_catalog_distinguishes_legacy_names_from_configured_runtime_models(
    monkeypatch,
):
    monkeypatch.setattr(
        type(model_endpoints_router.settings),
        "get_known_queue_keys",
        lambda _self: {"claude-sonnet-4-6"},
    )

    async def historical_facets(_session, *, org_id):
        return SimpleNamespace(
            models=[
                "anthropic/claude-sonnet-4-6-20250514",
                "anthropic/opus-5",
                "global.anthropic.claude-sonnet-4-6",
            ]
        )

    monkeypatch.setattr(
        model_endpoints_router, "browse_task_facets_core", historical_facets
    )
    catalog = await model_endpoints_router._model_endpoint_catalog("org-1")
    configured = [entry for entry in catalog if entry.source == "deployment"]
    assert len(configured) == 1
    assert configured[0].model == "global.anthropic.claude-sonnet-4-6"
    assert configured[0].route == "bedrock"
    assert {entry.model for entry in catalog if entry.source == "previously_used"} == {
        "anthropic/claude-sonnet-4-6-20250514",
        "anthropic/opus-5",
    }


@pytest.mark.parametrize(
    "status_code, expected",
    [
        (400, "supported request parameters"),
        (401, "API key"),
        (403, "not permitted"),
        (404, "does not establish a provider outage"),
        (429, "rate limit or quota"),
        (503, "server error"),
    ],
)
def test_provider_failure_explanations_do_not_copy_exception_text(
    status_code, expected
):
    failure = OpenAIError("Authorization: Bearer secret-that-must-not-be-shown")
    message = model_endpoints_router._safe_failure_message(
        failure, "provider", status_code
    )
    assert expected in message
    assert str(status_code) in message
    assert "secret-that-must-not-be-shown" not in message


@pytest.mark.asyncio
async def test_catalog_adds_provider_models_and_private_entries_without_limits(
    monkeypatch,
):
    monkeypatch.setattr(
        type(model_endpoints_router.settings),
        "get_known_queue_keys",
        lambda _self: set(),
    )
    monkeypatch.setattr(
        model_endpoints_router,
        "provider_models",
        lambda: {
            ("minimax/MiniMax-Test", "minimax", "minimax"),
            ("openai/gpt-test", "openai", "openai"),
            ("azure/MyDeployment", "azure", "azure"),
        },
    )
    monkeypatch.setattr(
        model_endpoints_router.settings, "model_catalog", ["moonshot/PrivateModel"]
    )

    async def facets(_session, *, org_id):
        return SimpleNamespace(models=["minimax/minimax-test", "xai/old-model"])

    monkeypatch.setattr(model_endpoints_router, "browse_task_facets_core", facets)
    catalog = await model_endpoints_router._model_endpoint_catalog("org-1")
    assert {entry.model for entry in catalog} == {
        "minimax/MiniMax-Test",
        "openai/gpt-test",
        "azure/MyDeployment",
        "moonshot/PrivateModel",
        "xai/old-model",
    }
    assert (
        next(entry for entry in catalog if entry.model == "minimax/MiniMax-Test").source
        == "provider_catalog"
    )
    assert (
        next(entry for entry in catalog if entry.model == "xai/old-model").source
        == "previously_used"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider,route,resolved",
    [
        ("minimax/MiniMax-Test", "minimax", "minimax", "minimax/MiniMax-Test"),
        ("anthropic/claude-test", "anthropic", "anthropic", "anthropic/claude-test"),
        ("amazon.test", "bedrock", "bedrock", "bedrock/amazon.test"),
        ("azure/MyDeployment", "azure", "azure", "azure/MyDeployment"),
        ("openai/gpt-test", "openai", "openai", "openai/gpt-test"),
    ],
)
async def test_discovered_model_uses_catalog_case_and_route(
    monkeypatch, model, provider, route, resolved
):
    monkeypatch.setattr(
        model_endpoints_router, "provider_models", lambda: {(model, provider, route)}
    )
    monkeypatch.setattr(model_endpoints_router.settings, "openai_provider", "azure")
    monkeypatch.setattr(
        type(model_endpoints_router.settings),
        "require_azure_openai_config",
        lambda _self: {
            "api_key": "azure-test-key",
            "endpoint": "https://example.invalid",
            "api_version": "test",
        },
    )

    async def completion(**kwargs):
        assert kwargs["model"] == resolved
        if route == "openai":
            assert "api_base" not in kwargs
        return SimpleNamespace(
            id="request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": model, "route": route}
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model"] == model
    assert response.json()["resolved_model"] == resolved


@pytest.mark.asyncio
@pytest.mark.parametrize("include_bedrock", [False, True])
@pytest.mark.parametrize("route", [None, "anthropic"])
async def test_claude_case_fallback_preserves_anthropic_route(
    monkeypatch, include_bedrock, route
):
    model = "anthropic/claude-sonnet-4-6"
    entries = {(model, "anthropic", "anthropic")}
    if include_bedrock:
        entries.add(("global.anthropic.claude-sonnet-4-6", "bedrock", "bedrock"))
    monkeypatch.setattr(model_endpoints_router, "provider_models", lambda: entries)

    async def completion(**kwargs):
        assert kwargs["model"] == model
        return SimpleNamespace(
            id="anthropic-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/check", json={"model": model.upper(), "route": route}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["model"] == model
    assert payload["resolved_model"] == model
    assert payload["provider"] == "anthropic"
    assert payload["route"] == "anthropic"


@pytest.mark.asyncio
async def test_alternate_xai_credentials_are_selected_and_cached_separately(
    monkeypatch,
):
    monkeypatch.setattr(
        model_endpoints_router,
        "provider_models",
        lambda: {
            ("xai/grok-test", "xai", "xai"),
            ("xai/grok-test", "xai", "xai-swem"),
        },
    )
    monkeypatch.setenv("XAI_SWEM_API_KEY", "alternate-secret")
    calls = []

    async def completion(**kwargs):
        assert kwargs["model"] == "xai/grok-test"
        calls.append(kwargs.get("api_key"))
        return SimpleNamespace(
            id="request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))],
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        for route in ["xai-swem", "xai", "xai-swem"]:
            response = await client.post(
                "/models/check", json={"model": "xai/grok-test", "route": route}
            )
            assert response.status_code == 200
            assert response.json()["route"] == route
            assert "alternate-secret" not in response.text
        assert response.json()["credential"] == "XAI_SWEM_API_KEY"
    assert calls == ["alternate-secret", None]


@pytest.mark.asyncio
async def test_explicit_models_select_connections_independently_of_job_route(
    monkeypatch,
):
    monkeypatch.setattr(model_endpoints_router.settings, "openai_provider", "azure")
    monkeypatch.setattr(
        model_endpoints_router.settings,
        "model_catalog",
        [
            "openai/PrivateModel",
            "xai-swem/PrivateGrok",
        ],
    )
    catalog = await model_endpoints_router._model_endpoint_catalog("org-1")
    assert any(
        entry.model == "openai/PrivateModel" and entry.route == "openai"
        for entry in catalog
    )
    assert any(
        entry.model == "xai/PrivateGrok"
        and entry.route == "xai-swem"
        and entry.credential == "XAI_SWEM_API_KEY"
        for entry in catalog
    )
