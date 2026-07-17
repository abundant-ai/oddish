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


def test_provider_hash_none_when_unresolvable(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert platform_key_hash_for_provider("xai") is None  # env not set
    assert platform_key_hash_for_provider(None) is None
    assert platform_key_hash_for_provider("not-a-real-provider") is None
