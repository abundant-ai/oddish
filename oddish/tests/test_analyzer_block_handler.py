from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import oddish.workers.jobs.handlers as job_handlers
import oddish.workers.queue.analyzer_block_handler as handler
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.blocks.analyzer.analyzer_block import AnalyzerType
from oddish.db.models import AnalyzerRunModel, JobStatus, PromptVersionModel


def test_automatic_run_uses_lifecycle_analyzer_type():
    assert (
        handler._analyzer_type_for_config({"automatic": True, "stage": "pre_trial"})
        == AnalyzerType.PRE_TRIAL
    )
    assert (
        handler._analyzer_type_for_config({"automatic": True, "stage": "post_trial"})
        == AnalyzerType.POST_TRIAL
    )
    assert (
        handler._analyzer_type_for_config({"automatic": False, "stage": "post_trial"})
        == AnalyzerType.CUSTOM_QA
    )


def test_subject_linkage_matches_the_lifecycle_cost_contract():
    """POST_TRIAL charges its trial (analyzer_id) and carries task_id; PRE_TRIAL
    charges its task; both leave attribution_org_id unset so _cost_attribution
    resolves the subject. CUSTOM_QA keeps the ad-hoc org attribution."""
    post = SimpleNamespace(id="run_1", org_id="org_1", scope_id="trial_9")
    assert handler._subject_linkage(
        AnalyzerType.POST_TRIAL, post, {"trial_id": "trial_9", "task_id": "task_3"}
    ) == ("trial_9", "task_3", None)

    pre = SimpleNamespace(id="run_2", org_id="org_1", scope_id="task_3")
    assert handler._subject_linkage(
        AnalyzerType.PRE_TRIAL, pre, {"task_id": "task_3"}
    ) == (None, "task_3", None)

    assert handler._subject_linkage(AnalyzerType.CUSTOM_QA, post, {}) == (
        "run_1",
        None,
        "org_1",
    )

    # Older runs whose run_config predates these keys fall back to scope_id.
    assert handler._subject_linkage(AnalyzerType.POST_TRIAL, post, {}) == (
        "trial_9",
        None,
        None,
    )


@pytest.mark.asyncio
async def test_post_trial_run_reaches_the_block_with_its_trial_subject(monkeypatch):
    """End-to-end through the handler: a lifecycle POST_TRIAL run must build the
    block with the trial as its cost subject, not the analyzer-run id and org
    that the ad-hoc (CUSTOM_QA) path uses."""
    run = SimpleNamespace(
        id="run_pt",
        org_id="org_1",
        prompt_version_id="version_pt",
        triggered_by_user_id="user_1",
        model="test-model",
        reasoning_effort=None,
        llm_client_type=LLMClientType.API.value,
        scope_id="trial_9",
        run_config={
            "automatic": True,
            "stage": "post_trial",
            "scope": {"type": "trial", "id": "trial_9"},
            "trial_id": "trial_9",
            "task_id": "task_3",
            "system_prompt": "Inspect the trial",
        },
        analyzer_block_id=None,
        status=JobStatus.QUEUED,
        output=None,
        error=None,
    )
    version = SimpleNamespace(id="version_pt", content="Audit this")
    captured: dict = {}

    class FakeSession:
        async def get(self, model, row_id, **kwargs):
            if model is AnalyzerRunModel:
                return run
            if model is PromptVersionModel:
                return version
            return None

    @asynccontextmanager
    async def get_session():
        yield FakeSession()

    class FakeBlock:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.id = "block_pt"
            self.error = None

        async def run(self):
            return SimpleNamespace(output={"ok": True})

    monkeypatch.setattr(handler, "get_session", get_session)
    monkeypatch.setattr(handler, "AnalyzerBlock", FakeBlock)

    await handler.run_analyzer_block_job(run.id)

    assert captured["analyzer_type"] is AnalyzerType.POST_TRIAL
    assert captured["analyzer_id"] == "trial_9"
    assert captured["task_id"] == "task_3"
    assert captured["attribution_org_id"] is None


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
