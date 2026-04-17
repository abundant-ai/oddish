from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints
from oddish.db import ExperimentModel, TaskModel, storage as storage_mod


class _FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) != 1:
            raise AssertionError("Expected exactly one row")
        return self._rows[0]


class _FakeDeleteTaskSession:
    def __init__(
        self,
        task_rows: list[tuple[str, str | None, str]],
        trial_rows: list[tuple[str, str | None]],
    ):
        self.task_rows = task_rows
        self.trial_rows = trial_rows
        self.statements = []
        self.delete_called = False

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _FakeRowsResult(self.task_rows)
        if len(self.statements) == 2:
            return _FakeRowsResult(self.trial_rows)
        return _FakeRowsResult([])

    async def delete(self, _obj):
        self.delete_called = True
        raise AssertionError("delete_task_core should use SQL DELETE statements")


@pytest.mark.asyncio
async def test_delete_task_core_uses_bulk_deletes_for_task_versions(monkeypatch):
    task_rows = [("task-123", "tasks/task-123/", "s3://tasks/task-123/")]
    trial_rows = [
        ("task-123-0", "tasks/task-123/trials/task-123-0/"),
        ("task-123-1", None),
    ]
    session = _FakeDeleteTaskSession(task_rows, trial_rows)
    monkeypatch.setattr(
        storage_mod,
        "collect_s3_prefixes_for_deletion",
        lambda *, tasks, trials: ["tasks/task-123/", *[row[1] for row in trials if row[1]]],
    )

    result = await endpoints.delete_task_core(session, task_id="task-123")

    assert result == {
        "s3_prefixes": [
            "tasks/task-123/",
            "tasks/task-123/trials/task-123-0/",
        ],
        "deleted": {"task_id": "task-123"},
    }
    assert session.delete_called is False
    assert [statement.table.name for statement in session.statements[2:]] == [
        "trials",
        "tasks",
    ]
    assert [
        statement.get_execution_options().get("synchronize_session")
        for statement in session.statements[2:]
    ] == [False, False]


def test_task_relationships_use_passive_deletes():
    assert ExperimentModel.tasks.property.passive_deletes is True
    assert TaskModel.trials.property.passive_deletes is True
    assert TaskModel.versions.property.passive_deletes is True
