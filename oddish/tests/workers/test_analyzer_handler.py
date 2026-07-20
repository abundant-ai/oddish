import pytest

from oddish.config import settings, to_anthropic_api_model_id
from oddish.db.models import WorkerJobKind
from oddish.evals.analyzer.taxonomy import Taxonomy
from oddish.workers.jobs import ensure_builtin_handlers_registered
from oddish.workers.jobs.registry import get_handler


async def _fake_load_taxonomy(session):
    return Taxonomy()


def test_build_analyzer_eval_config_uses_settings_model_and_default_concurrency(
    monkeypatch
):
    import oddish.workers.queue.analyzer_handler as rh

    monkeypatch.delenv("ODDISH_ANALYZER_CONCURRENCY", raising=False)
    config = rh._build_analyzer_eval_config()
    assert config.analysis_model == to_anthropic_api_model_id(settings.analysis_model)
    assert config.map_concurrency == 16  # AnalyzerEvalConfig default


def test_build_analyzer_eval_config_reads_concurrency_env(monkeypatch):
    import oddish.workers.queue.analyzer_handler as rh

    monkeypatch.setenv("ODDISH_ANALYZER_CONCURRENCY", "4")
    config = rh._build_analyzer_eval_config()
    assert config.map_concurrency == 4


def test_analyzer_handler_registered():
    ensure_builtin_handlers_registered()
    handler = get_handler(WorkerJobKind.ANALYZER)
    assert handler.kind == WorkerJobKind.ANALYZER
    assert handler.validate_payload({"analyzer_id": "r1"}) == {"analyzer_id": "r1"}


@pytest.mark.asyncio
async def test_handler_resets_terminal_status_before_retry(monkeypatch):
    """A retry must clear a prior FAILED status, else run_analyzer_generation_job
    early-exits and the retry is a no-op."""
    import oddish.workers.jobs.handlers as h
    from oddish.db.models import JobStatus

    class _Analyzer:
        def __init__(self):
            self.status = JobStatus.FAILED  # left FAILED by a previous attempt
            self.error = "transient boom"
            self.finished_at = "then"

    analyzer = _Analyzer()

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, model, id_, with_for_update=False): return analyzer
    monkeypatch.setattr(h, "get_session", lambda: _Session())

    seen = {}

    async def fake_run(analyzer_id, *, worker_job_id=None, eval_rows_fn=None):
        # By the time generation runs, the terminal state must be cleared.
        seen["status_at_run"] = analyzer.status
        analyzer.status = JobStatus.SUCCESS  # this attempt succeeds
    monkeypatch.setattr(h, "run_analyzer_generation_job", fake_run)

    outcome = await h.AnalyzerJobHandler().run(_Job("r1"))

    assert seen["status_at_run"] == JobStatus.QUEUED  # reset happened
    assert analyzer.error is None and analyzer.finished_at is None
    assert outcome.failure is None  # ok()


class _Job:
    def __init__(self, analyzer_id):
        self.subject_id = analyzer_id
        self.payload = {"analyzer_id": analyzer_id}
        self.queue_key = "qa"
        self.modal_function_call_id = None
        self.id = "job-1"


@pytest.mark.asyncio
async def test_handler_run_maps_status_to_outcome(monkeypatch):
    import oddish.workers.jobs.handlers as h

    # run_analyzer_generation_job is stubbed; status is read back from the analyzer row.
    async def fake_run(analyzer_id, *, worker_job_id=None, eval_rows_fn=None):
        fake_run.called = analyzer_id
    monkeypatch.setattr(h, "run_analyzer_generation_job", fake_run)

    class _Analyzer:
        status = __import__("oddish.db.models", fromlist=["JobStatus"]).JobStatus.SUCCESS
        error = None

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Analyzer()
    monkeypatch.setattr(h, "get_session", lambda: _Session())

    outcome = await h.AnalyzerJobHandler().run(_Job("r1"))
    assert outcome.failure is None  # JobOutcome.ok()
    assert fake_run.called == "r1"


@pytest.mark.asyncio
async def test_run_analyzer_generation_job_skips_persist_when_reaped(monkeypatch):
    """Mirrors qa_handler's cancellation-safety intent: if the worker_jobs row
    is no longer the live owner by the time the long work finishes (e.g. it
    was reaped and re-claimed elsewhere), the analyzer row must not be written.
    """
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    class _FakeAnalyzer:
        def __init__(self):
            self.status = JobStatus.PENDING
            self.org_id = "org1"
            self.started_at = None
            self.finished_at = None
            self.error = None
            self.num_trials = None
            self.save_trial_analyses = False

    analyzer = _FakeAnalyzer()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, id_, with_for_update=False):
            return analyzer

    monkeypatch.setattr(rh, "get_session", lambda: _FakeSession())

    liveness_calls = {"n": 0}

    async def fake_is_running(session, worker_job_id, *, with_for_update=False):
        liveness_calls["n"] += 1
        # 1st call: initial RUNNING guard (job still alive). 2nd call: persist
        # guard (job was reaped mid-run and no longer owns the work).
        return liveness_calls["n"] == 1

    monkeypatch.setattr(rh, "_worker_job_is_running", fake_is_running)

    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    class _Inputs:
        pass

    async def fake_build_inputs(rows):
        return _Inputs()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}

    async def fake_run_eval(inputs, config, **kwargs):
        return _Output()

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)

    async def fake_heartbeat(*, worker_job_id, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(rh, "_heartbeat_analyzer_worker_job", fake_heartbeat)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert liveness_calls["n"] == 2
    # Persist bailed out: status is still RUNNING (set by step 1), never SUCCESS.
    assert analyzer.status == JobStatus.RUNNING
    assert analyzer.finished_at is None
    assert analyzer.num_trials is None


def _install_owned_analyzer(monkeypatch, rh, JobStatus):
    """Shared scaffolding: a live analyzer whose worker job stays owned so the
    persist path actually writes (used by the failure/cancel tests)."""

    class _FakeAnalyzer:
        def __init__(self):
            self.status = JobStatus.PENDING
            self.org_id = "org1"
            self.started_at = None
            self.finished_at = None
            self.error = None
            self.num_trials = None
            self.save_trial_analyses = False

    analyzer = _FakeAnalyzer()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, id_, with_for_update=False):
            return analyzer

    monkeypatch.setattr(rh, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(rh, "load_taxonomy", _fake_load_taxonomy)

    async def always_running(session, worker_job_id, *, with_for_update=False):
        return True  # job keeps ownership through persist

    monkeypatch.setattr(rh, "_worker_job_is_running", always_running)

    async def fake_heartbeat(*, worker_job_id, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(rh, "_heartbeat_analyzer_worker_job", fake_heartbeat)
    return analyzer


@pytest.mark.asyncio
async def test_gather_failure_marks_failed_not_stuck_running(monkeypatch):
    """A failure in the trial-gather step (step 2) must still reach persist and
    mark the analyzer FAILED — not leave it stranded in RUNNING."""
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    async def boom_gather(session, analyzer_id, org_id):
        raise RuntimeError("db exploded during gather")

    monkeypatch.setattr(rh, "_gather_trial_rows", boom_gather)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.FAILED
    assert analyzer.finished_at is not None
    assert "db exploded during gather" in analyzer.error


@pytest.mark.asyncio
async def test_cancelled_error_marks_failed_not_stuck_running(monkeypatch):
    """CancelledError (a BaseException, not Exception) during the eval must be
    caught so persist runs and the analyzer ends FAILED with a clear reason."""
    import asyncio

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    async def fake_build_inputs(rows):
        return object()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    async def cancel_eval(inputs, config, *, taxonomy=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(rh, "run_analyzer_eval", cancel_eval)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.FAILED
    assert analyzer.finished_at is not None
    assert "cancelled" in analyzer.error.lower()


@pytest.mark.asyncio
async def test_run_analyzer_generation_job_stops_mid_loop_on_reap(monkeypatch):
    """Mirrors qa_handler's interior gating: if the worker job is reaped
    while iterating trials needing classification, the loop must stop
    immediately and not classify any further trials.
    """
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    class _FakeAnalyzer:
        def __init__(self):
            self.status = JobStatus.PENDING
            self.org_id = "org1"
            self.started_at = None
            self.finished_at = None
            self.error = None
            self.num_trials = None
            self.save_trial_analyses = False

    analyzer = _FakeAnalyzer()

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
            if model is rh.AnalyzerModel:
                return analyzer
            return trials_by_id.get(id_)

    monkeypatch.setattr(rh, "get_session", lambda: _FakeSession())

    liveness_calls = {"n": 0}

    async def fake_is_running(session, worker_job_id, *, with_for_update=False):
        liveness_calls["n"] += 1
        # 1: initial job-RUNNING guard. 2: loop-top check before t1 (still
        # live). 3: loop-top check before t2 (reaped mid-loop -> stop).
        return liveness_calls["n"] <= 2

    monkeypatch.setattr(rh, "_worker_job_is_running", fake_is_running)

    async def fake_gather(session, analyzer_id, org_id):
        return [(trials_by_id["t1"], "task1"), (trials_by_id["t2"], "task2")]

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    classify_calls = []

    async def fake_classify(tid, should_store=None):
        classify_calls.append(tid)

    monkeypatch.setattr(rh, "classify_trial_and_store", fake_classify)

    async def fake_heartbeat(*, worker_job_id, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(rh, "_heartbeat_analyzer_worker_job", fake_heartbeat)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    # Only the first trial was classified before the reap was detected.
    assert classify_calls == ["t1"]
    # The job stopped before reaching persist; status is unchanged from RUNNING.
    assert analyzer.status == JobStatus.RUNNING
    assert analyzer.finished_at is None



def _fake_output_with_findings():
    from oddish.evals.analyzer.schemas import Finding

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 1, "bad": 1, "good": 0}
        breakdown = {}
        reduce_prompt = "rp"
        proposals = []
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = [
            Finding(
                trial_id="t1", bucket="bad", subcategory="3a", evidence_quote="q",
                step_ids=[1], root_cause="rc", headroom_signal="hs",
                trajectory_link="link",
            )
        ]

    return _Output()


def _fake_inputs_with_subanalyses():
    from oddish.evals.primitives import SubAnalysis

    class _Inputs:
        bundles = []
        subanalyses = [
            SubAnalysis(
                trial_id="t1", trajectory_link="link", classification="c",
                subtype="st", evidence="ev", root_cause="rc", recommendation="rec",
            )
        ]

    return _Inputs()


class _RecordingStorage:
    def __init__(self):
        self.calls = []

    async def upload_bytes(self, data, s3_key, *, content_type=None):
        self.calls.append({"data": data, "s3_key": s3_key, "content_type": content_type})


def _wire_eval_paths(monkeypatch, rh, *, output, inputs):
    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    async def fake_build_inputs(rows):
        return inputs

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    async def fake_run_eval(inp, config, **kwargs):
        # Mirrors core: the real run_analyzer_eval carries the inputs'
        # subanalyses through onto the output, which is where the trial-analyses
        # export reads them from.
        output.subanalyses = inp.subanalyses
        return output

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)


@pytest.mark.asyncio
async def test_save_trial_analyses_uploads_when_flag_set(monkeypatch):
    import json

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)
    analyzer.save_trial_analyses = True

    _wire_eval_paths(
        monkeypatch, rh,
        output=_fake_output_with_findings(),
        inputs=_fake_inputs_with_subanalyses(),
    )

    storage = _RecordingStorage()
    monkeypatch.setattr(rh, "get_storage_client", lambda: storage)

    await rh.run_analyzer_generation_job("az1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.SUCCESS
    assert len(storage.calls) == 1
    call = storage.calls[0]
    assert call["s3_key"] == "analyzers/az1/trial_analyses.json"
    assert call["content_type"] == "application/json"
    payload = json.loads(call["data"].decode("utf-8"))
    assert payload["analyzer_id"] == "az1"
    assert payload["trials"][0]["trial_id"] == "t1"
    assert payload["trials"][0]["finding"]["subcategory"] == "3a"
    assert payload["trials"][0]["subanalysis"]["classification"] == "c"


@pytest.mark.asyncio
async def test_no_upload_when_flag_unset(monkeypatch):
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)
    analyzer.save_trial_analyses = False

    _wire_eval_paths(
        monkeypatch, rh,
        output=_fake_output_with_findings(),
        inputs=_fake_inputs_with_subanalyses(),
    )

    storage = _RecordingStorage()
    monkeypatch.setattr(rh, "get_storage_client", lambda: storage)

    await rh.run_analyzer_generation_job("az1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.SUCCESS
    assert storage.calls == []


@pytest.mark.asyncio
async def test_upload_failure_does_not_fail_job(monkeypatch):
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)
    analyzer.save_trial_analyses = True

    _wire_eval_paths(
        monkeypatch, rh,
        output=_fake_output_with_findings(),
        inputs=_fake_inputs_with_subanalyses(),
    )

    class _BoomStorage:
        async def upload_bytes(self, *a, **k):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(rh, "get_storage_client", lambda: _BoomStorage())

    await rh.run_analyzer_generation_job("az1", worker_job_id="job-1")

    # Best-effort: aggregate result still persisted SUCCESS despite S3 failure.
    assert analyzer.status == JobStatus.SUCCESS
    assert analyzer.error is None


@pytest.mark.asyncio
async def test_cancel_during_upload_keeps_successful_eval(monkeypatch):
    """A cancel in the best-effort upload must not discard the finished analysis.

    CancelledError is a BaseException, so it escapes _maybe_save_trial_analyses'
    ``except Exception`` and surfaces in the job's cancel handler, which must keep
    the already-computed output rather than reporting the job FAILED.
    """
    import asyncio

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)
    analyzer.save_trial_analyses = True

    _wire_eval_paths(
        monkeypatch, rh,
        output=_fake_output_with_findings(),
        inputs=_fake_inputs_with_subanalyses(),
    )

    class _CancellingStorage:
        async def upload_bytes(self, *a, **k):
            raise asyncio.CancelledError()

    monkeypatch.setattr(rh, "get_storage_client", lambda: _CancellingStorage())

    await rh.run_analyzer_generation_job("az1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.SUCCESS
    assert analyzer.error is None
    assert analyzer.num_trials == 1


@pytest.mark.asyncio
async def test_persist_writes_reduce_prompt_on_success(monkeypatch):
    """The reduce prompt from the eval output is persisted onto the analyzer row."""
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    async def fake_build_inputs(rows):
        return object()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 1, "bad": 1, "good": 0}
        breakdown = {"1b": 1}
        reduce_prompt = "the reduce prompt text"
        proposals = []
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = []

    async def fake_run_eval(inputs, config, **kwargs):
        return _Output()

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.status == JobStatus.SUCCESS
    assert analyzer.reduce_prompt == "the reduce prompt text"


@pytest.mark.asyncio
async def test_store_persists_findings(monkeypatch):
    """The map phase's findings used to be computed and dropped on the floor."""
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus
    from oddish.evals.analyzer.schemas import Finding

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    async def fake_build_inputs(rows):
        return object()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 1, "bad": 1, "good": 0}
        breakdown = {"1b": 1}
        reduce_prompt = "rp"
        proposals = []
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = [
            Finding(
                trial_id="t1", bucket="bad", subcategory="1b",
                evidence_quote="q", step_ids=[3], root_cause="rc",
                headroom_signal="", trajectory_link="/tasks/task/probe/t1",
                model="claude-opus-4-8",
            )
        ]

    async def fake_run_eval(inputs, config, **kwargs):
        return _Output()

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.findings == [
        {
            "trial_id": "t1", "bucket": "bad", "subcategory": "1b",
            "evidence_quote": "q", "step_ids": [3], "root_cause": "rc",
            "headroom_signal": "", "trajectory_link": "/tasks/task/probe/t1",
            "model": "claude-opus-4-8",
            "classification": None, "subtype": None,
            "task_id": None, "task_path": None,
            "capability_slug": None,
        }
    ]


@pytest.mark.asyncio
async def test_store_persists_models_by_task(monkeypatch):
    """The roster is the Task Construction denominator. Findings only record
    failures, so a model that PASSED a task appears nowhere in them."""
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus
    from oddish.evals.analyzer.schemas import Finding

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    class _Trial:
        def __init__(self, id_, model):
            self.id = id_
            self.task_id = "task"
            self.model = model
            self.analysis_status = JobStatus.SUCCESS  # already analyzed; skip classify

    trial_a = _Trial("t1", "model-a")  # fails -> produces a finding
    trial_b = _Trial("t2", "model-b")  # passes -> no finding, must still appear
    trials_by_id = {"t1": trial_a, "t2": trial_b}

    async def fake_gather(session, analyzer_id, org_id):
        return [(trial_a, "task_path"), (trial_b, "task_path")]

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    # _install_owned_analyzer's session.get() always returns the fake analyzer,
    # regardless of model class -- fine for the other tests, which pass empty
    # trial rows and never reach the classify-loop's TrialModel lookup. This
    # test needs a real trial back for that lookup, so use a session that
    # branches on the requested model class.
    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, model, id_, with_for_update=False):
            if model is rh.AnalyzerModel:
                return analyzer
            return trials_by_id.get(id_)

    monkeypatch.setattr(rh, "get_session", lambda: _Session())
    monkeypatch.setattr(rh, "load_taxonomy", _fake_load_taxonomy)

    async def fake_build_inputs(rows):
        return object()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 2, "bad": 1, "good": 1}
        breakdown = {"1b": 1}
        reduce_prompt = "rp"
        proposals = []
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = [
            Finding(
                trial_id="t1", bucket="bad", subcategory="1b",
                evidence_quote="q", step_ids=[3], root_cause="rc",
                headroom_signal="", trajectory_link="/tasks/task/probe/t1",
                model="model-a",
            )
        ]

    async def fake_run_eval(inputs, config, **kwargs):
        return _Output()

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.models_by_task == {"task": ["model-a", "model-b"]}


@pytest.mark.asyncio
async def test_store_writes_empty_list_not_null_when_no_findings(monkeypatch):
    """NULL means 'analyzed before findings were persisted'. [] means 'no
    failures found'. Collapsing the two makes a clean run look like a legacy row."""
    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db.models import JobStatus

    analyzer = _install_owned_analyzer(monkeypatch, rh, JobStatus)

    async def fake_gather(session, analyzer_id, org_id):
        return []

    monkeypatch.setattr(rh, "_gather_trial_rows", fake_gather)

    async def fake_build_inputs(rows):
        return object()

    monkeypatch.setattr(rh, "build_analyzer_inputs", fake_build_inputs)

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        by_model = None
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}
        reduce_prompt = None
        proposals = []
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = []

    async def fake_run_eval(inputs, config, **kwargs):
        return _Output()

    monkeypatch.setattr(rh, "run_analyzer_eval", fake_run_eval)

    await rh.run_analyzer_generation_job("r1", worker_job_id="job-1")

    assert analyzer.findings == []


# --- _store's proposal-persistence loop -------------------------------------
#
# Every _Output fake above sets proposals = [] and taxonomy_version = None
# (also the column default), so the `for p in output.proposals` body and the
# taxonomy_version/snapshot writes are never exercised anywhere in this file.
# _store is a closure inside run_analyzer_generation_job (not separately
# importable), and it does a real `session.execute(pg_insert(...)
# .on_conflict_do_nothing(...))` -- a fake session with no `execute` can't
# stand in for it, so these tests drive the real DB via `get_session`,
# uncommitted rows commit for real, and are cleaned up in `finally`.


async def _make_real_analyzer(analyzer_id: str) -> None:
    from oddish.db import get_session
    from oddish.db.models import AnalyzerModel, JobStatus as _JobStatus

    async with get_session() as session:
        session.add(AnalyzerModel(id=analyzer_id, name="t", status=_JobStatus.PENDING))


async def _cleanup_real_analyzer(analyzer_id: str) -> None:
    from sqlalchemy import delete

    from oddish.db import get_session
    from oddish.db.models import AnalyzerModel, CapabilityProposalModel

    async with get_session() as session:
        await session.execute(
            delete(CapabilityProposalModel).where(
                CapabilityProposalModel.analyzer_id == analyzer_id
            )
        )
        analyzer = await session.get(AnalyzerModel, analyzer_id)
        if analyzer is not None:
            await session.delete(analyzer)


@pytest.mark.asyncio
async def test_store_persists_a_capability_proposal_row_per_output_proposal():
    import uuid

    from sqlalchemy import select

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db import get_session
    from oddish.db.models import CapabilityProposalModel
    from oddish.evals.analyzer.schemas import CapabilityProposal

    analyzer_id = f"an-{uuid.uuid4().hex[:8]}"
    slug = f"prop-{uuid.uuid4().hex[:6]}"
    await _make_real_analyzer(analyzer_id)

    prop = CapabilityProposal(
        slug=slug, name="Prop", description="d", example="e",
        categories=["verification"], trial_ids=["t1"], trajectory_link="link",
    )

    class _Output:
        sections = {"bad": "", "good": "", "capabilities": "", "headroom": ""}
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}
        reduce_prompt = None
        by_model = None
        proposals = [prop]
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = []

    async def fake_eval_rows(rows, config, aid):
        return _Output()

    try:
        await rh.run_analyzer_generation_job(analyzer_id, eval_rows_fn=fake_eval_rows)

        async with get_session() as session:
            rows = (
                await session.execute(
                    select(CapabilityProposalModel).where(
                        CapabilityProposalModel.analyzer_id == analyzer_id
                    )
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].slug_suggestion == slug
        assert rows[0].name == "Prop"
        assert rows[0].category_slugs == ["verification"]
        assert rows[0].trial_ids == ["t1"]
        assert rows[0].status == "PENDING"
    finally:
        await _cleanup_real_analyzer(analyzer_id)


@pytest.mark.asyncio
async def test_store_is_idempotent_on_a_retried_proposal_insert():
    """The shield/retry comment on _store's insert claims on_conflict_do_nothing
    keeps a re-entered _store from duplicating a proposal row. Nothing checked
    that until now -- simulate the retry the comment describes by resetting the
    analyzer back to a re-runnable status and invoking the job again."""
    import uuid

    from sqlalchemy import select

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db import get_session
    from oddish.db.models import AnalyzerModel, CapabilityProposalModel, JobStatus
    from oddish.evals.analyzer.schemas import CapabilityProposal

    analyzer_id = f"an-{uuid.uuid4().hex[:8]}"
    slug = f"prop-{uuid.uuid4().hex[:6]}"
    await _make_real_analyzer(analyzer_id)

    prop = CapabilityProposal(
        slug=slug, name="Prop", description="d",
        categories=["verification"], trial_ids=["t1"], trajectory_link="link",
    )

    class _Output:
        sections = {"bad": "", "good": "", "capabilities": "", "headroom": ""}
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}
        reduce_prompt = None
        by_model = None
        proposals = [prop]
        taxonomy_version = None
        taxonomy_snapshot = None
        findings = []

    async def fake_eval_rows(rows, config, aid):
        return _Output()

    try:
        await rh.run_analyzer_generation_job(analyzer_id, eval_rows_fn=fake_eval_rows)

        # Simulate a retry: reset the terminal status as the real dispatcher
        # does before re-entering run_analyzer_generation_job (see
        # test_handler_resets_terminal_status_before_retry).
        async with get_session() as session:
            analyzer = await session.get(
                AnalyzerModel, analyzer_id, with_for_update=True
            )
            analyzer.status = JobStatus.PENDING
            analyzer.finished_at = None

        await rh.run_analyzer_generation_job(analyzer_id, eval_rows_fn=fake_eval_rows)

        async with get_session() as session:
            rows = (
                await session.execute(
                    select(CapabilityProposalModel).where(
                        CapabilityProposalModel.analyzer_id == analyzer_id
                    )
                )
            ).scalars().all()
        assert len(rows) == 1  # on_conflict_do_nothing held; no duplicate row
    finally:
        await _cleanup_real_analyzer(analyzer_id)


@pytest.mark.asyncio
async def test_store_persists_taxonomy_version_and_snapshot_distinct_from_default():
    """taxonomy_version/snapshot default to None on the column -- every fake
    _Output elsewhere in this file sets them to None too, so nothing has ever
    told 'assigned' apart from 'never assigned'."""
    import uuid

    import oddish.workers.queue.analyzer_handler as rh
    from oddish.db import get_session
    from oddish.db.models import AnalyzerModel

    analyzer_id = f"an-{uuid.uuid4().hex[:8]}"
    await _make_real_analyzer(analyzer_id)

    version = uuid.uuid4().hex[:12]
    snapshot = {
        "categories": [{"slug": "verification", "name": "V", "description": "d",
                        "sort_order": 0}],
        "capabilities": [],
    }

    class _Output:
        sections = {"bad": "", "good": "", "capabilities": "", "headroom": ""}
        counts = {"trials": 0, "bad": 0, "good": 0}
        breakdown = {}
        reduce_prompt = None
        by_model = None
        proposals = []
        taxonomy_version = version
        taxonomy_snapshot = snapshot
        findings = []

    async def fake_eval_rows(rows, config, aid):
        return _Output()

    try:
        await rh.run_analyzer_generation_job(analyzer_id, eval_rows_fn=fake_eval_rows)

        async with get_session() as session:
            analyzer = await session.get(AnalyzerModel, analyzer_id)
            assert analyzer.taxonomy_version == version
            assert analyzer.taxonomy_version is not None  # distinct from default
            assert analyzer.taxonomy_snapshot == snapshot
    finally:
        await _cleanup_real_analyzer(analyzer_id)
