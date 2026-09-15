from __future__ import annotations

import pytest
from harbor.models.trial.config import AgentConfig

from oddish.core.idempotency import compute_request_hash
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.queue import _build_harbor_config_for_trial
from oddish.reasoning_effort import (
    configured_reasoning_effort,
    with_default_reasoning_effort,
)
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
def test_new_supported_submissions_persist_high(agent, model):
    spec = TrialSpec(agent=agent, model=model)
    request = TaskSubmission(task_path="test", trials=[spec])
    persisted = _build_harbor_config_for_trial(request, spec, model=model)
    assert configured_reasoning_effort(persisted) == "high"
    assert spec.agent_config is None  # Do not mutate the original request/hash.


@pytest.mark.parametrize(
    "agent,model",
    [
        ("nop", "nop_oracle"),
        ("oracle", "nop_oracle"),
        ("gemini-cli", "google/gemini-2.5-flash"),
        ("antigravity-cli", "google/gemini-2.5-pro"),
        ("cursor-cli", "openai/gpt-5.6[effort=max]"),
        ("codex", "openai/gpt-4o"),
        ("custom-agent", "custom/model"),
    ],
)
def test_unsupported_or_inline_effort_does_not_acquire_a_default(agent, model):
    assert with_default_reasoning_effort(agent, model, None) is None


@pytest.mark.parametrize("effort", ["low", "max", None])
def test_explicit_values_including_null_are_preserved(effort):
    config = AgentConfig(kwargs={"reasoning_effort": effort, "version": "test"})
    assert (
        with_default_reasoning_effort(
            "claude-code", "global.anthropic.claude-opus-4-8", config
        )
        is config
    )


def test_defaults_preserve_other_kwargs_and_explicit_environment():
    config = AgentConfig(kwargs={"version": "test"}, env={"EXAMPLE": "kept"})
    resolved = with_default_reasoning_effort(
        "claude-code", "global.anthropic.claude-opus-4-8", config
    )
    assert resolved.kwargs == {"version": "test", "reasoning_effort": "high"}
    assert resolved.env == config.env
    assert config.kwargs == {"version": "test"}
    explicit_env = AgentConfig(env={"CLAUDE_CODE_EFFORT_LEVEL": "low"})
    assert (
        with_default_reasoning_effort(
            "claude-code", "global.anthropic.claude-opus-4-8", explicit_env
        )
        is explicit_env
    )


def test_cli_sweep_defaults_before_reconciliation_without_changing_request_hash():
    model = "global.anthropic.claude-opus-4-8"
    request = TaskSweepSubmission(
        task_id="task",
        configs=[AgentModelPair(agent="claude-code", model=model, n_trials=5)],
    )
    original_hash = compute_request_hash(request)
    specs = build_trial_specs_from_sweep(
        request, existing_counts={("claude-code", model, None): 5}
    )
    assert len(specs) == 5  # Unknown historical efforts are not counted as high.
    assert all(spec.agent_config.kwargs["reasoning_effort"] == "high" for spec in specs)
    assert not build_trial_specs_from_sweep(
        request, existing_counts={("claude-code", model, "high"): 5}
    )
    assert compute_request_hash(request) == original_hash
    assert request.configs[0].agent_config is None


def test_worker_receives_persisted_default_without_rewriting_model_or_history():
    model = "global.anthropic.claude-opus-4-8"
    spec = TrialSpec(agent="claude-code", model=model)
    saved = _build_harbor_config_for_trial(
        TaskSubmission(task_path="test", trials=[spec]), spec, model=model
    )
    runtime = _build_agent_config(
        agent=spec.agent, model=model, raw_harbor_config=saved
    )
    assert runtime.kwargs["reasoning_effort"] == "high"
    assert spec.model == model
    assert configured_reasoning_effort({}) is None


def test_custom_runner_does_not_acquire_unrecognized_kwargs():
    config = AgentConfig(import_path="custom.agent:Agent")
    assert (
        with_default_reasoning_effort(
            "claude-code", "global.anthropic.claude-opus-4-8", config
        )
        is config
    )


@pytest.mark.asyncio
async def test_create_persists_high_without_mutating_historical_rows(session):
    from uuid import uuid4
    from sqlalchemy import select
    from oddish.db.models import TrialModel
    from oddish.queue import create_task

    model = "global.anthropic.claude-opus-4-8"
    task = await create_task(
        session,
        TaskSubmission(
            name=f"default-high-{uuid4().hex[:8]}",
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
    assert rows[0].reasoning_effort == "high"
    assert rows[0].model == model
    assert rows[0].harbor_config["agent_config"]["kwargs"]["reasoning_effort"] == "high"
