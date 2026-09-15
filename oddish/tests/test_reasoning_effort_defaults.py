from __future__ import annotations

import pytest
from harbor.models.trial.config import AgentConfig

from oddish.core.idempotency import compute_request_hash
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.queue import _build_harbor_config_for_trial
from oddish.reasoning_effort import configured_reasoning_effort
from oddish.schemas import (
    AgentModelPair,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)
from oddish.workers.harbor.agent_config import _build_agent_config


@pytest.mark.parametrize(
    "agent,model",
    [
        ("claude-code", "global.anthropic.claude-opus-4-8"),
        ("claude-code", "global.anthropic.claude-opus-5"),
        ("codex", "openai/gpt-5.6"),
        ("gemini-cli", "google/gemini-3.1-pro-preview"),
        ("antigravity-cli", "google/gemini-3.7-flash"),
        ("cursor-cli", "anthropic/claude-opus-5"),
        ("grok-build", "xai/vendor-latest-learnability"),
        ("mini-swe-agent", "openai/gpt-5.6"),
        ("aider", "anthropic/claude-sonnet-4-6"),
        ("openhands", "gemini/gemini-3.1-pro-preview"),
        ("copilot-cli", "gpt-5.6"),
        ("dsh", "gpt-5.6"),
        ("tbh", "meta/test-model"),
    ],
)
def test_new_submissions_leave_effort_unset(agent, model):
    spec = TrialSpec(agent=agent, model=model)
    request = TaskSubmission(task_path="test", trials=[spec])
    persisted = _build_harbor_config_for_trial(request, spec)
    assert configured_reasoning_effort(persisted) is None
    assert "reasoning_effort" not in (persisted or {}).get("agent_config", {}).get(
        "kwargs", {}
    )
    assert spec.agent_config is None  # Do not mutate the original request/hash.


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"reasoning_effort": "high"},
        {"reasoning_effort": "low"},
        {"reasoning_effort": None},
    ],
)
def test_explicit_configuration_survives_sweep_and_persistence(kwargs):
    config = AgentConfig(
        kwargs={"version": "test", **kwargs}, env={"CLAUDE_CODE_EFFORT_LEVEL": "low"}
    )
    request = TaskSweepSubmission(
        task_id="task",
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="global.anthropic.claude-opus-4-8",
                agent_config=config,
            )
        ],
    )
    spec = build_trial_specs_from_sweep(request)[0]
    saved = _build_harbor_config_for_trial(
        TaskSubmission(task_path="test", trials=[spec]), spec
    )
    assert saved["agent_config"]["kwargs"] == config.kwargs
    assert saved["agent_config"]["env"] == config.env


def test_default_sweep_counts_unset_runs_without_changing_request_hash():
    model = "global.anthropic.claude-opus-4-8"
    request = TaskSweepSubmission(
        task_id="task",
        configs=[AgentModelPair(agent="claude-code", model=model, n_trials=5)],
    )
    original_hash = compute_request_hash(request)
    specs = build_trial_specs_from_sweep(
        request, existing_counts={("claude-code", model, None): 5}
    )
    assert specs == []
    specs = build_trial_specs_from_sweep(
        request, existing_counts={("claude-code", model, "high"): 5}
    )
    assert len(specs) == 5
    assert all(spec.agent_config is None for spec in specs)
    assert compute_request_hash(request) == original_hash
    assert request.configs[0].agent_config is None


def test_worker_receives_no_effort_override():
    model = "global.anthropic.claude-opus-4-8"
    spec = TrialSpec(agent="claude-code", model=model)
    saved = _build_harbor_config_for_trial(
        TaskSubmission(task_path="test", trials=[spec]), spec
    )
    runtime = _build_agent_config(
        agent=spec.agent, model=model, raw_harbor_config=saved or {}
    )
    assert "reasoning_effort" not in runtime.kwargs
    assert spec.model == model
    assert configured_reasoning_effort({}) is None


@pytest.mark.asyncio
async def test_create_persists_unset_effort(session):
    from uuid import uuid4
    from sqlalchemy import select
    from oddish.db.models import TrialModel
    from oddish.queue import create_task

    model = "global.anthropic.claude-opus-4-8"
    task = await create_task(
        session,
        TaskSubmission(
            name=f"agent-default-{uuid4().hex[:8]}",
            task_path="test",
            trials=[TrialSpec(agent="claude-code", model=model)],
        ),
    )
    rows = (
        (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task.id, TrialModel.kind == "agent"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].reasoning_effort is None
    assert rows[0].model == model
    assert "reasoning_effort" not in (rows[0].harbor_config or {}).get(
        "agent_config", {}
    ).get("kwargs", {})
