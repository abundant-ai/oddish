from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import TrialStatus  # noqa: E402
from oddish.workers.harbor.runner import HarborOutcome  # noqa: E402
from oddish.workers.queue import trial_handler as th  # noqa: E402
from oddish.workers.queue.trial_handler import (  # noqa: E402
    SCORELESS_EXPECTED_MESSAGE,
    _expects_no_reward,
    _store_trial_results,
)


def _trial(**overrides):
    defaults = dict(
        id="trial-1",
        task_id="task-1",
        agent="codex",
        harbor_config=None,
        status=TrialStatus.RUNNING,
        error_message=None,
        harbor_stage=None,
        finished_at=None,
        max_attempts=6,
        attempts=1,
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="w-1",
        current_queue_slot=0,
        heartbeat_at=None,
        analysis=None,
        analysis_status=None,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _outcome(reward=None, error=None):
    return HarborOutcome(
        reward=reward,
        error=error,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )


def _patch_session(monkeypatch, trial):
    @asynccontextmanager
    async def _fake_trial_session(trial_id, *, allow_missing=False):
        yield object(), trial

    monkeypatch.setattr(th, "_trial_session", _fake_trial_session)

    async def _fake_qa(session, trial_id):
        return False

    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", _fake_qa)


@pytest.mark.parametrize(
    ("trial", "expected"),
    [
        (_trial(harbor_config={"verifier": {"disable": True}}), True),
        (_trial(harbor_config={"verifier": {"disable": False}}), False),
        (_trial(agent="nop"), True),
        (_trial(agent="oracle"), True),
        (_trial(agent="agent-nop"), True),
        (_trial(agent="codex", harbor_config={"verifier": {}}), False),
    ],
)
def test_expects_no_reward(trial, expected):
    assert _expects_no_reward(trial) is expected


@pytest.mark.parametrize(
    "trial",
    [_trial(harbor_config={"verifier": {"disable": True}}), _trial(agent="nop")],
)
@pytest.mark.asyncio
async def test_expected_scoreless_reward_none_terminal_success(monkeypatch, trial):
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.status == TrialStatus.SUCCESS
    assert trial.error_message == SCORELESS_EXPECTED_MESSAGE
    assert trial.finished_at is not None
    assert trial.attempts == 1


@pytest.mark.asyncio
async def test_genuine_missing_reward_still_retries(monkeypatch):
    trial = _trial(agent="codex", harbor_config=None, attempts=1, max_attempts=6)
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.status == TrialStatus.RETRYING
    assert trial.finished_at is None


@pytest.mark.asyncio
async def test_scoreless_with_error_is_not_treated_as_success(monkeypatch):
    trial = _trial(
        agent="nop",
        harbor_config={"verifier": {"disable": True}},
        attempts=1,
        max_attempts=6,
    )
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None, error="boom"),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.status == TrialStatus.RETRYING
