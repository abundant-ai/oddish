"""Zero native cost from the harness must settle to a token estimate.

Claude Code prices models it doesn't know (GLM/MiniMax/Kimi/Fireworks
Anthropic-compat passthroughs) at $0 per message, so its ``total_cost_usd``
is 0.0 for those trials despite real token usage. ``_store_trial_results``
must not persist that 0.0 verbatim.
"""

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
from oddish.workers.queue.trial_handler import _store_trial_results  # noqa: E402


def _trial(**overrides):
    defaults = dict(
        id="trial-1",
        task_id="task-1",
        agent="claude-code",
        model="zai/glm-x-preview[1m]",
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
        cache_write_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="w-1",
        current_queue_slot=0,
        heartbeat_at=None,
        superseded_by_trial_id=None,
        deleted_at=None,
        analysis=None,
        analysis_status=None,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


def _patch_session(monkeypatch, trial):
    @asynccontextmanager
    async def _fake_trial_session(
        trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield object(), trial

    monkeypatch.setattr(th, "_trial_session", _fake_trial_session)

    async def _fake_qa(session, trial_id):
        return False

    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", _fake_qa)

    async def _fake_gate(session, trial_id):
        return False

    monkeypatch.setattr("oddish.queue.maybe_gate_llm_trials", _fake_gate)


@pytest.mark.asyncio
async def test_zero_harness_cost_settles_to_token_estimate(monkeypatch):
    trial = _trial()
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(),
        trial_s3_key=None,
        execution_error=None,
    )
    # glm-x-preview gap-table rate: 1M in @ 1e-6 + 100k out @ 3.2e-6.
    assert trial.cost_usd == pytest.approx(1.32)
    assert trial.status == TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_positive_native_cost_is_kept_verbatim(monkeypatch):
    trial = _trial(model="claude-opus-4-7")
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(cost_usd=4.56),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.cost_usd == pytest.approx(4.56)


@pytest.mark.asyncio
async def test_zero_cost_unpriceable_model_settles_to_none(monkeypatch):
    trial = _trial(model="totally-made-up-model")
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.cost_usd is None
