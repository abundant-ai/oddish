from __future__ import annotations

import pytest

from oddish import observability
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
        trial_attempt=trial.attempts,
    )


@pytest.mark.asyncio
async def test_zero_harness_cost_settles_to_token_estimate(monkeypatch):
    trial = _trial(agent="claude-code", model="zai/glm-x-preview[1m]")
    await _store(monkeypatch, trial, _outcome())
    assert trial.cost_usd == pytest.approx(1.32)
    assert trial.status == TrialStatus.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "bedrock/global.anthropic.claude-opus-4-7",
    ],
)
async def test_positive_native_cost_is_kept_for_anthropic_claude(monkeypatch, model):
    trial = _trial(agent="claude-code", model=model)
    await _store(monkeypatch, trial, _outcome(cost_usd=4.56))
    assert trial.cost_usd == pytest.approx(4.56)


@pytest.mark.asyncio
async def test_positive_native_cost_is_ignored_for_fireworks_claude_code(monkeypatch):
    trial = _trial(agent="claude-code", model="fireworks/minimax-m3")
    await _store(
        monkeypatch,
        trial,
        _outcome(
            cost_usd=14.941,
            input_tokens=2_000_000,
            cache_tokens=1_000_000,
            output_tokens=100_000,
        ),
    )
    expected = 1_000_000 * 3e-7 + 1_000_000 * 6e-8 + 100_000 * 1.2e-6
    assert trial.cost_usd == pytest.approx(expected)


@pytest.mark.asyncio
async def test_positive_native_cost_is_kept_for_non_claude_fireworks_agent(monkeypatch):
    trial = _trial(
        agent="mini-swe-agent",
        model="fireworks_ai/accounts/fireworks/models/minimax-m3",
    )
    await _store(monkeypatch, trial, _outcome(cost_usd=4.56))
    assert trial.cost_usd == pytest.approx(4.56)


@pytest.mark.asyncio
async def test_zero_cost_unpriceable_model_settles_to_none(monkeypatch):
    trial = _trial(agent="claude-code", model="totally-made-up-model")
    warnings = []
    monkeypatch.setattr(
        observability,
        "log_warning",
        lambda message, **attributes: warnings.append((message, attributes)),
    )
    await _store(monkeypatch, trial, _outcome())
    assert trial.cost_usd is None
    assert warnings == [
        (
            "Trial has token usage but no resolved cost",
            {
                "tags": ("cost-integrity", "unpriced-model"),
                "metric": "trial_cost_unpriced",
                "trial_id": trial.id,
                "model": "totally-made-up-model",
                "agent": "claude-code",
                "provider": "bedrock",
                "attempt": trial.attempts,
                "input_tokens": 1_000_000,
                "cache_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 100_000,
                "native_cost_usd": 0.0,
                "native_cost_trusted": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_priceable_model_does_not_log_unpriced_warning(monkeypatch):
    trial = _trial(agent="claude-code", model="zai/glm-x-preview[1m]")
    warnings = []
    monkeypatch.setattr(
        observability,
        "log_warning",
        lambda message, **attributes: warnings.append((message, attributes)),
    )
    await _store(monkeypatch, trial, _outcome())
    assert trial.cost_usd == pytest.approx(1.32)
    assert warnings == []
