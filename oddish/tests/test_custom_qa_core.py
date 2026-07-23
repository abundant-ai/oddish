from types import SimpleNamespace

import pytest

import oddish.core.custom_qa as custom_qa
from oddish.db.models import JobStatus
from oddish.schemas import CustomQARunRequest


@pytest.mark.asyncio
async def test_custom_qa_releases_session_while_analyzer_runs(monkeypatch):
    events: list[str] = []

    class FakeSession:
        def add(self, row):
            events.append("add")
            if getattr(row, "id", None) is None:
                row.id = "run_1"

        async def flush(self):
            events.append("flush")

        async def commit(self):
            events.append("commit")

        async def close(self):
            events.append("close")

    class FakeBlock:
        def __init__(self, **kwargs):
            self.id = "block_1"
            self.status = JobStatus.RUNNING
            self.error = None

        async def run(self):
            assert events[-1] == "close"
            self.status = JobStatus.SUCCESS
            return SimpleNamespace(output={"ok": True})

    async def validate_scope(*args, **kwargs):
        return None

    async def get_prompt(*args, **kwargs):
        return (
            SimpleNamespace(id="prompt_1", kind="QA_PRE_TRIAL"),
            SimpleNamespace(id="version_1", version=1, content="Audit this"),
        )

    monkeypatch.setattr(custom_qa, "_validate_scope", validate_scope)
    monkeypatch.setattr(custom_qa, "get_prompt_core", get_prompt)
    monkeypatch.setattr(custom_qa, "AnalyzerBlock", FakeBlock)

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

    assert events == ["add", "flush", "commit", "close", "add", "commit"]
    assert responses[0].status == JobStatus.SUCCESS.value
    assert responses[0].output == {"ok": True}
