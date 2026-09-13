"""Submit-time admission for the Geometric served-model allowlist.

Regression coverage for a real end-to-end miss: the gate was first placed in
``_plan_append_trials``, which only runs on the append/reconcile path, so a
FRESH submission created a trial row and burned worker retries before failing.
It now lives in ``build_trial_specs_from_sweep``, the chokepoint both the single
and batch submit paths share.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.sweeps import build_trial_specs_from_sweep  # noqa: E402
from oddish.schemas import TaskSweepSubmission  # noqa: E402


def _submission(model: str, agent: str = "mini-swe-agent") -> TaskSweepSubmission:
    return TaskSweepSubmission.model_validate(
        {
            "task_id": "task-under-test",
            "configs": [{"agent": agent, "model": model, "n_trials": 1}],
        }
    )


def test_served_model_is_admitted():
    specs = build_trial_specs_from_sweep(_submission("geometric/glm-5.3"))
    assert [spec.model for spec in specs] == ["geometric/glm-5.3"]


def test_gm_alias_is_admitted():
    specs = build_trial_specs_from_sweep(_submission("gm/glm-5.3"))
    assert len(specs) == 1


def test_unserved_model_is_rejected_at_submit():
    # Must fail here, before any trial row exists -- not later in the worker.
    with pytest.raises(HTTPException) as excinfo:
        build_trial_specs_from_sweep(_submission("geometric/glm-5.4"))
    assert excinfo.value.status_code == 400


def test_foreign_model_is_rejected_at_submit():
    # geometric/gpt-4o would otherwise reach litellm as ``openai/gpt-4o``,
    # whose default route is public OpenAI.
    with pytest.raises(HTTPException) as excinfo:
        build_trial_specs_from_sweep(_submission("geometric/gpt-4o"))
    assert excinfo.value.status_code == 400


def test_rejection_message_survives_rich_markup():
    # The CLI renders errors through rich, which parses ``[...]`` as console
    # markup and silently strips it. The served-model list is the one part the
    # reader needs, so it must not be bracketed.
    with pytest.raises(HTTPException) as excinfo:
        build_trial_specs_from_sweep(_submission("geometric/glm-5.4"))
    detail = str(excinfo.value.detail)
    assert "glm-5.3" in detail
    assert "[" not in detail and "]" not in detail


def test_other_providers_are_not_gated():
    # The allowlist is specific to Geometric's single-model endpoint.
    for model in ("zai/glm-5.4", "meta/anything-at-all", "claude-sonnet-4-5"):
        assert build_trial_specs_from_sweep(_submission(model, agent="claude-code"))
