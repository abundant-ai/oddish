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
