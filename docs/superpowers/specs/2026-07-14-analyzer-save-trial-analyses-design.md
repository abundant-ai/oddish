# Design: `--save-trials` flag — persist trial-level analyses to S3

**Date:** 2026-07-14
**Status:** Approved (pending spec review)

## Goal

Give `oddish analyzer create` an opt-in flag that, when set, saves every
trial-level analysis the analyzer produces to S3 as a single per-job JSON object.
Today the analyzer only persists its four aggregate narrative sections (plus
counts/breakdown) to the `analyzers` Postgres row; the per-trial `Finding`
objects from the "map" step are transient and discarded after the "reduce" step.
This feature lets a user keep them.

## Key architectural fact

The CLI does **not** run the analysis. `oddish analyzer create` POSTs to the
backend, which enqueues an `ANALYZER` worker job
(`run_analyzer_generation_job`) that runs server-side on Modal. The per-trial
analyses only exist inside that worker. Therefore the flag must travel from the
CLI to the worker and the S3 write happens in the worker — not the terminal:

```
oddish analyzer create ... --save-trials
  → POST /analyzers {..., "save_trial_analyses": true}
  → create_analyzer_core sets AnalyzerModel.save_trial_analyses = true   (persisted)
  → worker run_analyzer_generation_job reads the flag; after run_analyzer_eval
    it uploads one JSON blob to S3
```

Both data sources are already in the worker's scope at upload time:
`output.findings` (return of `run_analyzer_eval`) and `inputs.subanalyses`
(the `AnalyzerEvalInputs` built just before the eval). No change to the pure
eval core (`evals/analyzer/core.py`) is required.

## What lands in S3

One object per job, in the existing bucket and `StorageClient`.

- **Bucket:** `settings.s3_bucket` (`ODDISH_S3_BUCKET`, the shared bucket)
- **Key:** `analyzers/{analyzer_id}/trial_analyses.json`
- **Content type:** `application/json`
- **Body:** findings and subanalyses merged per trial:

```json
{
  "analyzer_id": "az_…",
  "counts": {"trials": 42, "bad": 12, "good": 8},
  "trials": [
    {
      "trial_id": "tr_…",
      "finding": {
        "bucket": "bad", "subcategory": "3a", "evidence_quote": "…",
        "step_indices": [4, 5], "root_cause": "…", "headroom_signal": "…",
        "trajectory_link": "…"
      },
      "subanalysis": {
        "trial_id": "tr_…", "trajectory_link": "…", "classification": "…",
        "subtype": "…", "evidence": "…", "root_cause": "…",
        "recommendation": "…", "trajectory_summary": null
      }
    }
  ]
}
```

Merge rules:
- Union of trial ids across `findings` and `subanalyses`.
- A trial with a subanalysis but no failing-bucket finding appears with
  `"finding": null`. A trial with a finding but no subanalysis appears with
  `"subanalysis": null`.
- `Finding` and `SubAnalysis` are both dataclasses → serialized via
  `dataclasses.asdict`. The `finding` object drops the redundant top-level
  `trial_id` (it's already the record key).
- `trials` is sorted by `trial_id` for deterministic output (testability).

## Components / files touched

1. **`oddish/src/oddish/cli/analyzer.py`** — add a `--save-trials` boolean
   option (default `False`) to `create`; include `save_trial_analyses` in the
   POST body.
2. **`oddish/src/oddish/schemas.py`** — `AnalyzerCreate` gains
   `save_trial_analyses: bool = False`.
3. **`oddish/src/oddish/db/models.py`** — `AnalyzerModel` gains a
   `save_trial_analyses` `Boolean` column, `nullable=False`,
   `server_default` false / default `False`.
4. **`oddish/alembic/versions/analyzers_003_add_save_trial_analyses.py`** — new
   migration adding the column (server default `false`, non-null).
   `down_revision` = the current migration head — verify with
   `uv run alembic heads` at implementation time; do not assume it is
   `analyzers_002_...` (other migrations may have landed after it).
5. **`oddish/src/oddish/core/analyzers.py`** — `create_analyzer_core` sets
   `save_trial_analyses=data.save_trial_analyses` on the new `AnalyzerModel`.
6. **`oddish/src/oddish/core/analyzer_trial_export.py`** (new) — pure function
   `build_trial_analyses_payload(*, analyzer_id, findings, subanalyses, counts) -> dict`.
   All merge/serialize logic lives here so it is unit-testable with no DB/S3.
7. **`oddish/src/oddish/workers/queue/analyzer_handler.py`** — capture the flag
   at load (alongside `org_id`); after `run_analyzer_eval` returns, if the flag
   is set, build the payload and upload it via `get_storage_client()`.

## Worker integration detail

In `run_analyzer_generation_job`:
- Step 1 (load + set RUNNING) already captures `org_id` from the row; also
  capture `save_trial_analyses` into a local.
- The upload goes in the try block, immediately after
  `output = await run_analyzer_eval(...)`, where both `output` and `inputs` are
  in scope. It is guarded by `if save_trial_analyses and output is not None`.
- Serialize `build_trial_analyses_payload(...)` → `json.dumps(...).encode()` →
  `await get_storage_client().upload_bytes(data, key, content_type="application/json")`.
- Log the resulting `s3://{bucket}/{key}` URI on success.

## Behavior decisions

- **Best-effort upload.** The upload is wrapped in its own `try/except`. On
  failure it logs a warning and the analyzer still finishes `SUCCESS` — the
  aggregate sections are the primary product and a trial-dump hiccup must not
  fail the whole job. (It is not folded into the outer except that flips the job
  to `FAILED`.)
- **Deterministic key, not stored back.** The key is fully derivable from
  `analyzer_id`, so no extra DB column records it; the worker logs the URI. A
  surfaced field (response/CLI/UI) can be added later if wanted.
- **Idempotent.** Re-running (retry) overwrites the same key; harmless.

## Error handling

- S3 upload errors: caught, logged, non-fatal (see above).
- Empty analyzer (no findings, no subanalyses): still writes a valid object with
  an empty `trials` array when the flag is set — a clear "ran, nothing to save"
  signal rather than a missing object.
- Flag defaults `False` everywhere (CLI option, schema, DB column), so existing
  callers and rows are unaffected.

## Testing

- **Unit (`analyzer_trial_export`):** merge correctness — finding-only,
  subanalysis-only, both, and empty inputs; deterministic ordering; `trial_id`
  handling.
- **Worker handler:** with the flag set, `get_storage_client().upload_bytes` is
  called once with the expected key and a JSON body matching the payload; with
  the flag unset, it is never called. Use a fake/monkeypatched storage client;
  no real S3. Assert an S3 upload exception does not flip the analyzer to
  `FAILED`.
- **CLI/schema:** `--save-trials` propagates into the POST body;
  `create_analyzer_core` persists the column.
- Run `pytest` from `oddish/`.

## Out of scope

- Reading the saved object back via CLI/API/UI.
- Per-trial object layout (one file per trial) — explicitly chose one-file-per-job.
- Backfilling trial analyses for already-completed analyzers.
