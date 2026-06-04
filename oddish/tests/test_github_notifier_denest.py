"""Regression tests: notify_* must not hold a DB session open across the
_update_pr_comment_for_task call.

The worker runs with a size-1 DB pool (db_pool_size=1, max_overflow=0). The
notifiers used to open a session and, while it was still held, call
_update_pr_comment_for_task, which opens *another* session — nesting two
sessions deadlocked the pool ("QueuePool limit of size 1 overflow 0 reached,
connection timed out"), so the PR comment never auto-updated. These tests
assert no session is open at the moment the comment update is invoked.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

notifier = importlib.import_module("oddish.integrations.github.notifier")


def _install_fakes(monkeypatch, *, trial=None, task=None):
    """Patch get_session (tracking open count) + _update_pr_comment_for_task
    (recording the open count at call time). Returns the shared state dict."""
    state = {"open": 0, "max_open": 0, "open_at_update": None, "called_with": None}

    class FakeSession:
        async def get(self, model, _id):
            if model is notifier.TrialModel:
                return trial
            if model is notifier.TaskModel:
                return task
            return None

    class _CM:
        async def __aenter__(self):
            state["open"] += 1
            state["max_open"] = max(state["max_open"], state["open"])
            return FakeSession()

        async def __aexit__(self, *a):
            state["open"] -= 1
            return False

    async def fake_update(task_arg, experiment_id=None):
        state["open_at_update"] = state["open"]
        state["called_with"] = (task_arg, experiment_id)
        return True

    monkeypatch.setattr(notifier, "get_session", lambda: _CM())
    monkeypatch.setattr(notifier, "_update_pr_comment_for_task", fake_update)
    return state


def test_notify_trial_update_closes_session_before_update(monkeypatch):
    trial = SimpleNamespace(task_id="task-1", experiment_id="exp-1")
    task = SimpleNamespace(id="task-1", name="t", tags={})
    state = _install_fakes(monkeypatch, trial=trial, task=task)

    assert asyncio.run(notifier.notify_trial_update("task-1-0")) is True
    # The session must be CLOSED (0 open) when the comment update runs — no nesting.
    assert state["open_at_update"] == 0
    assert state["max_open"] == 1  # only ever one session at a time
    assert state["called_with"] == (task, "exp-1")


def test_notify_analysis_update_closes_session_before_update(monkeypatch):
    trial = SimpleNamespace(task_id="task-1", experiment_id="exp-2")
    task = SimpleNamespace(id="task-1", name="t", tags={})
    state = _install_fakes(monkeypatch, trial=trial, task=task)

    assert asyncio.run(notifier.notify_analysis_update("task-1-0")) is True
    assert state["open_at_update"] == 0
    assert state["called_with"] == (task, "exp-2")


def test_notify_verdict_update_closes_session_before_update(monkeypatch):
    task = SimpleNamespace(id="task-1", name="t", tags={})
    state = _install_fakes(monkeypatch, task=task)

    assert asyncio.run(notifier.notify_verdict_update("task-1")) is True
    assert state["open_at_update"] == 0
    assert state["called_with"] == (task, None)


def test_missing_trial_returns_false_without_update(monkeypatch):
    state = _install_fakes(monkeypatch, trial=None, task=None)
    assert asyncio.run(notifier.notify_trial_update("nope")) is False
    assert state["open_at_update"] is None  # update never attempted
