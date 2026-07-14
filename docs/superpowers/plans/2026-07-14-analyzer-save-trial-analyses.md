# Analyzer `--save-trials` S3 Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `--save-trials` flag to `oddish analyzer create` that makes the analyzer worker upload every per-trial analysis (findings + subanalyses) to S3 as one JSON object per job.

**Architecture:** The CLI only POSTs to the backend; the analysis runs in a server-side worker job. So the flag is persisted on the `analyzers` DB row and read by the worker (`run_analyzer_generation_job`), which — after the eval — merges `output.findings` with `inputs.subanalyses` into one payload and uploads it to the shared S3 bucket via the existing `StorageClient`. The pure eval core is untouched.

**Tech Stack:** Python 3.13, Typer (CLI), Pydantic (schemas), SQLAlchemy + Alembic (Postgres), aioboto3 (`StorageClient`), pytest.

## Global Constraints

- Never commit to `main`. Work is on branch `feat/analyzer-save-trial-analyses` (already checked out).
- Migrations are idempotent + autocommit, `SET lock_timeout = '8s'`, mirror `analyzers_001`.
- Run tests from the `oddish/` package dir: `cd oddish && uv run pytest ...`.
- The flag defaults to `False` at every layer (CLI option, `AnalyzerCreate`, DB column) — existing callers/rows unaffected.
- S3 upload is **best-effort**: a failure logs a warning and the analyzer still finishes `SUCCESS`. It must never flip the job to `FAILED`.
- S3 key is deterministic: `analyzers/{analyzer_id}/trial_analyses.json`; bucket is `settings.s3_bucket`.
- Commit after each task.

---

## File Structure

- Create `oddish/src/oddish/core/analyzer_trial_export.py` — pure merge/serialize helper.
- Create `oddish/alembic/versions/analyzers_003_add_save_trial_analyses.py` — migration.
- Modify `oddish/src/oddish/db/models.py` — `AnalyzerModel.save_trial_analyses` column.
- Modify `oddish/src/oddish/schemas.py` — `AnalyzerCreate.save_trial_analyses`.
- Modify `oddish/src/oddish/cli/analyzer.py` — `--save-trials` option + POST body.
- Modify `oddish/src/oddish/core/analyzers.py` — persist the flag in `create_analyzer_core`.
- Modify `oddish/src/oddish/workers/queue/analyzer_handler.py` — read flag, upload after eval.
- Tests: `tests/db/test_analyzer_model.py`, `tests/db/test_analyzers_migration.py`, `tests/test_cli_analyzer_create.py`, `tests/core/test_analyzers_crud.py`, new `tests/core/test_analyzer_trial_export.py`, `tests/workers/test_analyzer_handler.py`.

---

## Task 1: DB column + migration

**Files:**
- Modify: `oddish/src/oddish/db/models.py:559` (after the `breakdown` column in `AnalyzerModel`)
- Create: `oddish/alembic/versions/analyzers_003_add_save_trial_analyses.py`
- Test: `oddish/tests/db/test_analyzer_model.py`, `oddish/tests/db/test_analyzers_migration.py`

**Interfaces:**
- Produces: `AnalyzerModel.save_trial_analyses: bool` (non-null, default `False`); migration `revision = "analyzers_003"`, `down_revision = "analyzers_002"`.

- [ ] **Step 1: Write the failing model test**

Add to `oddish/tests/db/test_analyzer_model.py`:

```python
def test_analyzer_has_save_trial_analyses_column():
    cols = AnalyzerModel.__table__.columns
    assert "save_trial_analyses" in cols.keys()
    col = cols["save_trial_analyses"]
    assert col.nullable is False
    assert col.default is not None
    assert col.default.arg is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_analyzer_model.py::test_analyzer_has_save_trial_analyses_column -v`
Expected: FAIL with `KeyError: 'save_trial_analyses'` / assertion on missing column.

- [ ] **Step 3: Add the column to the model**

In `oddish/src/oddish/db/models.py`, inside `AnalyzerModel`, immediately after the `breakdown` column (line 559), add (mirrors the existing pattern at `models.py:485`; `Boolean` and `text` are already imported):

```python
    # Opt-in (set at create time): when true, the worker uploads the per-trial
    # findings+subanalyses to S3 (analyzers/{id}/trial_analyses.json).
    save_trial_analyses: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
```

- [ ] **Step 4: Run the model test to verify it passes**

Run: `cd oddish && uv run pytest tests/db/test_analyzer_model.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing migration test**

Add to `oddish/tests/db/test_analyzers_migration.py`:

```python
MIG_003 = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "analyzers_003_add_save_trial_analyses.py"
)


def test_migration_003_adds_save_trial_analyses_column():
    text = MIG_003.read_text()
    assert 'revision = "analyzers_003"' in text
    assert 'down_revision = "analyzers_002"' in text
    assert "ADD COLUMN IF NOT EXISTS save_trial_analyses BOOLEAN NOT NULL DEFAULT false" in text
    assert "DROP COLUMN IF EXISTS save_trial_analyses" in text
    assert "SET lock_timeout = '8s'" in text
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_analyzers_migration.py::test_migration_003_adds_save_trial_analyses_column -v`
Expected: FAIL (`FileNotFoundError` — migration not created yet).

- [ ] **Step 7: Create the migration**

Create `oddish/alembic/versions/analyzers_003_add_save_trial_analyses.py`:

```python
"""add analyzers.save_trial_analyses flag

Additive column: NOT NULL with server default 'false' so the backfill is
instant and existing rows read false. Idempotent + autocommit, mirroring
analyzers_001.
"""

from alembic import op

revision = "analyzers_003"
down_revision = "analyzers_002"
branch_labels = None
depends_on = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def upgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit(
        "ALTER TABLE analyzers "
        "ADD COLUMN IF NOT EXISTS save_trial_analyses BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("ALTER TABLE analyzers DROP COLUMN IF EXISTS save_trial_analyses")
```

- [ ] **Step 8: Verify single migration head**

Run: `cd oddish && uv run alembic heads`
Expected: exactly one head, `analyzers_003 (head)`. If more than one head appears, the chosen `down_revision` was stale — re-point it at the real prior head and re-run.

- [ ] **Step 9: Run the migration + model tests to verify they pass**

Run: `cd oddish && uv run pytest tests/db/test_analyzers_migration.py tests/db/test_analyzer_model.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/alembic/versions/analyzers_003_add_save_trial_analyses.py oddish/tests/db/test_analyzer_model.py oddish/tests/db/test_analyzers_migration.py
git commit -m "feat(analyzer): add save_trial_analyses column + migration"
```

---

## Task 2: Schema field + CLI flag

**Files:**
- Modify: `oddish/src/oddish/schemas.py:1779-1781` (`AnalyzerCreate`)
- Modify: `oddish/src/oddish/cli/analyzer.py:19-69` (`create` command)
- Test: `oddish/tests/test_cli_analyzer_create.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `AnalyzerCreate.save_trial_analyses: bool = False`; CLI `--save-trials` puts `"save_trial_analyses": <bool>` in the POST body.

- [ ] **Step 1: Write the failing CLI test**

Add to `oddish/tests/test_cli_analyzer_create.py`:

```python
def test_analyzer_create_defaults_save_trials_false(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["analyzer", "create", "-e", "e1", "--name", "Q3"]
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.last_request["json"]["save_trial_analyses"] is False


def test_analyzer_create_save_trials_flag_sets_payload(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["analyzer", "create", "-e", "e1", "--name", "Q3", "--save-trials"]
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.last_request["json"]["save_trial_analyses"] is True
```

Also update the existing `test_analyzer_create_posts_expected_payload` assertion (line 46) to include the new key:

```python
    assert _FakeClient.last_request["json"] == {
        "name": "Q3", "experiment_ids": ["e1", "e2"], "save_trial_analyses": False,
    }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd oddish && uv run pytest tests/test_cli_analyzer_create.py -v`
Expected: FAIL (`save_trial_analyses` not in payload).

- [ ] **Step 3: Add the schema field**

In `oddish/src/oddish/schemas.py`, `AnalyzerCreate` (lines 1779-1781) becomes:

```python
class AnalyzerCreate(BaseModel):
    name: str = Field(min_length=1)
    experiment_ids: list[str] = Field(min_length=1)
    save_trial_analyses: bool = False
```

- [ ] **Step 4: Add the CLI option + payload key**

In `oddish/src/oddish/cli/analyzer.py`, add a parameter to `create` (after `json_output`, before `api_url`):

```python
    save_trials: Annotated[
        bool,
        typer.Option(
            "--save-trials",
            help="Also save each trial-level analysis to S3 (one JSON per job).",
        ),
    ] = False,
```

And update the POST body (currently line 53-55):

```python
        resp = client.post(
            f"{api_url}/analyzers",
            json={
                "name": name,
                "experiment_ids": ids,
                "save_trial_analyses": save_trials,
            },
        )
```

- [ ] **Step 5: Run the CLI tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_cli_analyzer_create.py tests/test_analyzer_schemas.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/src/oddish/cli/analyzer.py oddish/tests/test_cli_analyzer_create.py
git commit -m "feat(analyzer): --save-trials CLI flag + AnalyzerCreate field"
```

---

## Task 3: Persist the flag in `create_analyzer_core`

**Files:**
- Modify: `oddish/src/oddish/core/analyzers.py:46-51` (`create_analyzer_core`)
- Test: `oddish/tests/core/test_analyzers_crud.py`

**Interfaces:**
- Consumes: `AnalyzerCreate.save_trial_analyses` (Task 2), `AnalyzerModel.save_trial_analyses` (Task 1).
- Produces: `create_analyzer_core` writes `data.save_trial_analyses` onto the new row.

- [ ] **Step 1: Write the failing test**

Add to `oddish/tests/core/test_analyzers_crud.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd oddish && uv run pytest tests/core/test_analyzers_crud.py::test_create_analyzer_persists_save_trial_analyses -v`
Expected: FAIL (`save_trial_analyses` not set on the model → attribute is the column default at flush, but the `True` case fails / or AttributeError before flush).

- [ ] **Step 3: Set the field in `create_analyzer_core`**

In `oddish/src/oddish/core/analyzers.py`, the `AnalyzerModel(...)` construction (lines 46-51) becomes:

```python
    analyzer = AnalyzerModel(
        name=data.name,
        org_id=org_id,
        owner_user_id=user_id,
        status=JobStatus.PENDING,
        save_trial_analyses=data.save_trial_analyses,
    )
```

- [ ] **Step 4: Run the CRUD tests to verify they pass**

Run: `cd oddish && uv run pytest tests/core/test_analyzers_crud.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/analyzers.py oddish/tests/core/test_analyzers_crud.py
git commit -m "feat(analyzer): persist save_trial_analyses on create"
```

---

## Task 4: Pure trial-analyses export helper

**Files:**
- Create: `oddish/src/oddish/core/analyzer_trial_export.py`
- Test: `oddish/tests/core/test_analyzer_trial_export.py`

**Interfaces:**
- Consumes: `Finding` (`oddish.evals.analyzer.schemas`), `SubAnalysis` (`oddish.evals.primitives`).
- Produces: `build_trial_analyses_payload(*, analyzer_id: str, findings: list[Finding], subanalyses: list[SubAnalysis], counts: dict[str, int]) -> dict`. Output shape: `{"analyzer_id": str, "counts": dict, "trials": [{"trial_id": str, "finding": dict|None, "subanalysis": dict|None}, ...]}`, `trials` sorted by `trial_id`, and each `finding` dict has its redundant `trial_id` key removed.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/core/test_analyzer_trial_export.py`:

```python
from oddish.core.analyzer_trial_export import build_trial_analyses_payload
from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis


def _finding(trial_id: str) -> Finding:
    return Finding(
        trial_id=trial_id, bucket="bad", subcategory="3a",
        evidence_quote="q", step_indices=[1, 2], root_cause="rc",
        headroom_signal="hs", trajectory_link="link",
    )


def _sub(trial_id: str) -> SubAnalysis:
    return SubAnalysis(
        trial_id=trial_id, trajectory_link="link", classification="c",
        subtype="st", evidence="ev", root_cause="rc", recommendation="rec",
    )


def test_payload_merges_finding_and_subanalysis_by_trial():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t1")],
        subanalyses=[_sub("t1")],
        counts={"trials": 1, "bad": 1, "good": 0},
    )
    assert payload["analyzer_id"] == "az1"
    assert payload["counts"] == {"trials": 1, "bad": 1, "good": 0}
    assert len(payload["trials"]) == 1
    record = payload["trials"][0]
    assert record["trial_id"] == "t1"
    assert record["finding"]["subcategory"] == "3a"
    assert "trial_id" not in record["finding"]  # redundant with record key
    assert record["subanalysis"]["classification"] == "c"


def test_payload_includes_trials_with_only_one_side():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t_finding_only")],
        subanalyses=[_sub("t_sub_only")],
        counts={},
    )
    by_id = {r["trial_id"]: r for r in payload["trials"]}
    assert by_id["t_finding_only"]["subanalysis"] is None
    assert by_id["t_sub_only"]["finding"] is None


def test_payload_trials_sorted_and_empty_ok():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t2"), _finding("t1")],
        subanalyses=[],
        counts={},
    )
    assert [r["trial_id"] for r in payload["trials"]] == ["t1", "t2"]

    empty = build_trial_analyses_payload(
        analyzer_id="az1", findings=[], subanalyses=[], counts={},
    )
    assert empty["trials"] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd oddish && uv run pytest tests/core/test_analyzer_trial_export.py -v`
Expected: FAIL (`ModuleNotFoundError: oddish.core.analyzer_trial_export`).

- [ ] **Step 3: Implement the helper**

Create `oddish/src/oddish/core/analyzer_trial_export.py`:

```python
"""Pure serialization of per-trial analyzer outputs for the S3 trial-dump.

Kept DB/S3-free so the merge logic is unit-testable. Consumed by the analyzer
worker handler when ``AnalyzerModel.save_trial_analyses`` is set.
"""

from __future__ import annotations

from dataclasses import asdict

from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis


def build_trial_analyses_payload(
    *,
    analyzer_id: str,
    findings: list[Finding],
    subanalyses: list[SubAnalysis],
    counts: dict[str, int],
) -> dict:
    """Merge findings and subanalyses into one per-job JSON-able payload.

    Union of trial ids across both inputs; a trial present in only one appears
    with the other side ``None``. ``trials`` is sorted by ``trial_id`` for
    deterministic output.
    """
    findings_by_trial = {f.trial_id: f for f in findings}
    subs_by_trial = {s.trial_id: s for s in subanalyses}
    trial_ids = sorted(set(findings_by_trial) | set(subs_by_trial))

    trials = []
    for tid in trial_ids:
        finding = findings_by_trial.get(tid)
        finding_dict = None
        if finding is not None:
            finding_dict = asdict(finding)
            finding_dict.pop("trial_id", None)  # redundant with the record key
        sub = subs_by_trial.get(tid)
        trials.append(
            {
                "trial_id": tid,
                "finding": finding_dict,
                "subanalysis": asdict(sub) if sub is not None else None,
            }
        )

    return {"analyzer_id": analyzer_id, "counts": dict(counts), "trials": trials}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd oddish && uv run pytest tests/core/test_analyzer_trial_export.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/analyzer_trial_export.py oddish/tests/core/test_analyzer_trial_export.py
git commit -m "feat(analyzer): pure per-trial analyses export helper"
```

---

## Task 5: Worker uploads the trial analyses to S3

**Files:**
- Modify: `oddish/src/oddish/workers/queue/analyzer_handler.py` (imports; capture flag at step 1; upload after `run_analyzer_eval`; new helpers)
- Test: `oddish/tests/workers/test_analyzer_handler.py`

**Interfaces:**
- Consumes: `AnalyzerModel.save_trial_analyses` (Task 1), `build_trial_analyses_payload` (Task 4), `get_storage_client` (`oddish.db.storage`), `output.findings`/`output.counts` (`AnalyzerEvalOutput`), `inputs.subanalyses` (`AnalyzerEvalInputs`).
- Produces: `_trial_analyses_s3_key(analyzer_id) -> str` returning `f"analyzers/{analyzer_id}/trial_analyses.json"`; `_maybe_save_trial_analyses(...)` best-effort upload.

- [ ] **Step 1: Write the failing tests**

Add to `oddish/tests/workers/test_analyzer_handler.py` (helpers `_install_owned_analyzer`, imports already exist in that file):

```python
def _fake_output_with_findings():
    from oddish.evals.analyzer.schemas import Finding

    class _Output:
        sections = {"bad": "b", "good": "g", "capabilities": "c", "headroom": "h"}
        counts = {"trials": 1, "bad": 1, "good": 0}
        breakdown = {}
        findings = [
            Finding(
                trial_id="t1", bucket="bad", subcategory="3a", evidence_quote="q",
                step_indices=[1], root_cause="rc", headroom_signal="hs",
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

    async def fake_run_eval(inp, config):
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
```

Also update `_install_owned_analyzer`'s `_FakeAnalyzer.__init__` (and the two other inline `_FakeAnalyzer` classes in this file) to set `self.save_trial_analyses = False`, so the existing tests continue to have the attribute present when the handler reads it:

```python
            self.save_trial_analyses = False
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd oddish && uv run pytest tests/workers/test_analyzer_handler.py -k "save_trial or no_upload or upload_failure" -v`
Expected: FAIL (`get_storage_client` not an attribute of the handler module / upload never called).

- [ ] **Step 3: Add imports + helpers to the handler**

In `oddish/src/oddish/workers/queue/analyzer_handler.py`:

Add to the top-of-file imports (after `import os`):

```python
import json
import logging
```

Add these imports alongside the existing `from oddish...` imports:

```python
from oddish.core.analyzer_trial_export import build_trial_analyses_payload
from oddish.db.storage import get_storage_client
```

After `ANALYZER_HEARTBEAT_INTERVAL_SECONDS = 30`, add:

```python
logger = logging.getLogger(__name__)


def _trial_analyses_s3_key(analyzer_id: str) -> str:
    return f"analyzers/{analyzer_id}/trial_analyses.json"


async def _maybe_save_trial_analyses(
    *,
    analyzer_id: str,
    findings,
    subanalyses,
    counts,
) -> None:
    """Best-effort upload of the per-trial analyses to S3.

    A failure here logs a warning but must NOT fail the analyzer job — the
    aggregate sections are the primary product. Called only when the analyzer's
    ``save_trial_analyses`` flag is set.
    """
    key = _trial_analyses_s3_key(analyzer_id)
    try:
        payload = build_trial_analyses_payload(
            analyzer_id=analyzer_id,
            findings=findings,
            subanalyses=subanalyses,
            counts=counts,
        )
        data = json.dumps(payload, indent=2).encode("utf-8")
        await get_storage_client().upload_bytes(
            data, key, content_type="application/json"
        )
        logger.info(
            "Saved %d trial analyses for analyzer %s to s3://%s/%s",
            len(payload["trials"]), analyzer_id, settings.s3_bucket, key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to save trial analyses for analyzer %s to s3://%s/%s: %s",
            analyzer_id, settings.s3_bucket, key, exc,
        )
```

- [ ] **Step 4: Capture the flag at load time**

In `run_analyzer_generation_job`, step 1 block, right after `org_id = analyzer.org_id` (line 165), add:

```python
        save_trial_analyses = analyzer.save_trial_analyses
```

- [ ] **Step 5: Call the upload after the eval**

In the try block, immediately after `output = await run_analyzer_eval(inputs, _build_analyzer_eval_config())` (line 222), add:

```python
        if save_trial_analyses and output is not None:
            await _maybe_save_trial_analyses(
                analyzer_id=analyzer_id,
                findings=output.findings,
                subanalyses=inputs.subanalyses,
                counts=output.counts,
            )
```

- [ ] **Step 6: Run the handler tests to verify they pass**

Run: `cd oddish && uv run pytest tests/workers/test_analyzer_handler.py -v`
Expected: PASS (all, including the pre-existing reap/failure tests).

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/workers/queue/analyzer_handler.py oddish/tests/workers/test_analyzer_handler.py
git commit -m "feat(analyzer): upload per-trial analyses to S3 when flag set"
```

---

## Task 6: Full verification sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the whole analyzer test surface**

Run:
```bash
cd oddish && uv run pytest \
  tests/db/test_analyzer_model.py \
  tests/db/test_analyzers_migration.py \
  tests/test_cli_analyzer_create.py \
  tests/test_analyzer_schemas.py \
  tests/core/test_analyzers_crud.py \
  tests/core/test_analyzer_trial_export.py \
  tests/workers/test_analyzer_handler.py -v
```
Expected: all PASS.

- [ ] **Step 2: Confirm one migration head**

Run: `cd oddish && uv run alembic heads`
Expected: single head `analyzers_003 (head)`.

- [ ] **Step 3: Sanity-check the CLI help shows the flag**

Run: `cd oddish && uv run oddish analyzer create --help`
Expected: `--save-trials` listed in the options with its help text.

---

## Self-Review Notes

- **Spec coverage:** flag plumbing (Tasks 2/3), DB column + migration (Task 1), merged findings+subanalyses payload one-file-per-job (Task 4), worker S3 upload with best-effort semantics + empty-case valid object (Task 5), deterministic key not stored back (Task 5 helper), tests at every layer (all tasks). All spec sections mapped.
- **Type consistency:** `build_trial_analyses_payload` signature identical in Task 4 (definition) and Task 5 (call site). `_trial_analyses_s3_key` returns the exact key asserted in the Task 5 test. `AnalyzerModel.save_trial_analyses` name identical across Tasks 1/3/5.
- **Migration head:** Task 1 Step 8 verifies the head rather than assuming `analyzers_002` is still current.
