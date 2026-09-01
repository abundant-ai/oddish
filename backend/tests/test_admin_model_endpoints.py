import sys
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin


def _app():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(org_id="org-1")
    return app


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-1")


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
            "/admin/model-endpoints", json={"model": " Google/Gemini-3.5-Flash "}
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
            "/admin/model-endpoints",
            json={"model": "global.anthropic.claude-sonnet-5"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["model"] == "global.anthropic.claude-sonnet-5"
    assert payload["resolved_model"] == ("bedrock/global.anthropic.claude-sonnet-5")
    assert payload["provider"] == "bedrock"


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

    monkeypatch.setattr(admin_router.settings, "anthropic_hdo_api_key", "hdo-key")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints",
            json={"model": "anthropic-hdo/claude-sonnet-4-6"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "anthropic/claude-sonnet-4-6"
    assert payload["provider"] == "anthropic-hdo"


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

    settings_type = type(admin_router.settings)
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
            "/admin/model-endpoints", json={"model": "openai/gpt-5.4-mini"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["resolved_model"] == "azure/oddish-gpt"
    assert payload["provider"] == "openai"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_model_endpoint_surfaces_upstream_http_status(monkeypatch, status_code):
    class FakeAPIError(Exception):
        pass

    error = FakeAPIError("The model is unavailable")
    error.status_code = status_code
    error.request_id = f"provider-request-{status_code}"

    async def completion(**_kwargs):
        raise error

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            APIError=FakeAPIError,
            Timeout=FakeAPIError,
            APIConnectionError=FakeAPIError,
            acompletion=completion,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "xai/grok-code-fast-1"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["latency_ms"], int)
    assert payload["ok"] is False
    assert payload["failure_kind"] == "provider"
    assert payload["transport"] == "litellm_completion"
    assert payload["status_code"] == status_code
    assert payload["error"] == "FakeAPIError: The model is unavailable"
    assert payload["request_id"] == f"provider-request-{status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["Timeout", "APIConnectionError"])
async def test_model_endpoint_surfaces_transport_failures(monkeypatch, error_name):
    class FakeAPIError(Exception):
        pass

    class Timeout(Exception):
        pass

    class APIConnectionError(Exception):
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
        SimpleNamespace(
            APIError=FakeAPIError,
            Timeout=Timeout,
            APIConnectionError=APIConnectionError,
            acompletion=completion,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "xai/grok-code-fast-1"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["failure_kind"] == "provider"
    assert payload["status_code"] is None
    assert payload["error"] == f"{error_name}: The provider did not respond"


@pytest.mark.asyncio
async def test_model_endpoint_does_not_hide_internal_errors(monkeypatch):
    class FakeAPIError(Exception):
        pass

    async def completion(**_kwargs):
        raise RuntimeError("unexpected integration defect")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            APIError=FakeAPIError,
            Timeout=FakeAPIError,
            APIConnectionError=FakeAPIError,
            acompletion=completion,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "minimax/minimax-m3"}
        )

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_model_endpoint_reports_configuration_errors(monkeypatch):
    def missing_config(_settings):
        raise RuntimeError("AZURE_OPENAI_API_KEY is missing")

    settings_type = type(admin_router.settings)
    monkeypatch.setattr(settings_type, "get_openai_provider", lambda _self: "azure")
    monkeypatch.setattr(settings_type, "require_azure_openai_config", missing_config)

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "openai/gpt-5.4-mini"}
        )

    assert response.status_code == 200
    assert response.json()["failure_kind"] == "configuration"
    assert response.json()["error"] == ("RuntimeError: AZURE_OPENAI_API_KEY is missing")


@pytest.mark.asyncio
async def test_model_endpoint_rejects_non_model_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "task_expand"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Queue key is not an LLM model"


@pytest.mark.asyncio
async def test_model_endpoint_requires_operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/model-endpoints", json={"model": "minimax/minimax-m3"}
        )

    assert response.status_code == 403
