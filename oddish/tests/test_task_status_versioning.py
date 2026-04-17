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
):
    return SimpleNamespace(
        id=trial_id,
        task_version_id=task_version_id,
        status=status,
        reward=reward,
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
        lambda trial, task_path, queue_info=None: trial.id,
    )

    def fake_build_task_status_response(task, **kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(helpers, "_build_task_status_response", fake_build_task_status_response)

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
