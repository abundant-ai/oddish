from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints


class _FakeRowsResult:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        *,
        scalar_one_or_none_value: object | None = None,
        rowcount: int | None = None,
    ):
        self._rows = rows or []
        self._scalar_one_or_none_value = scalar_one_or_none_value
        self.rowcount = rowcount

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar_one_or_none_value


class _FakeDeleteExperimentSession:
    """Async-session fake that records experiment soft-delete statements."""

    def __init__(self):
        self.execute_calls = 0
        self.execute_statements = []
        self.scalar_calls = 0
        self.scalar_statements = []
        self.delete_called = False

    async def execute(self, statement, *_args, **_kwargs):
        self.execute_calls += 1
        self.execute_statements.append(statement)

        if self.execute_calls == 1:
            return _FakeRowsResult(
                scalar_one_or_none_value=SimpleNamespace(id="exp-123")
            )
        if self.execute_calls == 2:
            return _FakeRowsResult(rows=[("task-123",)])
        if self.execute_calls == 3:
            return _FakeRowsResult(rows=[("trial-123",)])
        if self.execute_calls == 4:
            return _FakeRowsResult(rowcount=1)
        if self.execute_calls == 5:
            return _FakeRowsResult(rowcount=1)
        if self.execute_calls == 6:
            return _FakeRowsResult(rowcount=1)
        if self.execute_calls == 7:
            return _FakeRowsResult(rowcount=1)

        raise AssertionError(f"Unexpected execute() call #{self.execute_calls}")

    async def scalar(self, statement, *_args, **_kwargs):
        self.scalar_calls += 1
        self.scalar_statements.append(statement)
        if self.scalar_calls in (1, 2):
            return 0
        raise AssertionError(f"Unexpected scalar() call #{self.scalar_calls}")

    async def delete(self, _obj):
        self.delete_called = True
        raise AssertionError("delete_experiment_core should not call session.delete()")


@pytest.mark.asyncio
async def test_delete_experiment_core_soft_deletes_domain_rows(monkeypatch):
    """Soft-delete keeps experiments/tasks/trials/memberships as tombstones."""

    async def _noop_cancel_task(*_args, **_kwargs):
        return None

    async def _noop_cancel_trials(*_args, **_kwargs):
        return None

    monkeypatch.setattr(endpoints, "_cancel_worker_jobs_for_task", _noop_cancel_task)
    monkeypatch.setattr(
        endpoints, "_cancel_worker_jobs_for_trials", _noop_cancel_trials
    )

    session = _FakeDeleteExperimentSession()

    result = await endpoints.delete_experiment_core(
        session,
        experiment_id="exp-123",
        org_id="org-123",
    )

    assert result == {
        "s3_prefixes": [],
        "deleted": {
            "trials": 1,
            "tasks": 1,
            "experiments": 1,
        },
    }
    assert session.delete_called is False

    write_statements = session.execute_statements[3:]
    table_names = [stmt.table.name for stmt in write_statements]
    assert table_names == [
        "trials",
        "task_experiments",
        "experiments",
        "tasks",
    ]
    assert all(stmt.__visit_name__ == "update" for stmt in write_statements)

    bulk_updates = [
        stmt for stmt in write_statements if stmt.table.name in {"trials", "tasks"}
    ]
    assert all(
        stmt.get_execution_options().get("synchronize_session") is False
        for stmt in bulk_updates
    )


def test_backend_router_exposes_delete_experiment_endpoint():
    """Hosted backend must surface the experiment soft-delete handler."""

    router_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "api"
        / "routers"
        / "tasks.py"
    )
    source = router_path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(router_path))

    delete_node: ast.AsyncFunctionDef | None = None
    for node in module.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "delete":
                continue
            if not decorator.args:
                continue
            first_arg = decorator.args[0]
            if (
                isinstance(first_arg, ast.Constant)
                and first_arg.value == "/experiments/{experiment_id}"
            ):
                delete_node = node
                break
        if delete_node is not None:
            break

    assert delete_node is not None, "Expected DELETE /experiments/{experiment_id} route"

    delete_source = ast.get_source_segment(source, delete_node)
    assert delete_source is not None
    assert "delete_experiment_core(" in delete_source
    assert "await session.commit()" in delete_source
