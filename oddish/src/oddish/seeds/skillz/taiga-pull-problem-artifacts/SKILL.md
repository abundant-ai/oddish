---
name: taiga-pull-problem-artifacts
description: Export Taiga problem-version artifacts from taiga.ant.dev. Use when the user points Codex at a Taiga problem-version URL or Taiga problem/problem-version IDs and asks to pull attempts, problem runs, full transcripts, boot steps, QA jobs, problem linter, env linter, difficulty analysis, ClaudeScope/transcript health, reward hacking reports, or related Taiga QA artifacts into local files.
---

# Taiga Pull Problem Artifacts

## Quick Start

Use the bundled script for repeatable exports:

```bash
TAIGA_ARTIFACT_SKILL_DIR=/absolute/path/to/skills/taiga-pull-problem-artifacts
python "$TAIGA_ARTIFACT_SKILL_DIR/scripts/pull_taiga_problem_artifacts.py" \
  "https://taiga.ant.dev/problem-versions/<problem_version_id>?problemId=<problem_id>&environmentId=<environment_id>" \
  --out-dir artifacts
```

The script writes:

- `taiga_<problem_id>_attempts_transcripts_with_qa_<pv8>.json`
- `taiga_<problem_id>_qa_reports_<pv8>.json`

## Workflow

1. Prefer the bundled script over manually retyping endpoint calls. Resolve the script path relative to this skill folder.
2. Accept a full Taiga problem-version URL when available. If the URL is missing query parameters, pass `--environment-id`, `--problem-id`, or `--problem-version-id` explicitly.
3. Use existing auth in this order:
   - `TAIGA_TOKEN`
   - `~/.config/taiga/auth.json`
4. If auth fails or is missing, ask the user to run their Taiga login flow, then retry.
5. After export, parse-check the JSON and report the output paths, counts, QA types, and any fetch errors.

## What The Export Contains

The combined JSON includes:

- Source URL, environment ID, problem ID, target problem-version ID
- Target problem-version metadata
- Problem-level attempts from `/api/environments/{environment_id}/problems/{problem_id}/attempts`
- Problem-level problem runs from `/api/environments/{environment_id}/problems/{problem_id}/problem-runs`
- Per-run details from `/api/problem-runs/{problem_run_id}`
- Full transcript strings from `/api/transcript/{problem_run_id}`
- Boot-step metadata from `/api/transcript/boot-steps/{problem_run_id}`
- QA report data from:
  - `/api/environments/{environment_id}/problem-versions/{problem_version_id}/qa-jobs`
  - `/api/environments/{environment_id}/problem-versions/{problem_version_id}/qa-items`
  - `/api/environments/{environment_id}/problem-versions/{problem_version_id}/qa-item-feedback`
  - `/api/qa-jobs/{qa_job_id}`
  - `/api/qa-jobs/{qa_job_id}/feedback`
  - `/api/qa-jobs/{qa_job_id}/item-feedback`
  - `/api/qa-jobs/{qa_job_id}/followups`

Taiga currently returns attempts and problem runs at problem scope; query parameters may not filter them to the target problem version. Preserve all returned runs by default and use the summary fields to identify target-PV runs. Use `--target-only` only when the user explicitly asks for just the linked problem-version runs.

## Options

Useful script flags:

- `--target-only`: fetch transcripts only for runs whose `problem_version_id` matches the target PV.
- `--include-logs`: also fetch environment logs and sandbox runtime logs for each run.
- `--base-url`: override the Taiga base URL. Defaults to `https://taiga.ant.dev`.
- `--out-dir`: output directory. Defaults to the current directory.
- `--timeout`: request timeout in seconds.

## Reporting

When done, report:

- Combined artifact path
- QA-only artifact path
- Attempt count, problem-run count, transcript count
- Target problem-version run IDs
- QA job count and QA types
- Any recorded per-run or per-QA errors

Do not print auth tokens or copy `id_token` or `refresh_token` values into outputs.
