"""Curated Fireworks/DeepSeek model allowlists and submit fail-fast."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish import config as config_mod  # noqa: E402
from oddish.config import (  # noqa: E402
    Settings,
    apply_model_catalog_overlay,
    auto_resolve_curated_model,
    list_curated_models,
    pin_model_provider,
    to_fireworks_model_id,
)
from oddish.core.sweeps import validate_sweep_submission  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSweepSubmission  # noqa: E402
from oddish.workers.queue.provider_failures import (  # noqa: E402
    is_permanent_model_setup_exception,
    is_permanent_provider_failure,
    is_setup_failure_without_work,
    trial_did_real_agent_work,
)


def _settings(monkeypatch, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_MODEL_CATALOG_OVERLAY", raising=False)
    monkeypatch.delenv("ODDISH_MODEL_CATALOG_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("ODDISH_ENFORCE_MODEL_CREDENTIALS", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return Settings(_env_file=None, **kwargs)


def test_unknown_fireworks_model_rejected_when_strict(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(ValueError, match="Unknown fireworks model"):
        to_fireworks_model_id(
            "fireworks/this-is-not-a-real-model", allow_unknown=False
        )


def test_unknown_fireworks_suggests_close_id(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(ValueError, match="Did you mean 'fireworks/glm-5p2'"):
        to_fireworks_model_id("fireworks/glm-5p3", allow_unknown=False)


def test_unknown_fireworks_model_allowed_with_flag(monkeypatch):
    _settings(monkeypatch)
    assert (
        to_fireworks_model_id(
            "fireworks/this-is-not-a-real-model", allow_unknown=True
        )
        == "fireworks/this-is-not-a-real-model"
    )


def test_fireworks_alias_suggests_serverless_id(monkeypatch):
    settings = _settings(monkeypatch)
    assert (
        settings.normalize_trial_model(
            "mini-swe-agent",
            "fireworks/deepseek-v4-flash",
            allow_unknown=False,
        )
        == "fireworks/deepseek-v4-flash-0731"
    )


def test_bare_deepseek_flash_auto_selects_fireworks(monkeypatch):
    _settings(monkeypatch)
    resolved, reason = auto_resolve_curated_model(
        "mini-swe-agent", "deepseek-v4-flash"
    )
    assert resolved == "fireworks/deepseek-v4-flash-0731"
    assert reason is not None
    assert "auto-selected" in reason


def test_dsh_bare_deepseek_flash_stays_on_deepseek(monkeypatch):
    _settings(monkeypatch)
    resolved, reason = auto_resolve_curated_model("dsh", "deepseek-v4-flash")
    assert resolved == "deepseek/deepseek-v4-flash"
    assert reason is not None
    assert "deepseek/deepseek-v4-flash" in reason


def test_validate_sweep_dsh_rejects_fireworks_prefix(monkeypatch):
    _settings(monkeypatch)
    submission = TaskSweepSubmission(
        task_id="task-1",
        configs=[
            AgentModelPair(
                agent="dsh",
                model="fireworks/deepseek-v4-flash",
                n_trials=1,
            )
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(submission)
    assert exc.value.status_code == 422
    assert "locked to provider 'deepseek'" in str(exc.value.detail)


def test_bare_glm_minimax_kimi_do_not_auto_pin_fireworks(monkeypatch):
    _settings(monkeypatch)
    for bare in ("glm-5.2", "minimax-m3", "kimi-k2.7-code"):
        resolved, reason = auto_resolve_curated_model("mini-swe-agent", bare)
        assert resolved == bare
        assert reason is None


def test_validate_sweep_auto_pins_bare_id(monkeypatch):
    _settings(monkeypatch)
    submission = TaskSweepSubmission(
        task_id="task-1",
        configs=[
            AgentModelPair(
                agent="mini-swe-agent",
                model="deepseek-v4-flash",
                n_trials=1,
            )
        ],
    )
    validate_sweep_submission(submission)
    assert submission.configs[0].model == "fireworks/deepseek-v4-flash-0731"


def test_validate_sweep_rejects_unknown_fireworks(monkeypatch):
    _settings(monkeypatch)
    submission = TaskSweepSubmission(
        task_id="task-1",
        configs=[
            AgentModelPair(
                agent="mini-swe-agent",
                model="fireworks/this-is-not-a-real-model",
                n_trials=1,
            )
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(submission)
    assert exc.value.status_code == 422
    assert "Unknown fireworks model" in str(exc.value.detail)


def test_validate_sweep_mutates_to_canonical(monkeypatch):
    _settings(monkeypatch)
    submission = TaskSweepSubmission(
        task_id="task-1",
        configs=[
            AgentModelPair(
                agent="mini-swe-agent",
                model="fireworks/deepseek-v4-flash",
                n_trials=1,
            )
        ],
    )
    validate_sweep_submission(submission)
    assert submission.configs[0].model == "fireworks/deepseek-v4-flash-0731"


def test_provider_pin_and_conflict(monkeypatch):
    _settings(monkeypatch)
    assert (
        pin_model_provider("deepseek-v4-flash-0731", "fireworks")
        == "fireworks/deepseek-v4-flash-0731"
    )
    with pytest.raises(ValueError, match="conflicts"):
        pin_model_provider("deepseek/deepseek-v4-flash", "fireworks")


def test_overlay_adds_private_fireworks_id(monkeypatch):
    _settings(monkeypatch)
    before = dict(config_mod._FIREWORKS_SHORT_MODEL_IDS)
    monkeypatch.setenv(
        "ODDISH_MODEL_CATALOG_OVERLAY",
        '{"fireworks": {"partner-secret-model": "partner-secret-model"}}',
    )
    try:
        apply_model_catalog_overlay()
        assert (
            to_fireworks_model_id(
                "fireworks/partner-secret-model", allow_unknown=False
            )
            == "fireworks/partner-secret-model"
        )
    finally:
        config_mod._FIREWORKS_SHORT_MODEL_IDS.clear()
        config_mod._FIREWORKS_SHORT_MODEL_IDS.update(before)


def test_list_curated_models_includes_fireworks(monkeypatch):
    _settings(monkeypatch)
    rows = list_curated_models()
    canons = {row["canonical"] for row in rows}
    assert "fireworks/deepseek-v4-flash-0731" in canons
    assert "deepseek/deepseek-v4-flash" in canons


def test_list_curated_models_agent_filter_hides_incompatible(monkeypatch):
    _settings(monkeypatch)
    # grok-build is locked to xai; curated Fireworks/DeepSeek rows are hidden.
    assert list_curated_models(agent="grok-build") == []


def test_validate_sweep_locked_agent_normalizes_case(monkeypatch):
    _settings(monkeypatch)
    submission = TaskSweepSubmission(
        task_id="task-1",
        configs=[
            AgentModelPair(
                agent=" Grok-Build ",
                model="fireworks/deepseek-v4-flash",
                n_trials=1,
            )
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(submission)
    assert exc.value.status_code == 422
    assert "locked to provider 'xai'" in str(exc.value.detail)


def test_permanent_model_setup_exception_types():
    assert is_permanent_model_setup_exception("NotFoundError")
    assert is_permanent_model_setup_exception("ModelNotFoundError")
    assert not is_permanent_model_setup_exception("AgentTimeoutError")


def test_permanent_provider_regex_does_not_match_generic_unauthorized():
    assert is_permanent_provider_failure("unauthorized access to path") is False
    assert is_permanent_provider_failure("AgentAuthenticationError: nope") is True


def test_setup_failure_without_work_is_not_real_eval():
    assert (
        trial_did_real_agent_work(
            input_tokens=None,
            output_tokens=None,
            has_trajectory=False,
            total_steps=None,
        )
        is False
    )


def test_setup_exception_after_real_work_stays_visible():
    assert (
        trial_did_real_agent_work(
            input_tokens=12,
            output_tokens=0,
            has_trajectory=False,
            total_steps=None,
        )
        is True
    )
    assert (
        trial_did_real_agent_work(
            input_tokens=None,
            output_tokens=None,
            has_trajectory=True,
            total_steps=None,
        )
        is True
    )


def test_message_only_setup_failure_without_work():
    assert (
        is_setup_failure_without_work(
            exception_type=None,
            error="NotFoundError: Model not found",
            input_tokens=None,
            output_tokens=None,
            has_trajectory=False,
            total_steps=None,
        )
        is True
    )
    assert (
        is_setup_failure_without_work(
            exception_type=None,
            error="NotFoundError: Model not found",
            input_tokens=8,
            output_tokens=None,
            has_trajectory=False,
            total_steps=None,
        )
        is False
    )
