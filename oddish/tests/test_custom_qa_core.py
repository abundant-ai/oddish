from types import SimpleNamespace

import pytest

import oddish.core.custom_qa as custom_qa
from oddish.db.models import JobStatus, WorkerJobKind
from oddish.schemas import CustomQARunRequest


@pytest.mark.asyncio
async def test_custom_qa_enqueues_one_analyzer_block_job(monkeypatch):
    added = []
    enqueued = []

    class FakeSession:
        def add(self, row):
            added.append(row)
            if getattr(row, "id", None) is None:
                row.id = "run_1"

        async def flush(self):
            return None

        async def commit(self):
            return None

    async def validate_scope(*args, **kwargs):
        return None

    async def get_prompt(*args, **kwargs):
        return (
            SimpleNamespace(id="prompt_1", kind="QA_PRE_TRIAL"),
            SimpleNamespace(id="version_1", version=1, content="Audit this"),
        )

    async def enqueue(session, request):
        enqueued.append(request)
        return SimpleNamespace(id="job_1")

    monkeypatch.setattr(custom_qa, "_validate_scope", validate_scope)
    monkeypatch.setattr(custom_qa, "get_prompt_core", get_prompt)
    monkeypatch.setattr(custom_qa, "enqueue_worker_job", enqueue)

    responses = await custom_qa.run_custom_qa_core(
        FakeSession(),
        data=CustomQARunRequest(
            scope_type="task",
            scope_id="task_1",
            variants=[{"kind": "QA_PRE_TRIAL"}],
            backend="api",
        ),
        org_id="org_1",
        user_id="user_1",
    )

    assert len(added) == 1
    assert added[0].status == JobStatus.QUEUED
    assert len(enqueued) == 1
    assert enqueued[0].kind == WorkerJobKind.ANALYZER_BLOCK
    assert enqueued[0].subject_table == "analyzer_runs"
    assert enqueued[0].subject_id == "run_1"
    assert enqueued[0].payload == {"analyzer_run_id": "run_1"}
    assert responses[0].status == JobStatus.QUEUED.value
    assert responses[0].analyzer_block_id == ""
