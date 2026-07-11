import pytest

from oddish.db.models import WorkerJobKind
from oddish.workers.jobs import ensure_builtin_handlers_registered
from oddish.workers.jobs.registry import get_handler


def test_report_handler_registered():
    ensure_builtin_handlers_registered()
    handler = get_handler(WorkerJobKind.REPORT)
    assert handler.kind == WorkerJobKind.REPORT
    assert handler.validate_payload({"report_id": "r1"}) == {"report_id": "r1"}


class _Job:
    def __init__(self, report_id):
        self.subject_id = report_id
        self.payload = {"report_id": report_id}
        self.queue_key = "qa"
        self.modal_function_call_id = None
        self.id = "job-1"


@pytest.mark.asyncio
async def test_handler_run_maps_status_to_outcome(monkeypatch):
    import oddish.workers.jobs.handlers as h

    # run_report_generation_job is stubbed; status is read back from the report row.
    async def fake_run(report_id, *, worker_job_id=None):
        fake_run.called = report_id
    monkeypatch.setattr(h, "run_report_generation_job", fake_run)

    class _Report:
        status = __import__("oddish.db.models", fromlist=["JobStatus"]).JobStatus.SUCCESS
        error = None

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Report()
    monkeypatch.setattr(h, "get_session", lambda: _Session())

    outcome = await h.ReportJobHandler().run(_Job("r1"))
    assert outcome.failure is None  # JobOutcome.ok()
    assert fake_run.called == "r1"
