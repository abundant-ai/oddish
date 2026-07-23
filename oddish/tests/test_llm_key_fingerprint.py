"""Unit tests for LLM-key fingerprinting (no DB)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.llm_key_fingerprint import (  # noqa: E402
    hash_llm_key,
    key_hint,
    platform_key_hash_for_provider,
    trial_llm_key_hash,
)


def test_hash_is_sha256_hex():
    assert hash_llm_key("xai-abcd1234") == hashlib.sha256(b"xai-abcd1234").hexdigest()


def test_hint_is_last_four():
    assert key_hint("xai-abcd1234") == "1234"


def test_provider_hash_reads_env_var(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-9f2c")
    assert (
        platform_key_hash_for_provider("xai")
        == hashlib.sha256(b"xai-secret-9f2c").hexdigest()
    )


def test_provider_hash_matches_admin_paste(monkeypatch):
    # The trial stamp and the admin-list entry must agree for the same key.
    key = "sk-ant-live-7777"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    assert platform_key_hash_for_provider("anthropic") == hash_llm_key(key)


def test_openai_provider_hash_uses_azure_key_in_default_route(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "openai_provider", "azure")
    monkeypatch.setattr(settings, "azure_openai_api_key", "azure-platform-key")
    monkeypatch.setenv("OPENAI_API_KEY", "public-openai-key")
    assert platform_key_hash_for_provider("openai") == hash_llm_key(
        "azure-platform-key"
    )


def test_openai_provider_hash_uses_public_key_in_public_route(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "openai_provider", "openai")
    monkeypatch.setattr(settings, "azure_openai_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "public-openai-key")
    monkeypatch.setenv("OPENAI_API_KEY", "different-public-key")
    assert platform_key_hash_for_provider("openai") == hash_llm_key(
        "public-openai-key"
    )


def test_openai_provider_hash_fails_open_on_invalid_route(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "openai_provider", "invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "public-openai-key")
    assert platform_key_hash_for_provider("openai") == hash_llm_key(
        "public-openai-key"
    )
    monkeypatch.delenv("OPENAI_API_KEY")
    assert platform_key_hash_for_provider("openai") is None


def test_provider_hash_none_when_unresolvable(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert platform_key_hash_for_provider("xai") is None  # env not set
    assert platform_key_hash_for_provider(None) is None
    assert platform_key_hash_for_provider("not-a-real-provider") is None


def test_oddish_wired_providers_resolve_outside_harbor_map(monkeypatch):
    # zai / minimax / fireworks exist only in Oddish's wiring, not Harbor's
    # PROVIDER_KEYS -- they must still stamp.
    from oddish.config import settings

    monkeypatch.setenv("ZAI_API_KEY", "zai-sponsored-1111")
    assert platform_key_hash_for_provider("zai") == hash_llm_key("zai-sponsored-1111")

    monkeypatch.setenv("MINIMAX_API_KEY", "mm-2222")
    assert platform_key_hash_for_provider("minimax") == hash_llm_key("mm-2222")

    monkeypatch.setenv("MOONSHOT_API_KEY", "ms-5555")
    assert platform_key_hash_for_provider("moonshot") == hash_llm_key("ms-5555")

    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-3333")
    assert platform_key_hash_for_provider("fireworks") == hash_llm_key("fw-3333")

    # azure resolves the key the worker actually exports
    # (AZURE_OPENAI_API_KEY), not Harbor's AZURE_API_KEY.
    monkeypatch.setattr(settings, "azure_openai_api_key", None)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_API_KEY", "az-wrong-var")
    assert platform_key_hash_for_provider("azure") is None
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-4444")
    assert platform_key_hash_for_provider("azure") == hash_llm_key("az-4444")


def test_anthropic_hdo_prefers_settings_then_env(monkeypatch):
    # Mirrors the worker's _resolve_anthropic_hdo_api_key: settings value
    # first, process env as the fallback.
    from oddish.config import settings

    monkeypatch.setattr(settings, "anthropic_hdo_api_key", "hdo-from-settings")
    monkeypatch.setenv("ANTHROPIC_HDO_API_KEY", "hdo-from-env")
    assert platform_key_hash_for_provider("anthropic-hdo") == hash_llm_key(
        "hdo-from-settings"
    )

    monkeypatch.setattr(settings, "anthropic_hdo_api_key", None)
    assert platform_key_hash_for_provider("anthropic-hdo") == hash_llm_key(
        "hdo-from-env"
    )


def test_bedrock_hash_uses_bearer_token_not_aws_access_key(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-platform-token")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "storage-access-key")
    assert platform_key_hash_for_provider("bedrock") == hash_llm_key(
        "bedrock-platform-token"
    )


def test_bedrock_hash_none_without_bearer_token(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "storage-access-key")
    assert platform_key_hash_for_provider("bedrock") is None


def test_stamp_strips_whitespace_to_match_admin_paste(monkeypatch):
    # An env value with a stray newline must hash equal to the trimmed key an
    # admin pastes into the UI (the router strips before hashing).
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-9f2c\n")
    assert platform_key_hash_for_provider("xai") == hash_llm_key("xai-secret-9f2c")


def test_byok_overlay_stamps_the_user_key(monkeypatch):
    # A trial that ran on a BYOK overlay must stamp the user's key, so listing
    # the platform key never swallows BYOK spend -- and pasting the user key
    # can exclude it.
    monkeypatch.setenv("XAI_API_KEY", "xai-platform")
    assert trial_llm_key_hash("xai", {"XAI_API_KEY": "xai-user"}) == hash_llm_key(
        "xai-user"
    )

    # claude-code BYOK on a Bedrock-canonicalized model reroutes to the direct
    # Anthropic API: the overlay carries ANTHROPIC_API_KEY, not bedrock's var.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-platform")
    assert trial_llm_key_hash(
        "bedrock", {"ANTHROPIC_API_KEY": "sk-ant-user"}
    ) == hash_llm_key("sk-ant-user")


def test_non_anthropic_provider_ignores_anthropic_byok_fallback(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-platform")
    assert trial_llm_key_hash(
        "xai", {"ANTHROPIC_API_KEY": "sk-ant-user"}
    ) == hash_llm_key("xai-platform")


def test_anthropic_hdo_ignores_anthropic_byok_fallback(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "anthropic_hdo_api_key", "hdo-platform")
    assert trial_llm_key_hash(
        "anthropic-hdo", {"ANTHROPIC_API_KEY": "sk-ant-user"}
    ) == hash_llm_key("hdo-platform")


def test_no_byok_overlay_falls_back_to_platform_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-platform")
    platform = hash_llm_key("xai-platform")
    assert trial_llm_key_hash("xai", None) == platform
    assert trial_llm_key_hash("xai", {}) == platform
    # An overlay with no model key for this provider (e.g. probe-only creds)
    # does not shadow the platform stamp.
    assert trial_llm_key_hash("xai", {"ODDISH_PROBE_TASK_ID": "t1"}) == platform
