"""Curated Fireworks/DeepSeek allowlist and credential-independent resolve."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish import config as config_mod  # noqa: E402
from oddish.config import (  # noqa: E402
    Settings,
    apply_model_catalog_overlay,
    auto_resolve_curated_model,
    settings,
)
from oddish.core.idempotency import compute_request_hash  # noqa: E402
from oddish.core.sweeps import validate_sweep_submission  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSweepSubmission  # noqa: E402


def _settings(monkeypatch, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CATALOG_OVERLAY", raising=False)
    monkeypatch.delenv("ODDISH_ENFORCE_MODEL_CREDENTIALS", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return Settings(_env_file=None, **kwargs)


def _sweep(agent: str, model: str, **kwargs) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task-1",
        configs=[AgentModelPair(agent=agent, model=model, n_trials=1, **kwargs)],
    )


def test_bare_deepseek_flash_pins_fireworks(monkeypatch):
    _settings(monkeypatch)
    resolved, _ = auto_resolve_curated_model("mini-swe-agent", "deepseek-v4-flash")
    assert resolved == "fireworks/deepseek-v4-flash-0731"


def test_dsh_stays_on_deepseek(monkeypatch):
    _settings(monkeypatch)
    resolved, _ = auto_resolve_curated_model("dsh", "deepseek-v4-flash")
    assert resolved == "deepseek/deepseek-v4-flash"
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(_sweep("dsh", "fireworks/deepseek-v4-flash"))
    assert exc.value.status_code == 422


def test_native_vendor_ids_do_not_auto_pin(monkeypatch):
    _settings(monkeypatch)
    for model in ("glm-5.2", "minimax-m3", "kimi-k2.7-code"):
        resolved, reason = auto_resolve_curated_model("mini-swe-agent", model)
        assert resolved == model
        assert reason is None


def test_unknown_fireworks_id_422s_unless_allowed(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(_sweep("mini-swe-agent", "fireworks/nope"))
    assert exc.value.status_code == 422
    allowed = _sweep("mini-swe-agent", "fireworks/nope", allow_unknown_model=True)
    validate_sweep_submission(allowed)
    assert allowed.configs[0].model == "fireworks/nope"


def test_alias_canonicalizes_and_append_normalize_matches(monkeypatch):
    _settings(monkeypatch)
    submission = _sweep("mini-swe-agent", "fireworks/deepseek-v4-flash")
    validate_sweep_submission(submission)
    assert submission.configs[0].model == "fireworks/deepseek-v4-flash-0731"
    assert (
        settings.normalize_trial_model(
            "mini-swe-agent",
            "fireworks/deepseek-v4-flash",
            strict=False,
        )
        == "fireworks/deepseek-v4-flash-0731"
    )


def test_codex_accepts_azure_rejects_fireworks(monkeypatch):
    _settings(monkeypatch)
    validate_sweep_submission(_sweep("codex", "azure/my-deployment"))
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(_sweep("codex", "fireworks/glm-5p2"))
    assert exc.value.status_code == 422


def test_resolver_ignores_provider_keys(monkeypatch):
    _settings(monkeypatch)

    def _forbidden(provider):
        raise AssertionError(f"resolver read credentials for {provider!r}")

    monkeypatch.setattr(config_mod, "has_provider_credential", _forbidden)
    for keys in ((), ("FIREWORKS_API_KEY",), ("DEEPSEEK_API_KEY",)):
        for name in ("FIREWORKS_API_KEY", "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        for name in keys:
            monkeypatch.setenv(name, "sk-test")
        assert auto_resolve_curated_model("mini-swe-agent", "deepseek-v4-flash") == (
            "fireworks/deepseek-v4-flash-0731",
            "auto-selected fireworks/deepseek-v4-flash-0731 "
            "for bare id 'deepseek-v4-flash'",
        )


def test_raw_hash_stable_across_defaults_and_differs_after_rewrite(monkeypatch):
    _settings(monkeypatch)
    raw = _sweep("mini-swe-agent", "deepseek-v4-flash")
    hashed = compute_request_hash(raw)
    validate_sweep_submission(raw)
    assert raw.configs[0].model == "fireworks/deepseek-v4-flash-0731"
    assert compute_request_hash(raw) != hashed
    again = _sweep("mini-swe-agent", "deepseek-v4-flash")
    assert compute_request_hash(again) == hashed


def test_overlay_alias_and_geometric_untouched(monkeypatch):
    _settings(monkeypatch)
    before_fw = dict(config_mod._FIREWORKS_SHORT_MODEL_IDS)
    monkeypatch.setenv(
        "ODDISH_MODEL_CATALOG_OVERLAY",
        '{"fireworks": {"deepseek-v9-secret": "deepseek-v9-secret-0101"}}',
    )
    try:
        apply_model_catalog_overlay()
        resolved, _ = auto_resolve_curated_model("mini-swe-agent", "deepseek-v9-secret")
        assert resolved == "fireworks/deepseek-v9-secret-0101"
    finally:
        config_mod._FIREWORKS_SHORT_MODEL_IDS.clear()
        config_mod._FIREWORKS_SHORT_MODEL_IDS.update(before_fw)
    geo = _sweep("mini-swe-agent", "geometric/glm-5.3")
    validate_sweep_submission(geo)
    assert geo.configs[0].model == "geometric/glm-5.3"
