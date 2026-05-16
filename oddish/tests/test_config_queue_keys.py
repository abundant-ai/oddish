from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import NOP_ORACLE_QUEUE_KEY, Settings  # noqa: E402


def _settings(monkeypatch, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_NOP_ORACLE_CONCURRENCY", raising=False)
    return Settings(_env_file=None, **kwargs)


def test_nop_and_oracle_use_dedicated_queue_key(monkeypatch):
    settings = _settings(monkeypatch)

    assert settings.normalize_trial_model("nop", None) == "default"
    assert settings.normalize_trial_model("oracle", None) == "default"
    assert settings.get_queue_key_for_trial("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("oracle", None) == NOP_ORACLE_QUEUE_KEY


def test_nop_oracle_queue_has_separate_default_concurrency(monkeypatch):
    settings = _settings(
        monkeypatch,
        default_model_concurrency=8,
        nop_oracle_concurrency=64,
    )

    assert settings.get_model_concurrency("default") == 8
    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 64
    assert NOP_ORACLE_QUEUE_KEY in settings.get_known_queue_keys()


def test_model_concurrency_overrides_can_override_nop_oracle_queue(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        f'{{"{NOP_ORACLE_QUEUE_KEY}": 12, "default": 3}}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 12
    assert settings.get_model_concurrency("default") == 3
