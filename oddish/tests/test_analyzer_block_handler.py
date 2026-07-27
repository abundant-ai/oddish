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
    resolves the subject and set no explicit subject_type/subject_id. CUSTOM_QA
    keeps the ad-hoc org attribution and passes its own scope as the subject so
    the cost row is not left scope-less."""
    post = SimpleNamespace(
        id="run_1", org_id="org_1", scope_type="trial", scope_id="trial_9"
    )
    assert handler._subject_linkage(
        AnalyzerType.POST_TRIAL, post, {"trial_id": "trial_9", "task_id": "task_3"}
    ) == ("trial_9", "task_3", None, None, None)

    pre = SimpleNamespace(
        id="run_2", org_id="org_1", scope_type="task", scope_id="task_3"
    )
    assert handler._subject_linkage(
        AnalyzerType.PRE_TRIAL, pre, {"task_id": "task_3"}
    ) == (None, "task_3", None, None, None)

    assert handler._subject_linkage(AnalyzerType.CUSTOM_QA, post, {}) == (
        "run_1",
        None,
        "org_1",
        "trial",
        "trial_9",
    )

    # Older runs whose run_config predates these keys fall back to scope_id.
    assert handler._subject_linkage(AnalyzerType.POST_TRIAL, post, {}) == (
        "trial_9",
        None,
        None,
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
async def test_pre_trial_assignment_runs_worker_local_not_sandbox(
    monkeypatch, tmp_path
):
    """A pre-trial QA-job assignment (Path B) must run on the worker-local
    CLAUDE_CLI backend -- worker downloads the task source, agent Read/Globs it --
    even when the assignment stored a sandbox backend. No sandbox, no oddish-CLI
    install, so nothing depends on a published CLI version."""
    run = SimpleNamespace(
        id="run_pre",
        org_id="org_1",
        prompt_version_id="version_pre",
        triggered_by_user_id="user_1",
        model="claude-haiku-4-5",
        reasoning_effort=None,
        # Stored sandbox backend -- must be overridden for pre-trial.
        llm_client_type=LLMClientType.SANDBOX.value,
        scope_id="task_3",
        run_config={
            "automatic": True,
            "stage": "pre_trial",
            "scope": {"type": "task", "id": "task_3"},
            "task_id": "task_3",
            "trial_id": "trial_9",
            "oddish_cli_enabled": True,
            "system_prompt": "sandbox-flavored instructions",
        },
        analyzer_block_id=None,
        status=JobStatus.QUEUED,
        output=None,
        error=None,
    )
    version = SimpleNamespace(id="version_pre", content="Audit the source at cwd.")
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
            self.id = "block_pre"
            self.error = None

        async def run(self):
            return SimpleNamespace(output={"items": []})

    async def fake_resolve_task_source(task_id):
        assert task_id == "task_3"
        return "s3://task_3/key", None

    async def fake_resolve_task_directory(task_id, *, task_s3_key, task_path):
        return tmp_path, None, task_s3_key

    monkeypatch.setattr(handler, "get_session", get_session)
    monkeypatch.setattr(handler, "AnalyzerBlock", FakeBlock)
    monkeypatch.setattr(handler, "_resolve_task_source", fake_resolve_task_source)
    monkeypatch.setattr(handler, "resolve_task_directory", fake_resolve_task_directory)

    await handler.run_analyzer_block_job(run.id)

    assert captured["analyzer_type"] is AnalyzerType.PRE_TRIAL
    assert captured["llm_client_type"] is LLMClientType.CLAUDE_CLI
    assert "sandbox_config" not in captured
    assert captured["cli_config"].cwd == tmp_path
    # Charges the task, and keeps the assignment's configured model.
    assert captured["task_id"] == "task_3"
    assert captured["model"] == "claude-haiku-4-5"
    assert run.status == JobStatus.SUCCESS


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
