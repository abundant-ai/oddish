from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.analyze import classifier  # noqa: E402


def test_verdict_client_defaults_to_mapped_azure_deployment(monkeypatch):
    seen: dict[str, object] = {}

    class _OpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(classifier, "OpenAI", _OpenAI)
    monkeypatch.setattr(classifier.settings, "openai_provider", "azure")
    monkeypatch.setattr(classifier.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_deployments",
        {"gpt-5.4": "oddish-gpt"},
    )

    _client, runtime_model, provider_label = classifier._build_verdict_openai_client(
        model="gpt-5.4",
        api_key=None,
        timeout=30,
    )

    assert runtime_model == "oddish-gpt"
    assert provider_label == "Azure OpenAI"
    assert seen == {
        "api_key": "az-key",
        "base_url": "https://example.openai.azure.com/openai/v1",
        "timeout": 30,
    }


def test_verdict_client_fails_when_azure_mapping_is_missing(monkeypatch):
    monkeypatch.setattr(classifier.settings, "openai_provider", "azure")
    monkeypatch.setattr(classifier.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        classifier.settings,
        "azure_openai_deployments",
        {"gpt-5.2": "oddish-gpt"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        classifier._build_verdict_openai_client(
            model="gpt-5.4",
            api_key=None,
            timeout=30,
        )


def test_verdict_client_public_openai_requires_explicit_provider(monkeypatch):
    seen: dict[str, object] = {}

    class _OpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(classifier, "OpenAI", _OpenAI)
    monkeypatch.setattr(classifier.settings, "openai_provider", "openai")
    monkeypatch.setattr(classifier.settings, "openai_api_key", "sk-test")

    with pytest.warns(UserWarning, match="public OpenAI API"):
        _client, runtime_model, provider_label = (
            classifier._build_verdict_openai_client(
                model="gpt-5.4",
                api_key=None,
                timeout=30,
            )
        )

    assert runtime_model == "gpt-5.4"
    assert provider_label == "public OpenAI"
    assert seen == {"api_key": "sk-test", "timeout": 30}


_AZURE_ENDPOINT = "https://abundant-oddish-foundry.openai.azure.com/openai/v1"
_AZURE_HOST = "abundant-oddish-foundry.openai.azure.com"


def test_azure_compat_codex_allowlists_azure_endpoint(monkeypatch):
    """The Azure Codex variant must declare the Azure host so Harbor's Modal
    egress firewall doesn't blackhole every gpt-5.5 request."""
    from oddish.config import settings as oddish_settings
    from oddish.workers.codex_agent import AzureCompatibleCodex

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
        import_path="oddish.workers.codex_agent:AzureCompatibleCodex",
        model_name="openai/gpt-5.5",
        agent_kwargs={},
    )

    assert _AZURE_HOST in domains
