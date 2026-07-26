import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import oddish.core.custom_qa as custom_qa
from oddish.core.prompts import set_prompt_core
from oddish.db import (
    PromptModel,
    TaskModel,
    TaskStatus,
    WorkerJobModel,
    get_session,
)
from oddish.db.models import AnalyzerRunModel, JobStatus, TrialModel, WorkerJobKind
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

    async def resolve_prompt(*args, **kwargs):
        return (
            SimpleNamespace(id="prompt_1", kind="QA_PRE_TRIAL", org_id="org_1"),
            SimpleNamespace(id="version_1", version=1, content="Audit this"),
        )

    async def enqueue(session, request):
        enqueued.append(request)
        return SimpleNamespace(id="job_1")

    monkeypatch.setattr(custom_qa, "_validate_scope", validate_scope)
    monkeypatch.setattr(custom_qa, "resolve_prompt_core", resolve_prompt)
    monkeypatch.setattr(custom_qa, "enqueue_worker_job", enqueue)

    responses = await custom_qa.run_custom_qa_core(
        FakeSession(),
        data=CustomQARunRequest(
            scope_type="task",
            scope_id="task_1",
            variants=[{"kind": "QA_PRE_TRIAL"}],
            backend="sandbox",
            allow_oddish_cli=True,
        ),
        org_id="org_1",
        user_id="user_1",
        oddish_api_base_url="https://api.test",
    )

    assert len(added) == 1
    assert added[0].status == JobStatus.QUEUED
    config = added[0].run_config
    assert config["oddish_api_scope"] == "read"
    assert config["oddish_cli_policy"] == (
        "read-only; inspection only; submissions and mutations forbidden"
    )
    assert config["oddish_api_base_url"] == "https://api.test"
    assert "read-only access" in config["system_prompt"]
    assert "Do not submit or mutate" in config["system_prompt"]
    assert "inspect or submit" not in config["system_prompt"]
    assert len(enqueued) == 1
    assert enqueued[0].kind == WorkerJobKind.ANALYZER_BLOCK
    assert enqueued[0].subject_table == "analyzer_runs"
    assert enqueued[0].subject_id == "run_1"
    assert enqueued[0].payload == {"analyzer_run_id": "run_1"}
    assert responses[0].status == JobStatus.QUEUED.value
    assert responses[0].analyzer_block_id == ""


@pytest.mark.asyncio
async def test_custom_qa_rejects_foreign_org_prompt(monkeypatch):
    # `variant.kind` is a free-form string with no vocabulary check, so it can
    # also resolve as another org's prompt id via the unscoped id fallback.
    class FakeSession:
        def add(self, row):
            pass

        async def flush(self):
            return None

        async def commit(self):
            return None

    async def validate_scope(*args, **kwargs):
        return None

    async def resolve_prompt(*args, **kwargs):
        return (
            SimpleNamespace(id="prompt_evil", kind="QA_PRE_TRIAL", org_id="org_2"),
            SimpleNamespace(id="version_1", version=1, content="Audit this"),
        )

    monkeypatch.setattr(custom_qa, "_validate_scope", validate_scope)
    monkeypatch.setattr(custom_qa, "resolve_prompt_core", resolve_prompt)

    with pytest.raises(HTTPException) as exc:
        await custom_qa.run_custom_qa_core(
            FakeSession(),
            data=CustomQARunRequest(
                scope_type="task",
                scope_id="task_1",
                variants=[{"kind": "prompt_evil"}],
                backend="api",
            ),
            org_id="org_1",
            user_id="user_1",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_trial_variant_resolution_passes_full_prompt_hierarchy(monkeypatch):
    seen = {}
    prompt = SimpleNamespace(id="prompt_1", kind="check", org_id="org_1")
    version = SimpleNamespace(id="version_1", version=1, content="Audit this")

    async def resolve_prompt(session, kind, **kwargs):
        seen.update(kwargs)
        return prompt, version

    monkeypatch.setattr(custom_qa, "resolve_prompt_core", resolve_prompt)
    trial = TrialModel(
        id="trial_1",
        task_id="task_1",
        experiment_id="experiment_1",
    )

    resolved = await custom_qa._resolve_variant(
        object(),
        "check",
        version=None,
        org_id="org_1",
        user_id="user_1",
        scope_type="trial",
        scope_id="trial_1",
        target=trial,
    )

    assert resolved == (prompt, version)
    assert seen == {
        "org_id": "org_1",
        "user_id": "user_1",
        "experiment_id": "experiment_1",
        "task_id": "task_1",
        "trial_id": "trial_1",
    }


# ---------------------------------------------------------------------------
# Finding 1 (integration): scope resolution against a real DB, exercising
# resolve_prompt_core rather than a mocked prompt lookup.
# ---------------------------------------------------------------------------


async def _seed_task(session, *, org_id: str) -> str:
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/custom-qa-fake-task",
            status=TaskStatus.COMPLETED,
        )
    )
    await session.flush()
    await session.commit()
    return task_id


async def _cleanup(session, *, kind: str, task_id: str, run_ids: list[str]) -> None:
    if run_ids:
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id.in_(run_ids)
            )
        )
        await session.execute(
            AnalyzerRunModel.__table__.delete().where(AnalyzerRunModel.id.in_(run_ids))
        )
    await session.execute(
        PromptModel.__table__.delete().where(PromptModel.kind == kind)
    )
    await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
    await session.commit()


@pytest.mark.asyncio
async def test_run_custom_qa_core_resolves_org_scoped_prompt_over_global():
    kind = f"test_custom_qa_scope_{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        task_id = await _seed_task(session, org_id=org_id)
        await set_prompt_core(session, kind=kind, content="global content")
        org_version = await set_prompt_core(
            session,
            kind=kind,
            content="org content",
            scope_type="org",
            scope_id=org_id,
            org_id=org_id,
        )
        await session.commit()

    run_ids: list[str] = []
    try:
        async with get_session() as session:
            responses = await custom_qa.run_custom_qa_core(
                session,
                data=CustomQARunRequest(
                    scope_type="task",
                    scope_id=task_id,
                    variants=[{"kind": kind}],
                    backend="api",
                ),
                org_id=org_id,
                user_id="user_1",
            )
        run_ids = [r.id for r in responses]

        assert responses[0].run_config["prompt"]["id"] == org_version.prompt_id
        assert responses[0].run_config["prompt_key"] == kind
        assert responses[0].run_config["prompt_version"] == org_version.version
    finally:
        async with get_session() as session:
            await _cleanup(session, kind=kind, task_id=task_id, run_ids=run_ids)


@pytest.mark.asyncio
async def test_run_custom_qa_core_falls_back_to_global_prompt():
    kind = f"test_custom_qa_scope_{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        task_id = await _seed_task(session, org_id=org_id)
        global_version = await set_prompt_core(
            session, kind=kind, content="global content"
        )
        await session.commit()

    run_ids: list[str] = []
    try:
        async with get_session() as session:
            responses = await custom_qa.run_custom_qa_core(
                session,
                data=CustomQARunRequest(
                    scope_type="task",
                    scope_id=task_id,
                    variants=[{"kind": kind}],
                    backend="api",
                ),
                org_id=org_id,
                user_id="user_1",
            )
        run_ids = [r.id for r in responses]

        assert responses[0].run_config["prompt"]["id"] == global_version.prompt_id
        assert responses[0].run_config["prompt_version"] == global_version.version
    finally:
        async with get_session() as session:
            await _cleanup(session, kind=kind, task_id=task_id, run_ids=run_ids)


@pytest.mark.asyncio
async def test_run_custom_qa_core_pinned_version_survives_scope_resolution():
    kind = f"test_custom_qa_scope_{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        task_id = await _seed_task(session, org_id=org_id)
        v1 = await set_prompt_core(session, kind=kind, content="v1 content")
        v2 = await set_prompt_core(session, kind=kind, content="v2 content")
        assert v1.prompt_id == v2.prompt_id
        assert (v1.version, v2.version) == (1, 2)
        await session.commit()

    run_ids: list[str] = []
    try:
        async with get_session() as session:
            responses = await custom_qa.run_custom_qa_core(
                session,
                data=CustomQARunRequest(
                    scope_type="task",
                    scope_id=task_id,
                    variants=[{"kind": kind, "version": 1}],
                    backend="api",
                ),
                org_id=org_id,
                user_id="user_1",
            )
        run_ids = [r.id for r in responses]

        assert responses[0].prompt_version == 1
        assert responses[0].run_config["prompt_version"] == 1
    finally:
        async with get_session() as session:
            await _cleanup(session, kind=kind, task_id=task_id, run_ids=run_ids)


@pytest.mark.asyncio
async def test_run_custom_qa_core_pinned_missing_version_404s():
    kind = f"test_custom_qa_scope_{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        task_id = await _seed_task(session, org_id=org_id)
        await set_prompt_core(session, kind=kind, content="v1 content")
        await session.commit()

    try:
        async with get_session() as session:
            with pytest.raises(HTTPException) as exc:
                await custom_qa.run_custom_qa_core(
                    session,
                    data=CustomQARunRequest(
                        scope_type="task",
                        scope_id=task_id,
                        variants=[{"kind": kind, "version": 7}],
                        backend="api",
                    ),
                    org_id=org_id,
                    user_id="user_1",
                )
        assert exc.value.status_code == 404
    finally:
        async with get_session() as session:
            await _cleanup(session, kind=kind, task_id=task_id, run_ids=[])
