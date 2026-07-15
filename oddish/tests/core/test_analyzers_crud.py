import pytest
from fastapi import HTTPException
from sqlalchemy import inspect

from oddish.core.analyzers import (
    create_analyzer_core,
    delete_analyzer_core,
    experiment_ids_for_analyzer,
    get_analyzer_core,
    list_experiment_options_core,
    list_analyzers_core,
)
from oddish.schemas import AnalyzerCreate
from oddish.db.models import ExperimentModel, JobStatus


@pytest.mark.asyncio
async def test_create_and_get_analyzer(session, monkeypatch):
    # Stub the enqueue so the test doesn't need a live dispatcher.
    import oddish.core.analyzers as mod
    calls = {}

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        calls["analyzer_id"] = analyzer_id

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    # analyzer_experiments.experiment_id FKs to experiments.id, so the rows
    # referenced by experiment_ids must exist first.
    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    e2 = ExperimentModel(name="exp-2", org_id="org_1")
    session.add_all([e1, e2])
    await session.flush()

    analyzer = await create_analyzer_core(
        session,
        data=AnalyzerCreate(name="Q3", experiment_ids=[e1.id, e2.id]),
        org_id="org_1", user_id="user_1",
    )

    assert analyzer.status == JobStatus.PENDING
    assert calls["analyzer_id"] == analyzer.id

    got = await get_analyzer_core(session, analyzer.id, org_id="org_1")
    assert got.name == "Q3"

    listed = await list_analyzers_core(session, org_id="org_1")
    assert any(r.id == analyzer.id for r in listed)

    exp_ids = await experiment_ids_for_analyzer(session, analyzer.id)
    assert sorted(exp_ids) == sorted([e1.id, e2.id])

    options = await list_experiment_options_core(session, org_id="org_1")
    option_ids = {o.id for o in options}
    assert {e1.id, e2.id} <= option_ids

    await delete_analyzer_core(session, analyzer.id, org_id="org_1")
    with pytest.raises(HTTPException) as exc:
        await get_analyzer_core(session, analyzer.id, org_id="org_1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_cancels_inflight_worker_job(session, monkeypatch):
    """Soft-deleting an analyzer must cancel its in-flight generation job so it
    stops running map/reduce LLM work (mirrors task/trial deletion)."""
    import oddish.core.analyzers as mod

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        pass

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    session.add(e1)
    await session.flush()

    analyzer = await create_analyzer_core(
        session,
        data=AnalyzerCreate(name="ToDelete", experiment_ids=[e1.id]),
        org_id="org_1", user_id="user_1",
    )

    # Capture the cancel UPDATE (it executes for real, matching zero rows here).
    executed: list[tuple[str, object]] = []
    real_execute = session.execute

    async def spy_execute(statement, params=None, *a, **k):
        executed.append((str(statement), params))
        return await real_execute(statement, params, *a, **k)

    monkeypatch.setattr(session, "execute", spy_execute)

    await delete_analyzer_core(session, analyzer.id, org_id="org_1")

    cancels = [
        (sql, p) for sql, p in executed
        if "UPDATE worker_jobs" in sql and "CANCELLED" in sql
    ]
    assert cancels, "delete did not issue a worker_jobs cancel"
    sql, params = cancels[0]
    assert params == {"analyzer_id": analyzer.id}
    assert "kind::text = 'ANALYZER'" in sql
    assert "subject_table = 'analyzers'" in sql
    # Only in-flight rows are cancelled; terminal ones are excluded.
    assert "('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')" in sql


@pytest.mark.asyncio
async def test_get_analyzer_core_404_for_unknown_id(session):
    with pytest.raises(HTTPException) as exc:
        await get_analyzer_core(session, "does-not-exist", org_id="org_1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_analyzer_rejects_unknown_or_foreign_org_experiment_id(
    session, monkeypatch
):
    import oddish.core.analyzers as mod
    from sqlalchemy import select

    from oddish.db.models import AnalyzerModel

    calls = {"enqueued": False}

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        calls["enqueued"] = True

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    foreign = ExperimentModel(name="exp-foreign", org_id="org_2")
    session.add_all([e1, foreign])
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await create_analyzer_core(
            session,
            data=AnalyzerCreate(
                name="Cross-org", experiment_ids=[e1.id, foreign.id, "does-not-exist"]
            ),
            org_id="org_1",
            user_id="user_1",
        )
    assert exc.value.status_code == 400

    # No orphan analyzer row and no enqueue on the rejected path.
    assert calls["enqueued"] is False
    rows = (await session.execute(select(AnalyzerModel))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_create_analyzer_dedupes_experiment_ids(session, monkeypatch):
    import oddish.core.analyzers as mod

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        pass

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    session.add(e1)
    await session.flush()

    analyzer = await create_analyzer_core(
        session,
        data=AnalyzerCreate(name="Dup", experiment_ids=[e1.id, e1.id]),
        org_id="org_1", user_id="user_1",
    )

    exp_ids = await experiment_ids_for_analyzer(session, analyzer.id)
    assert exp_ids == [e1.id]


@pytest.mark.asyncio
async def test_list_analyzers_core_does_not_load_findings(session, monkeypatch):
    """Regression gate for the load_only allowlist: if list_analyzers_core ever
    reverts to a bare ``select(AnalyzerModel)``, this catches it by asserting the
    ~500KB findings/models_by_task blobs stay unloaded off the list query, while
    a column the list view actually renders (name) stays loaded."""
    import oddish.core.analyzers as mod

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        pass

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    session.add(e1)
    await session.flush()

    analyzer = await create_analyzer_core(
        session,
        data=AnalyzerCreate(name="Q3", experiment_ids=[e1.id]),
        org_id="org_1", user_id="user_1",
    )
    analyzer.findings = [{"trial_id": "t1"}]
    analyzer.models_by_task = {"task": ["model"]}
    analyzer.vertical_scaling_content = "v-content"
    await session.flush()

    # The object create_analyzer_core returned is fully loaded in the identity
    # map; expire it so the next query actually has to decide what to fetch.
    session.expire_all()

    listed = await list_analyzers_core(session, org_id="org_1")
    row = next(r for r in listed if r.id == analyzer.id)

    unloaded = inspect(row).unloaded
    assert "findings" in unloaded
    assert "models_by_task" in unloaded
    assert "name" not in unloaded
    assert "vertical_scaling_content" not in unloaded


@pytest.mark.asyncio
async def test_create_analyzer_persists_save_trial_analyses(session, monkeypatch):
    import oddish.core.analyzers as mod

    async def _fake_enqueue(session, *, analyzer_id, org_id):
        pass

    monkeypatch.setattr(mod, "_enqueue_analyzer_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    session.add(e1)
    await session.flush()

    default_az = await create_analyzer_core(
        session,
        data=AnalyzerCreate(name="Default", experiment_ids=[e1.id]),
        org_id="org_1", user_id="user_1",
    )
    assert default_az.save_trial_analyses is False

    saving_az = await create_analyzer_core(
        session,
        data=AnalyzerCreate(
            name="Saving", experiment_ids=[e1.id], save_trial_analyses=True
        ),
        org_id="org_1", user_id="user_1",
    )
    assert saving_az.save_trial_analyses is True


def test_analyzer_model_has_vertical_scaling_content_column():
    from oddish.db.models import AnalyzerModel

    col = AnalyzerModel.__table__.columns["vertical_scaling_content"]
    assert col.nullable is True
