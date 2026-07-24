from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import oddish.workers.jobs.handlers as job_handlers
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
        scope_type="task",
        scope_id="task_1",
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

    blocks = []

    class FakeBlock:
        def __init__(self, **kwargs):
            self.id = "block_1"
            self.error = None
            self.kwargs = kwargs
            blocks.append(self)

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

    assert len(blocks) == 1
    assert blocks[0].kwargs["subject_type"] == run.scope_type
    assert blocks[0].kwargs["subject_id"] == run.scope_id


@pytest.mark.asyncio
async def test_missing_prompt_version_persists_failed_status(monkeypatch):
    run = SimpleNamespace(
        id="run_missing",
        prompt_version_id="version_missing",
        status=JobStatus.QUEUED,
        error=None,
    )
    committed = False

    class FakeSession:
        async def get(self, model, row_id, **kwargs):
            if model is AnalyzerRunModel:
                return run
            if model is PromptVersionModel:
                return None
            raise AssertionError(f"unexpected model: {model}")

    @asynccontextmanager
    async def get_session():
        nonlocal committed
        yield FakeSession()
        committed = True

    monkeypatch.setattr(handler, "get_session", get_session)

    with pytest.raises(
        handler.MissingPromptVersionError,
        match="Prompt version version_missing not found",
    ):
        await handler.run_analyzer_block_job(run.id)

    assert committed
    assert run.status == JobStatus.FAILED
    assert run.error == "Prompt version version_missing not found"


@pytest.mark.asyncio
async def test_missing_prompt_version_is_a_permanent_job_failure(monkeypatch):
    run = SimpleNamespace(
        id="run_missing",
        status=JobStatus.QUEUED,
        analyzer_block_id=None,
        error=None,
        output=None,
    )

    class FakeSession:
        async def get(self, model, row_id, **kwargs):
            return run

    @asynccontextmanager
    async def get_session():
        yield FakeSession()

    async def execute(*args, **kwargs):
        raise handler.MissingPromptVersionError("Prompt version missing")

    monkeypatch.setattr(job_handlers, "get_session", get_session)
    monkeypatch.setattr(job_handlers, "run_analyzer_block_job", execute)

    outcome = await job_handlers.AnalyzerBlockJobHandler().run(
        SimpleNamespace(
            id="job_1",
            subject_id=run.id,
            payload={"analyzer_run_id": run.id},
        )
    )

    assert outcome.failure is not None
    assert outcome.failure.retryable is False
    assert outcome.failure.error_message == "Prompt version missing"
