from __future__ import annotations

import pytest

from oddish.db import TrialStatus
from oddish.workers.harbor.runner import HarborOutcome
from oddish.workers.queue.trial_handler import _store_trial_results
from test_scoreless_trial_no_retry import _patch_session, _trial


def _outcome(**overrides):
    defaults = dict(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
        input_tokens=1_000_000,
        output_tokens=100_000,
        cost_usd=0.0,
    )
    defaults.update(overrides)
    return HarborOutcome(**defaults)


async def _store(monkeypatch, trial, outcome):
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id=trial.id,
        outcome=outcome,
        trial_s3_key=None,
        execution_error=None,
    )


@pytest.mark.asyncio
async def test_zero_harness_cost_settles_to_token_estimate(monkeypatch):
    trial = _trial(agent="claude-code", model="zai/glm-x-preview[1m]")
    await _store(monkeypatch, trial, _outcome())
    assert trial.cost_usd == pytest.approx(1.32)
    assert trial.cost_is_estimated is True
    assert trial.status == TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_positive_native_cost_is_kept_verbatim(monkeypatch):
    trial = _trial(agent="claude-code", model="claude-opus-4-7")
    await _store(monkeypatch, trial, _outcome(cost_usd=4.56))
    assert trial.cost_usd == pytest.approx(4.56)
    assert trial.cost_is_estimated is False


@pytest.mark.asyncio
async def test_zero_cost_unpriceable_model_settles_to_none(monkeypatch):
    trial = _trial(agent="claude-code", model="totally-made-up-model")
    await _store(monkeypatch, trial, _outcome())
    assert trial.cost_usd is None
    assert trial.cost_is_estimated is False
