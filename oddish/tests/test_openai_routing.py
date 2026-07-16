from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.llm import _resolve_route  # noqa: E402


def test_verdict_route_defaults_to_mapped_azure_deployment(monkeypatch):
    monkeypatch.setattr(settings, "openai_provider", "azure")
    monkeypatch.setattr(settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        settings,
        "azure_openai_deployments",
        {"gpt-5.4": "oddish-gpt"},
    )

    litellm_model, pricing_model, kwargs = _resolve_route("gpt-5.4", None)

    assert litellm_model == "openai/oddish-gpt"
    assert pricing_model == "gpt-5.4"
    assert kwargs == {
        "api_key": "az-key",
        "api_base": "https://example.openai.azure.com/openai/v1",
    }


def test_verdict_route_fails_when_azure_mapping_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "openai_provider", "azure")
    monkeypatch.setattr(settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        settings,
        "azure_openai_deployments",
        {"gpt-5.2": "oddish-gpt"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        _resolve_route("gpt-5.4", None)


def test_verdict_route_public_openai_requires_explicit_provider(monkeypatch):
    monkeypatch.setattr(settings, "openai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    with pytest.warns(UserWarning, match="public OpenAI API"):
        litellm_model, pricing_model, kwargs = _resolve_route("gpt-5.4", None)

    assert litellm_model == "openai/gpt-5.4"
    assert pricing_model == "gpt-5.4"
    assert kwargs == {"api_key": "sk-test"}


_AZURE_ENDPOINT = "https://abundant-oddish-foundry.openai.azure.com/openai/v1"
_AZURE_HOST = "abundant-oddish-foundry.openai.azure.com"


def test_azure_compat_codex_allowlists_azure_endpoint(monkeypatch):
    """The Azure Codex variant must declare the Azure host so Harbor's Modal
    egress firewall doesn't blackhole every gpt-5.5 request."""
    from oddish.config import settings as oddish_settings
    from oddish.workers.agents.codex import AzureCompatibleCodex

    monkeypatch.setattr(oddish_settings, "azure_openai_endpoint", _AZURE_ENDPOINT)

    domains = AzureCompatibleCodex.required_outbound_domains(
        model_name="openai/gpt-5.5", kwargs={}
    )

    assert _AZURE_HOST in domains


def test_harbor_infer_agent_domains_uses_azure_codex_hook(monkeypatch):
    """End-to-end: Harbor's egress-allowlist resolver picks up the Azure host
    via the AzureCompatibleCodex import path. Without the hook it falls back to
    the static codex map (api.openai.com / ab.chatgpt.com) and the real Azure
    endpoint is firewalled."""
    from harbor.environments.modal_network import infer_agent_domains

    from oddish.config import settings as oddish_settings

    monkeypatch.setattr(oddish_settings, "azure_openai_endpoint", _AZURE_ENDPOINT)

    domains = infer_agent_domains(
        name=None,
        import_path="oddish.workers.agents.codex:AzureCompatibleCodex",
        model_name="openai/gpt-5.5",
        agent_kwargs={},
    )

    assert _AZURE_HOST in domains


def test_azure_compat_codex_allowlists_per_trial_openai_base_url():
    """If a trial pins an explicit OPENAI_BASE_URL (extra_env), the hook
    allowlists that host too (mirrors harbor's base Codex hook)."""
    from oddish.workers.agents.codex import AzureCompatibleCodex

    domains = AzureCompatibleCodex.required_outbound_domains(
        model_name="openai/gpt-5.5",
        kwargs={
            "extra_env": {
                "OPENAI_BASE_URL": "https://other-foundry.openai.azure.com/openai/v1"
            }
        },
    )

    assert "other-foundry.openai.azure.com" in domains
