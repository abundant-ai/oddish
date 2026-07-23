from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import oddish.workers.queue.analyzer_block_handler as handler
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.db.models import AnalyzerRunModel, JobStatus, PromptVersionModel


@pytest.mark.asyncio
async def test_analyzer_block_finalizes_with_a_fresh_session(monkeypatch):
    run = SimpleNamespace(
        id="run_1",
        org_id="org_1",
        prompt_version_id="version_1",
        triggered_by_user_id="user_1",
        model="test-model",
        reasoning_effort=None,
        llm_client_type=LLMClientType.API.value,
        run_config={
            "scope": {"type": "task", "id": "task_1"},
            "system_prompt": "Inspect the task",
        },
        analyzer_block_id=None,
        status=JobStatus.QUEUED,
        output=None,
        error=None,
    )
    version = SimpleNamespace(id="version_1", content="Audit this")
    sessions = []

    class FakeSession:
        def __init__(self):
            self.closed = False

        async def get(self, model, row_id, **kwargs):
            assert not self.closed
            if model is AnalyzerRunModel and row_id == run.id:
                return run
            if model is PromptVersionModel and row_id == version.id:
                return version
            return None

    @asynccontextmanager
    async def get_session():
        session = FakeSession()
        sessions.append(session)
        try:
            yield session
        finally:
            session.closed = True

    class FakeBlock:
        def __init__(self, **kwargs):
            self.id = "block_1"
            self.error = None

        async def run(self):
            assert sessions[0].closed
            return SimpleNamespace(output={"ok": True})

    monkeypatch.setattr(handler, "get_session", get_session)
    monkeypatch.setattr(handler, "AnalyzerBlock", FakeBlock)

    await handler.run_analyzer_block_job(run.id)

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert all(session.closed for session in sessions)
    assert run.analyzer_block_id == "block_1"
    assert run.status == JobStatus.SUCCESS
    assert run.output == {"ok": True}
