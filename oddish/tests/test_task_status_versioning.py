from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import helpers
from oddish.db import TrialStatus


def _trial(
    trial_id: str,
    *,
    task_version_id: str | None,
    status: TrialStatus,
    reward: float | None,
    experiment_id: str | None = None,
    superseded_by_trial_id: str | None = None,
):
    return SimpleNamespace(
        id=trial_id,
        task_version_id=task_version_id,
        status=status,
        reward=reward,
        experiment_id=experiment_id,
        superseded_by_trial_id=superseded_by_trial_id,
    )


def test_get_task_status_trials_filters_to_current_version():
    task = SimpleNamespace(
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-2",
                task_version_id="task-1-v2",
                status=TrialStatus.FAILED,
                reward=0,
            ),
            _trial(
                "task-1-3",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=0.25,
            ),
        ],
    )

    visible_trials = helpers.get_task_status_trials(task)

    assert [trial.id for trial in visible_trials] == [
        "task-1-1",
        "task-1-2",
        "task-1-3",
    ]


def test_get_task_status_trials_keeps_all_trials_for_legacy_tasks():
    task = SimpleNamespace(
        current_version_id=None,
        trials=[
            _trial(
                "task-1-0",
                task_version_id=None,
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-1",
                task_version_id=None,
                status=TrialStatus.FAILED,
                reward=0,
            ),
        ],
    )

    visible_trials = helpers.get_task_status_trials(task)

    assert [trial.id for trial in visible_trials] == ["task-1-0", "task-1-1"]


def test_build_task_status_response_uses_current_version_trials(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        helpers,
        "build_trial_response",
        lambda trial, task_path, queue_info=None, **kwargs: trial.id,
    )

    def fake_build_task_status_response(task, **kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        helpers, "_build_task_status_response", fake_build_task_status_response
    )

    task = SimpleNamespace(
        task_path="/tmp/demo-task",
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-2",
                task_version_id="task-1-v2",
                status=TrialStatus.FAILED,
                reward=0,
            ),
            _trial(
                "task-1-3",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=0.25,
            ),
        ],
    )

    helpers.build_task_status_response(
        task,
        queue_info_by_trial_id={},
    )

    assert captured["total"] == 3
    assert captured["completed"] == 2
    assert captured["failed"] == 1
    assert captured["reward_success"] == 1
    assert captured["reward_sum"] == 1.25
    assert captured["reward_total"] == 3
    assert captured["trials"] == ["task-1-1", "task-1-2", "task-1-3"]


def test_get_task_status_trials_honors_explicit_version_override():
    """Callers can override the version pivot — e.g. experiment-scoped views."""
    task = SimpleNamespace(
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v1",
                status=TrialStatus.FAILED,
                reward=0,
            ),
            _trial(
                "task-1-2",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
        ],
    )

    visible = helpers.get_task_status_trials(task, version_id="task-1-v1")

    assert [trial.id for trial in visible] == ["task-1-0", "task-1-1"]


def test_get_task_status_trials_with_version_id_none_returns_all():
    task = SimpleNamespace(
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
        ],
    )

    visible = helpers.get_task_status_trials(task, version_id=None)

    assert [trial.id for trial in visible] == ["task-1-0", "task-1-1"]


def test_get_task_status_trials_hides_superseded_trials():
    """Reruns insert a fresh trial and mark the old row superseded.

    The default listing must collapse the rerun chain so users only see
    the live attempt -- otherwise the trial viewer "piles up" with
    history rows on every retry click.
    """
    task = SimpleNamespace(
        current_version_id="task-1-v1",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.FAILED,
                reward=0,
                superseded_by_trial_id="task-1-2",
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
            ),
            _trial(
                "task-1-2",
                task_version_id="task-1-v1",
                status=TrialStatus.QUEUED,
                reward=None,
            ),
        ],
    )

    visible_trials = helpers.get_task_status_trials(task)

    assert [trial.id for trial in visible_trials] == ["task-1-1", "task-1-2"]


def test_resolve_effective_version_id_returns_global_without_experiment():
    task = SimpleNamespace(
        current_version_id="task-1-v3",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-a",
            ),
        ],
    )

    assert helpers.resolve_effective_version_id(task) == "task-1-v3"


def test_resolve_effective_version_id_uses_latest_trial_in_experiment():
    """When the task was re-uploaded elsewhere, the experiment view should keep
    showing the newest version the experiment actually has trials for — not the
    task's global ``current_version_id``."""
    task = SimpleNamespace(
        current_version_id="task-1-v3",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-a",
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-a",
            ),
            _trial(
                "task-1-2",
                task_version_id="task-1-v3",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-b",
            ),
        ],
    )

    assert (
        helpers.resolve_effective_version_id(task, experiment_context_id="exp-a")
        == "task-1-v2"
    )


def test_resolve_effective_version_id_falls_back_when_experiment_has_no_trials():
    task = SimpleNamespace(
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v2",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-a",
            ),
        ],
    )

    assert (
        helpers.resolve_effective_version_id(task, experiment_context_id="exp-missing")
        == "task-1-v2"
    )


def test_build_task_status_response_scopes_trials_to_experiment_version(monkeypatch):
    """Regression: an experiment with trials at v1 should still surface them
    when the underlying task has since been bumped to v2 elsewhere."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        helpers,
        "build_trial_response",
        lambda trial, task_path, queue_info=None, **kwargs: trial.id,
    )

    def fake_build_task_status_response(task, **kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        helpers, "_build_task_status_response", fake_build_task_status_response
    )

    # Simulate what ``list_tasks_core`` does before calling the builder:
    # trials have already been filtered to just this experiment's rows.
    task = SimpleNamespace(
        task_path="/tmp/demo-task",
        current_version_id="task-1-v2",
        trials=[
            _trial(
                "task-1-0",
                task_version_id="task-1-v1",
                status=TrialStatus.SUCCESS,
                reward=1,
                experiment_id="exp-a",
            ),
            _trial(
                "task-1-1",
                task_version_id="task-1-v1",
                status=TrialStatus.FAILED,
                reward=0,
                experiment_id="exp-a",
            ),
        ],
    )

    helpers.build_task_status_response(
        task,
        queue_info_by_trial_id={},
        experiment_context_id="exp-a",
    )

    assert captured["effective_version_id"] == "task-1-v1"
    assert captured["total"] == 2
    assert captured["completed"] == 1
    assert captured["failed"] == 1
    assert captured["trials"] == ["task-1-0", "task-1-1"]
