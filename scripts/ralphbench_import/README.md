# RalphBench Oddish Import

Imports Slack Clone and Mastodon Clone trial logs from the Oddish hosted
experiment `cd8c33d8` into `s3://ralphbench-logs/<task_name>/`.

## Files

- `select.py` — picks up to 5 valid trials per agent/model pair (13 pairs),
  using DB queries against the Oddish Postgres + size-aware tie-breaks
  computed from Oddish S3 listings. Writes `selection.json`.
- `upload.py` — streams each selected trial from Oddish S3 directly to
  `ralphbench-logs`, injecting `task_name` / `task_hash` / `task_version_id`
  into the top-level `config.json` and `result.json`. Maintains `state.json`
  so re-runs are idempotent per trial.
- `manifest.py` — emits the per-task `_manifest.json` matching the existing
  RalphBench convention.

## Environment

Reads `ODDISH_DATABASE_URL` and `ODDISH_S3_*` for source access. The
destination bucket is hardcoded to `ralphbench-logs` (us-west-2); the
IAM credentials for that bucket are passed via env/argv at run time.

## Output layout

```
s3://ralphbench-logs/<task_name>/
  _manifest.json
  <trial_id>/
    config.json   (top-level Harbor job config + task_name/task_hash/task_version_id)
    result.json   (top-level Harbor job result + task_name/task_hash/task_version_id)
    job.log, lock.json, modal-output.log
    task-<task_hash>-<random>/    (canonical Harbor attempt)
      config.json
      result.json
      agent/...
      verifier/...
      artifacts/...
      trial.log, exception.txt, ...
```
