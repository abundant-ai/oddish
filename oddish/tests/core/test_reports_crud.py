import pytest
from fastapi import HTTPException

from oddish.core.reports import (
    create_report_core,
    delete_report_core,
    experiment_ids_for_report,
    get_report_core,
    list_experiment_options_core,
    list_reports_core,
)
from oddish.schemas import ReportCreate
from oddish.db.models import ExperimentModel, JobStatus


@pytest.mark.asyncio
async def test_create_and_get_report(session, monkeypatch):
    # Stub the enqueue so the test doesn't need a live dispatcher.
    import oddish.core.reports as mod
    calls = {}

    async def _fake_enqueue(session, *, report_id, org_id):
        calls["report_id"] = report_id

    monkeypatch.setattr(mod, "_enqueue_report_worker_job", _fake_enqueue)

    # report_experiments.experiment_id FKs to experiments.id, so the rows
    # referenced by experiment_ids must exist first.
    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    e2 = ExperimentModel(name="exp-2", org_id="org_1")
    session.add_all([e1, e2])
    await session.flush()

    report = await create_report_core(
        session,
        data=ReportCreate(name="Q3", experiment_ids=[e1.id, e2.id]),
        org_id="org_1", user_id="user_1",
    )

    assert report.status == JobStatus.PENDING
    assert calls["report_id"] == report.id

    got = await get_report_core(session, report.id, org_id="org_1")
    assert got.name == "Q3"

    listed = await list_reports_core(session, org_id="org_1")
    assert any(r.id == report.id for r in listed)

    exp_ids = await experiment_ids_for_report(session, report.id)
    assert sorted(exp_ids) == sorted([e1.id, e2.id])

    options = await list_experiment_options_core(session, org_id="org_1")
    option_ids = {o.id for o in options}
    assert {e1.id, e2.id} <= option_ids

    await delete_report_core(session, report.id, org_id="org_1")
    with pytest.raises(HTTPException) as exc:
        await get_report_core(session, report.id, org_id="org_1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_report_core_404_for_unknown_id(session):
    with pytest.raises(HTTPException) as exc:
        await get_report_core(session, "does-not-exist", org_id="org_1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_report_dedupes_experiment_ids(session, monkeypatch):
    import oddish.core.reports as mod

    async def _fake_enqueue(session, *, report_id, org_id):
        pass

    monkeypatch.setattr(mod, "_enqueue_report_worker_job", _fake_enqueue)

    e1 = ExperimentModel(name="exp-1", org_id="org_1")
    session.add(e1)
    await session.flush()

    report = await create_report_core(
        session,
        data=ReportCreate(name="Dup", experiment_ids=[e1.id, e1.id]),
        org_id="org_1", user_id="user_1",
    )

    exp_ids = await experiment_ids_for_report(session, report.id)
    assert exp_ids == [e1.id]
