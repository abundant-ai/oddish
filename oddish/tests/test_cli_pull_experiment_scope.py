"""Regression tests for ``oddish pull <experiment_id>`` trial scoping.

``pull`` used to resolve an experiment's artifacts by *task*: it re-fetched each
task via ``GET /tasks/{id}`` (which returns every trial on the task, regardless
of experiment) and queued all of them. Pulling one experiment therefore also
downloaded trials owned by sibling experiments that merely share the task id.

These tests pin the fix:
  * the experiment task list is fetched with embedded, experiment-scoped trials
    (``include_trials`` / ``compact_trials``);
  * the pull queues only those scoped trials, and the ``_get_task_status``
    fallback filters foreign trials back out;
  * each pulled trial's manifest entry is labelled with its owning experiment.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pull_mod = importlib.import_module("oddish.cli.pull")


class _Resp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _FakeClient:
    """Minimal stand-in for ``httpx.Client`` used by the pull helpers.

    Records every path fetched. Returns the configured single-task status for
    ``GET /tasks/{id}`` (the fallback path) and 404 for trial sub-resources so
    ``_pull_trial`` downloads nothing and just yields its summary.
    """

    def __init__(self, calls: list[str], task_status: dict | None = None):
        self._calls = calls
        self._task_status = task_status

    def get(self, url, params=None):
        self._calls.append(url)
        if url == "/tasks":
            return _Resp(200, [])
        if url.startswith("/tasks/") and self._task_status is not None:
            return _Resp(200, self._task_status)
        return _Resp(404, {"detail": "not found"})


def _pull_experiment(client, tasks, tmp_path):
    return pull_mod._pull_once(
        client,
        "experiment",
        "EXP",
        tmp_path,
        include_logs=False,
        include_files=False,
        include_structured_logs=False,
        include_task_files=False,
        cached_data=tasks,
    )


# --- the scoped list fetch ---------------------------------------------------


class _CapturingClient:
    def __init__(self, payload):
        self.captured: dict = {}
        self._payload = payload

    def get(self, url, params=None):
        self.captured = {"url": url, "params": params or {}}
        return _Resp(200, self._payload)


def test_list_tasks_for_experiment_requests_scoped_embedded_trials():
    client = _CapturingClient([{"id": "task-a", "experiment_id": "EXP", "trials": []}])
    pull_mod._list_tasks_for_experiment(client, "EXP", include_trials=True)
    assert client.captured["url"] == "/tasks"
    assert client.captured["params"]["experiment_id"] == "EXP"
    # Ask the server for experiment-scoped trials instead of re-fetching per task.
    assert client.captured["params"]["include_trials"] == "true"
    assert client.captured["params"]["compact_trials"] == "true"


def test_list_tasks_for_experiment_is_light_by_default():
    # The default must not embed trials — the watch completion poll uses it and
    # must not drag every trial on each iteration.
    client = _CapturingClient([{"id": "task-a", "experiment_id": "EXP"}])
    pull_mod._list_tasks_for_experiment(client, "EXP")
    assert "include_trials" not in client.captured["params"]
    assert "compact_trials" not in client.captured["params"]


def test_experiment_terminal_poll_does_not_fetch_trials():
    # `pull --watch` calls _is_experiment_terminal every interval; it only reads
    # task status, so it must issue the light (trial-free) list request.
    client = _CapturingClient(
        [{"id": "task-a", "status": "completed", "experiment_id": "EXP"}]
    )
    assert pull_mod._is_experiment_terminal(client, "EXP") is True
    assert "include_trials" not in client.captured["params"]


# --- the fallback filter -----------------------------------------------------


def test_scope_trials_to_experiment_keeps_only_owning_experiment():
    task = {
        "id": "task-a",
        "trials": [
            {"id": "task-a-30", "experiment_id": "EXP"},
            {"id": "task-a-32", "experiment_id": "SIBLING"},
        ],
    }
    scoped = pull_mod._scope_trials_to_experiment(task, "EXP")
    assert [t["id"] for t in scoped["trials"]] == ["task-a-30"]
    # The original task dict is not mutated.
    assert len(task["trials"]) == 2


# --- the pull itself ---------------------------------------------------------


def test_pull_experiment_queues_only_scoped_trials_and_labels_them(tmp_path):
    tasks = [
        {
            "id": "task-a",
            "status": "completed",
            "experiment_id": "EXP",
            "trials": [
                {"id": "task-a-30", "experiment_id": "EXP"},
                {"id": "task-a-31", "experiment_id": "EXP"},
            ],
        }
    ]
    calls: list[str] = []
    manifest = _pull_experiment(_FakeClient(calls), tasks, tmp_path)

    pulled = {t["trial_id"]: t.get("experiment_id") for t in manifest["trials"]}
    assert pulled == {"task-a-30": "EXP", "task-a-31": "EXP"}
    # Embedded trials were present, so no per-task /tasks/{id} refetch fired
    # (that refetch is what used to drag in sibling-experiment trials).
    assert not any(path.startswith("/tasks/task-a") for path in calls)


def test_pull_experiment_fallback_excludes_sibling_experiment_trials(tmp_path):
    # A task arrives WITHOUT embedded trials (older server, or the param was
    # ignored), forcing the /tasks/{id} fallback that returns every trial.
    tasks = [{"id": "task-a", "status": "completed", "experiment_id": "EXP"}]
    cross_experiment_task = {
        "id": "task-a",
        "status": "completed",
        "experiment_id": "EXP",
        "trials": [
            {"id": "task-a-30", "experiment_id": "EXP"},  # ours
            {"id": "task-a-32", "experiment_id": "SIBLING1"},  # sibling experiment
            {"id": "task-a-33", "experiment_id": "SIBLING2"},  # sibling experiment
        ],
    }
    calls: list[str] = []
    manifest = _pull_experiment(
        _FakeClient(calls, task_status=cross_experiment_task), tasks, tmp_path
    )

    pulled = {t["trial_id"] for t in manifest["trials"]}
    assert pulled == {"task-a-30"}  # siblings filtered back out
    assert "task-a-32" not in pulled
    assert "task-a-33" not in pulled
    # The fallback path was exercised (and then scoped).
    assert any(path == "/tasks/task-a" for path in calls)
