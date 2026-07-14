import pytest
from fastapi import HTTPException

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
