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


@pytest.mark.asyncio
async def test_run_report_generation_job_skips_persist_when_reaped(monkeypatch):
    """Mirrors qa_handler's cancellation-safety intent: if the worker_jobs row
    is no longer the live owner by the time the long work finishes (e.g. it
    was reaped and re-claimed elsewhere), the report row must not be written.
    """
    import oddish.workers.queue.report_handler as rh
    from oddish.db.models import JobStatus

    class _FakeReport:
        def __init__(self):
            self.status = JobStatus.PENDING
            self.org_id = "org1"
            self.started_at = None
            self.finished_at = None
            self.error = None
            self.num_trials = None

    report = _FakeReport()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, id_, with_for_update=False):
            return report

    monkeypatch.setattr(rh, "get_session", lambda: _FakeSession())

    liveness_calls = {"n": 0}

    async def fake_is_running(session, worker_job_id, *, with_for_update=False):
        liveness_calls["n"] += 1
        # 1st call: initial RUNNING guard (job still alive). 2nd call: persist
        # guard (job was reaped mid-run and no longer owns the work).
        return liveness_calls["n"] == 1

    monkeypatch.setattr(rh, "_worker_job_is_running", fake_is_running)

    async def fake_gather(session, report_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    class _Inputs:
        pass

    async def fake_build_inputs(rows):
        return _Inputs()

    monkeypatch.setattr(rh, "build_report_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}

    async def fake_run_eval(inputs, config):
        return _Output()

    monkeypatch.setattr(rh, "run_report_eval", fake_run_eval)

    async def fake_heartbeat(*, worker_job_id, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(rh, "_heartbeat_report_worker_job", fake_heartbeat)

    await rh.run_report_generation_job("r1", worker_job_id="job-1")

    assert liveness_calls["n"] == 2
    # Persist bailed out: status is still RUNNING (set by step 1), never SUCCESS.
    assert report.status == JobStatus.RUNNING
    assert report.finished_at is None
    assert report.num_trials is None


@pytest.mark.asyncio
async def test_run_report_generation_job_stops_mid_loop_on_reap(monkeypatch):
    """Mirrors qa_handler's interior gating: if the worker job is reaped
    while iterating trials needing classification, the loop must stop
    immediately and not classify any further trials.
    """
    import oddish.workers.queue.report_handler as rh
    from oddish.db.models import JobStatus

    class _FakeReport:
        def __init__(self):
            self.status = JobStatus.PENDING
            self.org_id = "org1"
            self.started_at = None
            self.finished_at = None
            self.error = None
            self.num_trials = None

    report = _FakeReport()

    class _FakeTrial:
        def __init__(self, id_):
            self.id = id_
            self.analysis_status = JobStatus.PENDING

    trials_by_id = {"t1": _FakeTrial("t1"), "t2": _FakeTrial("t2")}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, id_, with_for_update=False):
            if model is rh.ReportModel:
                return report
            return trials_by_id.get(id_)

    monkeypatch.setattr(rh, "get_session", lambda: _FakeSession())

    liveness_calls = {"n": 0}

    async def fake_is_running(session, worker_job_id, *, with_for_update=False):
        liveness_calls["n"] += 1
        # 1: initial job-RUNNING guard. 2: loop-top check before t1 (still
        # live). 3: loop-top check before t2 (reaped mid-loop -> stop).
        return liveness_calls["n"] <= 2

    monkeypatch.setattr(rh, "_worker_job_is_running", fake_is_running)

    async def fake_gather(session, report_id, org_id):
        return [(trials_by_id["t1"], "task1"), (trials_by_id["t2"], "task2")]

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    classify_calls = []

    async def fake_classify(tid, should_store=None):
        classify_calls.append(tid)

    monkeypatch.setattr(rh, "classify_trial_and_store", fake_classify)

    async def fake_heartbeat(*, worker_job_id, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(rh, "_heartbeat_report_worker_job", fake_heartbeat)

    await rh.run_report_generation_job("r1", worker_job_id="job-1")

    # Only the first trial was classified before the reap was detected.
    assert classify_calls == ["t1"]
    # The job stopped before reaching persist; status is unchanged from RUNNING.
    assert report.status == JobStatus.RUNNING
    assert report.finished_at is None
