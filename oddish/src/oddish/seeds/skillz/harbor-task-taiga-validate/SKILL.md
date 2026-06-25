---
name: harbor-task-taiga-validate
description: Validate Harbor tasks in Taiga after local task checks, using direct local Taiga execution when available and the PR/CI platform path as fallback.
---

# Harbor Task Taiga Validate

## Overview

Use this skill after a Harbor task has a locally runnable harness and needs Taiga validation. The validation loop is one workflow with two execution paths:

- Direct local path: use Taiga auth and local publishing tools to convert, upsert, run, and inspect Taiga feedback directly.
- Platform path: use the repo's PR/CI workflow to convert, publish, run, and report Taiga feedback when local execution is blocked.

Local task success is necessary but not sufficient. Do not declare the task done until Taiga reports zero critical or error issues, or until the blocker preventing Taiga validation is clearly documented.

## Load References Deliberately

- Read [references/taiga-validation-loop.md](references/taiga-validation-loop.md) for the validation loop, done condition, and final reporting bar.
- Read [references/conversion-workflow.md](references/conversion-workflow.md) when choosing between direct local execution and the platform PR/CI path.
- Read [references/task-validation-note.md](references/task-validation-note.md) when recording validation status for review and tracking.

## Working Rules

- Validate the task locally before running Taiga validation.
- Check local auth early.
- For Taiga API access, prefer `TAIGA_TOKEN`; otherwise use `~/.config/taiga/auth.json`.
- Run `python scripts/direct_local_preflight.py` before the direct local path. It verifies Taiga auth and Artifact Registry auth from inside this skill.
- If local Taiga auth is missing or expired, run `python scripts/direct_local_preflight.py --force-login`.
- Prefer the direct local path when valid auth, Docker, image publish access, and local conversion tooling are available.
- Use the platform PR/CI path when direct local execution is blocked by missing or expired Taiga auth, missing Docker or registry publish access, missing local tooling, or a local failure that CI can supply.
- Treat Taiga API results as the primary feedback surface on the direct local path.
- Treat PR comments, checks, and workflow artifacts as the primary feedback surface on the platform path.
- Fix the task and repeat the same path until Taiga reports zero critical or error issues.
- Escalate from direct local execution to the platform path only when the direct path is blocked, not merely because the task has findings.
- Report blockers explicitly when neither path can complete validation.

## Final Output

Include:

- validation path used: direct local or platform PR/CI
- Taiga problem, run, or QA job IDs when available
- critical or error findings resolved
- remaining blockers, if any
- commands or workflow evidence used for validation
