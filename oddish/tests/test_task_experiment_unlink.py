"""Unit tests for ``unlink_task_from_experiment_core`` and its endpoint.

Removing a task from one experiment must drop *just* that membership: the
``task_experiments`` join row and the experiment's trials for the task are
tombstoned, while the task row -- and its data in every other experiment --
is left untouched. This is the safe path for pulling a *shared* task out of
one experiment, where a whole-task delete would wrongly remove it from the
others.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints
from oddish.core.endpoints import deletion as _deletion
from oddish.db import TaskStatus
from fastapi import HTTPException


class _FakeRowsResult:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        *,
        one_or_none_value: object | None = None,
        scalar_one_or_none_value: object | None = None,
    ):
        self._rows = rows or []
        self._one_or_none_value = one_or_none_value
        self._scalar_one_or_none_value = scalar_one_or_none_value

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._one_or_none_value

    def scalar_one_or_none(self):
        return self._scalar_one_or_none_value


class _FakeUnlinkSession:
    """Async-session fake that records the unlink soft-delete statements.

    The fake replays results in execute() order:
      1. task lookup (``one_or_none`` -> ``(task_id, org_id)``)
      2. scoped trial-id projection (``all``)
      3. UPDATE trials   SET deleted_at
      4. UPDATE task_experiments SET deleted_at
      5. task reload (``scalar_one_or_none`` -> task ORM stand-in)
    The single ``scalar`` call is the live-membership count.
    """

    def __init__(self, *, task_row, trial_rows, link_count=1, task_obj=None):
        self.task_row = task_row
        self.trial_rows = trial_rows
        self.link_count = link_count
        self.task_obj = task_obj
        self.execute_statements: list[object] = []
        self.scalar_calls = 0
        self.delete_called = False

    async def execute(self, statement, *_args, **_kwargs):
        self.execute_statements.append(statement)
        idx = len(self.execute_statements)
        if idx == 1:
            return _FakeRowsResult(one_or_none_value=self.task_row)
        if idx == 2:
            return _FakeRowsResult(rows=self.trial_rows)
        if idx == 5:
            return _FakeRowsResult(scalar_one_or_none_value=self.task_obj)
        return _FakeRowsResult()

    async def scalar(self, _statement, *_args, **_kwargs):
        self.scalar_calls += 1
        return self.link_count

    async def delete(self, _obj):
        self.delete_called = True
        raise AssertionError(
            "unlink_task_from_experiment_core should not call session.delete()"
        )


@pytest.fixture
def _patched_side_effects(monkeypatch):
    async def _noop_cancel_trials(*_args, **_kwargs):
        return None

    async def _noop_tag_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        _deletion, "_cancel_worker_jobs_for_trials", _noop_cancel_trials
    )
    # Lazy-imported inside the core function from oddish.queue.
    import oddish.queue as _queue

    monkeypatch.setattr(
        _queue, "_recompute_tag_projection_on_membership_removed", _noop_tag_hook
    )


@pytest.mark.asyncio
async def test_unlink_tombstones_link_and_scoped_trials_only(_patched_side_effects):
    """The join row + this experiment's trials are tombstoned; the task is not."""
    task_obj = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        verdict="good",
        verdict_status=None,
        trials=[],
    )
    session = _FakeUnlinkSession(
        task_row=("task-123", "org-1"),
        trial_rows=[("task-123-0",), ("task-123-1",)],
        link_count=1,
        task_obj=task_obj,
    )

    result = await endpoints.unlink_task_from_experiment_core(
        session,
        task_id="task-123",
        experiment_id="exp-9",
        org_id="org-1",
    )

    assert result == {
        "s3_prefixes": [],
        "deleted": {
            "task_id": "task-123",
            "experiment_id": "exp-9",
            "trials_deleted": 2,
            "task_removed": False,
        },
        "unlinked": True,
    }
    assert session.delete_called is False
    # Verdict cache cleared because the live trial set shrank.
    assert task_obj.verdict is None

    # Only two writes: UPDATE trials and UPDATE task_experiments. Crucially,
    # no UPDATE against the tasks table -- the task is never tombstoned.
    writes = [
        stmt
        for stmt in session.execute_statements
        if getattr(stmt, "__visit_name__", None) == "update"
    ]
    write_tables = [stmt.table.name for stmt in writes]
    assert write_tables == ["trials", "task_experiments"]
    assert "tasks" not in write_tables
    # Bulk trial UPDATE stays cheap; the soft-delete filter handles reads.
    trials_update = next(s for s in writes if s.table.name == "trials")
    assert (
        trials_update.get_execution_options().get("synchronize_session") is False
    )


@pytest.mark.asyncio
async def test_unlink_with_no_trials_drops_only_the_association(_patched_side_effects):
    """When the trials were already removed, only the join row is tombstoned."""
    session = _FakeUnlinkSession(
        task_row=("task-123", "org-1"),
        trial_rows=[],
        link_count=1,
        task_obj=SimpleNamespace(
            status=TaskStatus.COMPLETED, verdict=None, verdict_status=None, trials=[]
        ),
    )

    result = await endpoints.unlink_task_from_experiment_core(
        session,
        task_id="task-123",
        experiment_id="exp-9",
        org_id="org-1",
    )

    assert result["deleted"]["trials_deleted"] == 0
    assert result["unlinked"] is True
    writes = [
        stmt
        for stmt in session.execute_statements
        if getattr(stmt, "__visit_name__", None) == "update"
    ]
    # No trials -> no UPDATE trials; just the association is dropped.
    assert [stmt.table.name for stmt in writes] == ["task_experiments"]


@pytest.mark.asyncio
async def test_unlink_missing_task_raises_404(_patched_side_effects):
    session = _FakeUnlinkSession(task_row=None, trial_rows=[], link_count=0)

    with pytest.raises(HTTPException) as exc:
        await endpoints.unlink_task_from_experiment_core(
            session, task_id="nope", experiment_id="exp-9", org_id="org-1"
        )
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail


@pytest.mark.asyncio
async def test_unlink_not_a_member_raises_404(_patched_side_effects):
    session = _FakeUnlinkSession(
        task_row=("task-123", "org-1"), trial_rows=[], link_count=0
    )

    with pytest.raises(HTTPException) as exc:
        await endpoints.unlink_task_from_experiment_core(
            session, task_id="task-123", experiment_id="exp-other", org_id="org-1"
        )
    assert exc.value.status_code == 404
    assert "not a member" in exc.value.detail


def test_backend_router_exposes_unlink_endpoint():
    """Hosted backend must surface the task<->experiment unlink handler."""
    router_path = (
        Path(__file__).resolve().parents[2] / "backend" / "api" / "routers" / "tasks.py"
    )
    source = router_path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(router_path))

    target = "/experiments/{experiment_id}/tasks/{task_id}"
    unlink_node: ast.AsyncFunctionDef | None = None
    for node in module.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr != "delete":
                continue
            if not decorator.args:
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and first.value == target:
                unlink_node = node
                break
        if unlink_node is not None:
            break

    assert unlink_node is not None, f"Expected DELETE {target} route"
    src = ast.get_source_segment(source, unlink_node)
    assert src is not None
    assert "unlink_task_from_experiment_core(" in src
    assert "await session.commit()" in src
    assert "require_admin" in src
