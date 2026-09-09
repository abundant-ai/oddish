"""Catalog discovery uses credential presence, never scheduling limits or secrets in output."""

from api.services import model_catalog


def test_registry_models_follow_credentials_and_keep_provider_ids(monkeypatch):
    monkeypatch.setattr(
        model_catalog,
        "_registry",
        lambda: {
            "gpt-test": {"litellm_provider": "openai", "mode": "chat"},
            "text-embedding-test": {"litellm_provider": "openai", "mode": "embedding"},
            "openai/container": {"litellm_provider": "openai", "mode": "chat"},
            "ft:gpt-test": {"litellm_provider": "openai", "mode": "chat"},
            "claude-test": {"litellm_provider": "anthropic", "mode": "chat"},
            "minimax/MiniMax-Test": {"litellm_provider": "minimax", "mode": "chat"},
            "xai/grok-test": {"litellm_provider": "xai", "mode": "chat"},
            "fireworks_ai/accounts/fireworks/models/test-model": {
                "litellm_provider": "fireworks_ai",
                "mode": "chat",
            },
            "gemini/gemini-test": {"litellm_provider": "gemini", "mode": "chat"},
            "gemini-test": {"litellm_provider": "gemini", "mode": "chat"},
            "bedrock/us-east-1/amazon.test": {
                "litellm_provider": "bedrock",
                "mode": "chat",
            },
            "amazon.test": {"litellm_provider": "bedrock_converse", "mode": "chat"},
            "moonshot/private-test": {"litellm_provider": "moonshot", "mode": "chat"},
            "other/test": {"litellm_provider": "other", "mode": "chat"},
        },
    )
    connected = {
        "openai",
        "anthropic-hdo",
        "minimax",
        "xai",
        "xai-swem",
        "fireworks",
        "gemini",
        "bedrock",
    }
    monkeypatch.setattr(
        model_catalog, "credential_configured", lambda route: route in connected
    )
    monkeypatch.setattr(
        model_catalog.settings,
        "azure_openai_deployments",
        {"openai/gpt-test": "MyDeployment"},
    )
    # Public OpenAI must remain independent of the default job route.
    monkeypatch.setattr(model_catalog.settings, "openai_provider", "azure")
    monkeypatch.setattr(model_catalog.settings, "model_concurrency_overrides", {})

    assert model_catalog.provider_models() == {
        ("openai/gpt-test", "openai", "openai"),
        ("anthropic-hdo/claude-test", "anthropic-hdo", "anthropic-hdo"),
        ("minimax/MiniMax-Test", "minimax", "minimax"),
        ("xai/grok-test", "xai", "xai"),
        ("xai/grok-test", "xai", "xai-swem"),
        ("fireworks/test-model", "fireworks", "fireworks"),
        ("google/gemini-test", "gemini", "gemini"),
        ("amazon.test", "bedrock", "bedrock"),
        ("azure/MyDeployment", "azure", "azure"),
    }


def test_credential_presence_does_not_expose_values_or_count_other_settings(
    monkeypatch,
):
    monkeypatch.setattr(
        model_catalog.settings, "openai_api_key", "secret-from-settings"
    )
    monkeypatch.setattr(model_catalog.settings, "azure_openai_api_key", None)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("XAI_SWEM_API_KEY", "secret-alternate-key")
    monkeypatch.setenv("XAI_API_KEY", "  ")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert model_catalog.credential_configured("openai") is True
    assert model_catalog.credential_configured("azure") is False
    assert model_catalog.credential_configured("xai-swem") is True
    assert model_catalog.credential_configured("xai") is False
    assert model_catalog.credential_configured("bedrock") is None
    assert model_catalog.credential_variable("xai-swem") == "XAI_SWEM_API_KEY"


def test_installed_catalog_contains_supported_chat_models():
    # Verify the package/resource contract against the installed pinned dependency.
    registry = model_catalog._registry()
    providers = {
        info.get("litellm_provider")
        for info in registry.values()
        if info.get("mode") == "chat"
    }
    assert {
        "openai",
        "anthropic",
        "gemini",
        "fireworks_ai",
        "minimax",
        "moonshot",
        "xai",
        "zai",
        "openrouter",
    } <= providers
