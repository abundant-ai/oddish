"""Resolution semantics for QA job assignments.

Assignments *union* across scopes, unlike prompt resolution which picks one
winner. Every test arranges its own prompt and assignment state -- nothing here
depends on the seeded registry, which is order-dependent and has repeatedly
been found empty on the shared dev database.
"""

import uuid

import pytest
import pytest_asyncio

from oddish.core.prompts import set_prompt_core
from oddish.core.qa_assignments import (
    GLOBAL_SCOPE_TYPE,
    enqueue_qa_assignment_runs_core,
    list_qa_assignments_core,
    normalize_scope,
    resolve_qa_assignments_core,
    upsert_qa_assignment_core,
)
from oddish.db import (
    AnalyzerRunModel,
    PromptModel,
    QAAssignmentModel,
    QAStage,
    WorkerJobModel,
    get_session,
)

POST = QAStage.POST_TRIAL.value
PRE = QAStage.PRE_TRIAL.value

_NO_SCOPES = {
    "org_id": None,
    "user_id": None,
    "experiment_id": None,
    "task_id": None,
    "trial_id": None,
}


@pytest_asyncio.fixture
async def kinds():
    """Throwaway kinds so tests never collide with seeded rows."""
    made: list[str] = []

    def _new(label: str) -> str:
        kind = f"test_qa_{label}_{uuid.uuid4().hex[:8]}"
        made.append(kind)
        return kind

    yield _new

    async with get_session() as session:
        for kind in made:
            rows = (
                await session.execute(
                    PromptModel.__table__.select().where(PromptModel.kind == kind)
                )
            ).all()
            for row in rows:
                await session.execute(
                    QAAssignmentModel.__table__.delete().where(
                        QAAssignmentModel.prompt_id == row.id
                    )
                )
            await session.execute(
                PromptModel.__table__.delete().where(PromptModel.kind == kind)
            )
        await session.commit()


async def _prompt(kind, *, scope_type=None, scope_id=None, org_id=None, content="c"):
    async with get_session() as session:
        version = await set_prompt_core(
            session,
            kind=kind,
            content=content,
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
        )
        prompt_id = version.prompt_id
        await session.commit()
    return prompt_id


async def _assign(
    prompt_id, *, stage=POST, scope_type, scope_id=None, org_id=None, **kw
):
    async with get_session() as session:
        prompt = await session.get(PromptModel, prompt_id)
        assignment = await upsert_qa_assignment_core(
            session,
            prompt=prompt,
            stage=stage,
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
            model=kw.pop("model", "claude-sonnet-4-6"),
            llm_client_type=kw.pop("llm_client_type", "Api"),
            **kw,
        )
        assignment_id = assignment.id
        await session.commit()
    return assignment_id


async def _resolve(**scopes):
    async with get_session() as session:
        return await resolve_qa_assignments_core(session, **{**_NO_SCOPES, **scopes})


# ---------------------------------------------------------------------------
# Union vs override -- the core decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_assignment_resolves_with_no_overrides(kinds):
    kind = kinds("global")
    prompt_id = await _prompt(kind)
    await _assign(prompt_id, scope_type=GLOBAL_SCOPE_TYPE)

    resolved = await _resolve(org_id="org_a")
    mine = [r for r in resolved if r.kind == kind]
    assert len(mine) == 1
    assert mine[0].inherited_from == "global"


@pytest.mark.asyncio
async def test_narrower_scope_overrides_same_kind(kinds):
    """An org default and a task override name *different* prompt rows for the
    same kind. Keying resolution on prompt_id would run both."""
    kind = kinds("override")
    global_prompt = await _prompt(kind, content="global")
    task_prompt = await _prompt(
        kind, scope_type="task", scope_id="task_a", org_id="org_a", content="task"
    )
    await _assign(global_prompt, scope_type=GLOBAL_SCOPE_TYPE, model="sonnet")
    await _assign(
        task_prompt, scope_type="task", scope_id="task_a", org_id="org_a", model="opus"
    )

    resolved = await _resolve(org_id="org_a", task_id="task_a")
    mine = [r for r in resolved if r.kind == kind]
    assert len(mine) == 1, "override must replace, not add a second job"
    assert mine[0].assignment.model == "opus"
    assert mine[0].inherited_from == "task:task_a"


@pytest.mark.asyncio
async def test_task_extra_job_does_not_disable_global_default(kinds):
    """The case that motivated union: adding a leak check to one task must not
    silently switch off the installation's default post-trial QA."""
    default_kind = kinds("default")
    extra_kind = kinds("extra")
    await _assign(await _prompt(default_kind), scope_type=GLOBAL_SCOPE_TYPE)
    await _assign(
        await _prompt(extra_kind, scope_type="task", scope_id="task_a", org_id="org_a"),
        scope_type="task",
        scope_id="task_a",
        org_id="org_a",
    )

    resolved = await _resolve(org_id="org_a", task_id="task_a")
    kinds_found = {r.kind for r in resolved}
    assert default_kind in kinds_found, "task addition disabled the global default"
    assert extra_kind in kinds_found


@pytest.mark.asyncio
async def test_disabled_narrow_row_suppresses_broader_default(kinds):
    kind = kinds("suppress")
    await _assign(await _prompt(kind), scope_type=GLOBAL_SCOPE_TYPE)
    await _assign(
        await _prompt(kind, scope_type="task", scope_id="task_a", org_id="org_a"),
        scope_type="task",
        scope_id="task_a",
        org_id="org_a",
        enabled=False,
    )

    resolved = await _resolve(org_id="org_a", task_id="task_a")
    assert kind not in {r.kind for r in resolved}
    # ...but only for the scope that suppressed it.
    elsewhere = await _resolve(org_id="org_a", task_id="task_b")
    assert kind in {r.kind for r in elsewhere}


@pytest.mark.asyncio
async def test_task_outranks_experiment(kinds):
    """Not a containment fact -- a deliberate call, matching
    resolve_prompt_core: a task override encodes durable knowledge about that
    task, an experiment override only intent for one run."""
    kind = kinds("task_beats_exp")
    await _assign(
        await _prompt(kind, scope_type="task", scope_id="task_a", org_id="org_a"),
        scope_type="task",
        scope_id="task_a",
        org_id="org_a",
        model="task-model",
    )
    await _assign(
        await _prompt(kind, scope_type="experiment", scope_id="exp_a", org_id="org_a"),
        scope_type="experiment",
        scope_id="exp_a",
        org_id="org_a",
        model="experiment-model",
    )

    resolved = await _resolve(org_id="org_a", task_id="task_a", experiment_id="exp_a")
    mine = [r for r in resolved if r.kind == kind]
    assert len(mine) == 1
    assert mine[0].assignment.model == "task-model"


@pytest.mark.asyncio
async def test_stages_are_independent(kinds):
    """The key is (stage, kind): the same prompt bound to both stages is two
    jobs, which is why rollups cannot key on analyzer_blocks.prompt_id."""
    kind = kinds("both_stages")
    prompt_id = await _prompt(kind)
    await _assign(
        prompt_id, stage=PRE, scope_type=GLOBAL_SCOPE_TYPE, llm_client_type="Sandbox"
    )
    await _assign(prompt_id, stage=POST, scope_type=GLOBAL_SCOPE_TYPE)

    resolved = await _resolve(org_id="org_a")
    stages = {r.stage for r in resolved if r.kind == kind}
    assert stages == {PRE, POST}

    only_post = await _resolve(org_id="org_a")
    async with get_session() as session:
        filtered = await resolve_qa_assignments_core(
            session, stage=POST, **{**_NO_SCOPES, "org_id": "org_a"}
        )
    assert {r.stage for r in filtered if r.kind == kind} == {POST}
    assert only_post  # sanity: unfiltered call still returned rows


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_orgs_assignment_is_invisible(kinds):
    kind = kinds("tenant")
    await _assign(
        await _prompt(kind, scope_type="task", scope_id="task_shared", org_id="org_a"),
        scope_type="task",
        scope_id="task_shared",
        org_id="org_a",
    )

    seen_by_b = await _resolve(org_id="org_b", task_id="task_shared")
    assert kind not in {r.kind for r in seen_by_b}
    seen_by_a = await _resolve(org_id="org_a", task_id="task_shared")
    assert kind in {r.kind for r in seen_by_a}


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassigning_same_scope_updates_in_place(kinds):
    kind = kinds("upsert")
    prompt_id = await _prompt(kind)
    first = await _assign(prompt_id, scope_type=GLOBAL_SCOPE_TYPE, model="a")
    second = await _assign(prompt_id, scope_type=GLOBAL_SCOPE_TYPE, model="b")
    assert first == second, "re-assigning must repoint the row, not add a rival"

    resolved = await _resolve(org_id="org_a")
    mine = [r for r in resolved if r.kind == kind]
    assert len(mine) == 1
    assert mine[0].assignment.model == "b"


@pytest.mark.asyncio
async def test_pinned_version_beats_latest(kinds):
    kind = kinds("pin")
    prompt_id = await _prompt(kind, content="v1")
    await _prompt(kind, content="v2")  # appends version 2 to the same row

    await _assign(prompt_id, scope_type=GLOBAL_SCOPE_TYPE)
    unpinned = [r for r in await _resolve(org_id="org_a") if r.kind == kind]
    assert unpinned[0].effective_version == 2

    await _assign(prompt_id, scope_type=GLOBAL_SCOPE_TYPE, prompt_version=1)
    pinned = [r for r in await _resolve(org_id="org_a") if r.kind == kind]
    assert pinned[0].effective_version == 1


@pytest.mark.asyncio
async def test_defined_scope_listing_includes_disabled_rows(kinds):
    """`resolved=false` feeds the edit UI: a suppression row must be visible in
    order to be removable."""
    kind = kinds("defined")
    await _assign(
        await _prompt(kind, scope_type="task", scope_id="task_a", org_id="org_a"),
        scope_type="task",
        scope_id="task_a",
        org_id="org_a",
        enabled=False,
    )
    async with get_session() as session:
        rows = await list_qa_assignments_core(
            session, scope_type="task", scope_id="task_a", org_id="org_a"
        )
    assert [r.kind for r in rows if r.kind == kind] == [kind]
    assert rows[0].assignment.enabled is False


@pytest.mark.asyncio
async def test_cli_access_requires_sandbox_backend(kinds):
    kind = kinds("cli_guard")
    with pytest.raises(ValueError, match="sandbox"):
        await _assign(
            await _prompt(kind),
            scope_type=GLOBAL_SCOPE_TYPE,
            llm_client_type="Api",
            allow_oddish_cli=True,
        )


def test_global_scope_normalizes_to_the_not_null_pair():
    assert normalize_scope(GLOBAL_SCOPE_TYPE, None) == (GLOBAL_SCOPE_TYPE, "")


def test_domain_scope_requires_an_id():
    with pytest.raises(ValueError, match="scope_id"):
        normalize_scope("task", None)


@pytest.mark.asyncio
async def test_lifecycle_event_enqueues_analyzer_block_once(kinds):
    kind = kinds("automatic")
    assignment_id = await _assign(
        await _prompt(
            kind,
            scope_type="task",
            scope_id="task_auto",
            org_id="org_a",
            content="inspect this lifecycle event",
        ),
        stage=POST,
        scope_type="task",
        scope_id="task_auto",
        org_id="org_a",
        model="claude-sonnet-4-6",
    )

    async with get_session() as session:
        first = await enqueue_qa_assignment_runs_core(
            session,
            stage=POST,
            stage_event_key="trial:trial_auto",
            org_id="org_a",
            user_id="user_a",
            experiment_id="exp_auto",
            task_id="task_auto",
            trial_id="trial_auto",
            run_scope_type="trial",
            run_scope_id="trial_auto",
        )
        await session.commit()

    assert len(first) == 1
    run_id = first[0].id
    assert first[0].qa_assignment_id == assignment_id
    assert first[0].run_config["automatic"] is True
    assert first[0].run_config["stage"] == POST
    assert first[0].run_config["scope"] == {
        "type": "trial",
        "id": "trial_auto",
    }

    async with get_session() as session:
        second = await enqueue_qa_assignment_runs_core(
            session,
            stage=POST,
            stage_event_key="trial:trial_auto",
            org_id="org_a",
            user_id="user_a",
            experiment_id="exp_auto",
            task_id="task_auto",
            trial_id="trial_auto",
            run_scope_type="trial",
            run_scope_id="trial_auto",
        )
        jobs = (
            await session.execute(
                WorkerJobModel.__table__.select().where(
                    WorkerJobModel.subject_table == "analyzer_runs",
                    WorkerJobModel.subject_id == run_id,
                )
            )
        ).all()
        assert second == []
        assert len(jobs) == 1

        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_table == "analyzer_runs",
                WorkerJobModel.subject_id == run_id,
            )
        )
        await session.execute(
            AnalyzerRunModel.__table__.delete().where(AnalyzerRunModel.id == run_id)
        )
        await session.commit()
