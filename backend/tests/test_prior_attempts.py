"""Tests for the prior-attempts probe feature."""

from __future__ import annotations

from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _minimal_submission(**overrides) -> TaskSubmission:
    """Helper: build a TaskSubmission with the minimum required fields."""
    base = dict(
        task_path="/tmp/fake-task",
        name="t",
        trials=[TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")],
        user="alice",
    )
    base.update(overrides)
    return TaskSubmission(**base)


def test_build_harbor_config_persists_preset_name_and_prior_attempts_config():
    submission = _minimal_submission(
        extra_instructions="probe me",
        preset_name="cheat-detector",
        prior_attempts_config={
            "enabled": True,
            "mode": "last_n",
            "last_n": 5,
            "max_attempts": 50,
        },
    )
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert result["preset_name"] == "cheat-detector"
    assert result["prior_attempts_config"]["enabled"] is True
    assert result["prior_attempts_config"]["mode"] == "last_n"


def test_build_harbor_config_omits_fields_when_unset():
    submission = _minimal_submission(extra_instructions="probe me")
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert "preset_name" not in result
    assert "prior_attempts_config" not in result


from oddish.worker.prior_attempts import format_prior_attempts_block


def test_format_prior_attempts_block_empty_returns_empty_string():
    assert format_prior_attempts_block([]) == ""


def test_format_prior_attempts_block_renders_titles_and_outcomes():
    attempts = [
        {
            "title": "Modify main.rs to fake PASS output",
            "outcome": "Verifier rebuilt with pristine main.rs; reward 0.0.",
        },
        {
            "title": "Pre-write /tmp/score.txt as read-only",
            "outcome": "Verifier didn't depend on that path.",
        },
    ]
    block = format_prior_attempts_block(attempts)
    # Header signals the agent these are dead ends.
    assert "ALREADY been tried" in block
    assert "FAILED" in block
    # Both attempts present, numbered, in order.
    assert "1." in block and "2." in block
    assert "Modify main.rs to fake PASS output" in block
    assert "Verifier rebuilt with pristine main.rs" in block
    assert "Pre-write /tmp/score.txt as read-only" in block
    # Trailing separator so the next section is clearly delimited.
    assert block.rstrip().endswith("---")


def test_format_prior_attempts_block_handles_missing_outcome():
    attempts = [{"title": "A bare attempt with no outcome field"}]
    block = format_prior_attempts_block(attempts)
    assert "A bare attempt with no outcome field" in block
    # Title-only line: should not contain the dash-separator that joins
    # title and outcome on a normal entry.
    assert "A bare attempt with no outcome field —" not in block


def test_format_prior_attempts_block_truncates_to_char_budget():
    long_outcome = "x" * 500
    attempts = [
        {"title": f"attempt {i}", "outcome": long_outcome} for i in range(50)
    ]
    block = format_prior_attempts_block(attempts, char_budget=2000)
    # We only kept what fits — far fewer than 50 numbered lines.
    assert block.count("\n") < 30
    assert len(block) <= 2200  # budget + header/footer slack


import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)
from oddish.worker.prior_attempts import fetch_prior_attempts


@pytest_asyncio.fixture
async def seeded_prior_trials():
    """Seed three completed trials for the same (task_id, preset_name).

    Each trial's ``analysis.attempts`` carries a mix of failed/null/successful
    entries so we can verify the success-only filter and time ordering.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_pa_{suffix}"
    task_id = f"task_pa_{suffix}"
    preset_name = "cheat-detector"
    now = datetime.now(timezone.utc)

    rows = [
        # Newest: 2 failed cheats + 1 investigation.
        (
            f"trial_pa_{suffix}_0",
            now - timedelta(hours=1),
            [
                {"title": "Investigate verifier", "outcome": "...", "success": None},
                {"title": "Fake binary output", "outcome": "blocked by rebuild", "success": False},
                {"title": "Pre-write score.txt", "outcome": "no effect", "success": False},
            ],
        ),
        # Middle: 1 failed cheat + 1 successful.
        (
            f"trial_pa_{suffix}_1",
            now - timedelta(hours=2),
            [
                {"title": "Strace bypass", "outcome": "blocked by canary", "success": False},
                {"title": "Patch run_tests.sh", "outcome": "actually worked", "success": True},
            ],
        ),
        # Oldest: 1 failed cheat.
        (
            f"trial_pa_{suffix}_2",
            now - timedelta(days=2),
            [
                {"title": "Hardcode results.json", "outcome": "rejected", "success": False},
            ],
        ),
    ]

    async with get_session() as session:
        session.add(ExperimentModel(id=experiment_id, name=f"e-{suffix}"))
        session.add(
            TaskModel(
                id=task_id,
                name=f"t-{suffix}",
                user="test",
                task_path="/tmp/fake-task",
            )
        )
        for trial_id, finished_at, attempts in rows:
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key="test-pa",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    finished_at=finished_at,
                    harbor_config={"preset_name": preset_name},
                    analysis={"kind": "probe_summary", "attempts": attempts},
                    analysis_status=AnalysisStatus.SUCCESS,
                )
            )

    yield task_id, preset_name, now

    async with get_session() as session:
        for trial_id, _, _ in rows:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id == trial_id)
            )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
        )


@pytest.mark.asyncio
async def test_fetch_prior_attempts_last_n_returns_failed_only_newest_first(
    seeded_prior_trials,
):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "last_n", "last_n": 2, "max_attempts": 50},
        )
    # Last 2 trials only → 3 failed (2 from newest + 1 from middle).
    assert len(out) == 3
    titles = [a["title"] for a in out]
    assert "Fake binary output" in titles
    assert "Pre-write score.txt" in titles
    assert "Strace bypass" in titles
    # Successful attempt excluded.
    assert "Patch run_tests.sh" not in titles
    # Investigation excluded.
    assert "Investigate verifier" not in titles
    # Oldest trial excluded by last_n=2.
    assert "Hardcode results.json" not in titles


@pytest.mark.asyncio
async def test_fetch_prior_attempts_all_mode_includes_every_trial(
    seeded_prior_trials,
):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "all", "max_attempts": 50},
        )
    # All 3 trials → 4 failed cheats total.
    assert len(out) == 4
    assert {a["title"] for a in out} == {
        "Fake binary output",
        "Pre-write score.txt",
        "Strace bypass",
        "Hardcode results.json",
    }


@pytest.mark.asyncio
async def test_fetch_prior_attempts_since_date_filters_by_finished_at(
    seeded_prior_trials,
):
    task_id, preset_name, now = seeded_prior_trials
    cutoff = (now - timedelta(hours=12)).date().isoformat()  # excludes the 2-day-old trial
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={
                "mode": "since_date",
                "since_date": cutoff,
                "max_attempts": 50,
            },
        )
    titles = {a["title"] for a in out}
    assert "Hardcode results.json" not in titles
    assert "Fake binary output" in titles
    assert "Strace bypass" in titles


@pytest.mark.asyncio
async def test_fetch_prior_attempts_max_attempts_truncates(seeded_prior_trials):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "all", "max_attempts": 2},
        )
    assert len(out) == 2


@pytest.mark.asyncio
async def test_fetch_prior_attempts_skips_other_preset(seeded_prior_trials):
    task_id, _, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name="some-other-preset",
            filter_config={"mode": "all", "max_attempts": 50},
        )
    assert out == []
