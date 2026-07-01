"""Settings tests for the dynamic-concurrency feed-forward config.

The controller's per-model ceiling is fed by operator-owned JSON: a quota-bucket
table (``ODDISH_PROVIDER_RATE_LIMITS``) plus a many-to-one ``queue_key -> bucket``
map (``ODDISH_QUEUE_KEY_BUCKETS``). The self-tuning loop itself is gated off by
default behind ``ODDISH_DYNAMIC_MODEL_CONCURRENCY``.
"""

from __future__ import annotations

import json

import pytest

from oddish.config import Settings

_ENV_VARS = (
    "ODDISH_DYNAMIC_MODEL_CONCURRENCY",
    "ODDISH_PROVIDER_RATE_LIMITS",
    "ODDISH_QUEUE_KEY_BUCKETS",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_dynamic_flag_defaults_off():
    assert Settings().dynamic_model_concurrency is False


def test_dynamic_flag_enabled(monkeypatch):
    monkeypatch.setenv("ODDISH_DYNAMIC_MODEL_CONCURRENCY", "true")
    assert Settings().dynamic_model_concurrency is True


def test_provider_rate_limits_empty_by_default():
    s = Settings()
    assert s.provider_rate_limits == {}
    assert s.queue_key_buckets == {}
    assert s.get_provider_rate_limit("openai/gpt-5.2") is None


def test_many_queue_keys_map_to_one_bucket(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_PROVIDER_RATE_LIMITS",
        json.dumps(
            {
                "anthropic-opus": {
                    "rpm": 4000,
                    "tpm": 400000,
                    "headroom": 0.6,
                }
            }
        ),
    )
    monkeypatch.setenv(
        "ODDISH_QUEUE_KEY_BUCKETS",
        json.dumps(
            {
                "anthropic/claude-opus-4-v1": "anthropic-opus",
                "anthropic/claude-opus-4-v2": "anthropic-opus",
            }
        ),
    )
    s = Settings()
    b1 = s.get_provider_rate_limit("anthropic/claude-opus-4-v1")
    b2 = s.get_provider_rate_limit("anthropic/claude-opus-4-v2")
    assert b1 == b2
    assert b1["rpm"] == 4000
    assert b1["tpm"] == 400000
    assert b1["headroom"] == 0.6


def test_unmapped_queue_key_has_no_bucket(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_PROVIDER_RATE_LIMITS",
        json.dumps({"b": {"rpm": 100, "tpm": 1000}}),
    )
    monkeypatch.setenv("ODDISH_QUEUE_KEY_BUCKETS", json.dumps({"openai/gpt-5.2": "b"}))
    s = Settings()
    assert s.get_provider_rate_limit("openai/gpt-5.2") is not None
    assert s.get_provider_rate_limit("google/gemini-3") is None


def test_invalid_provider_rate_limits_json_raises(monkeypatch):
    monkeypatch.setenv("ODDISH_PROVIDER_RATE_LIMITS", "{not json")
    with pytest.raises(ValueError):
        Settings()
