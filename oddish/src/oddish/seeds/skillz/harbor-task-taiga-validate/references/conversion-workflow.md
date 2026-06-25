# Conversion Workflow

## Purpose

This repo is the working area for Harbor-format tasks that are being converted to Taiga and reviewed in PRs.

## Repository Rules

- Keep tasks under `tasks/<task-name>/`.
- Keep exactly one task in each PR.
- Keep task rewrites, validation notes, and review tracking separate when possible.
- Treat the repo as the conversion and review record, not a scratch space for unvalidated task bundles.
- Use a real branch for each task. Create a PR when you need repo review or when direct Taiga execution is blocked and CI must supply the conversion and QA loop.

## Direct Local Taiga Path

When a valid `TAIGA_TOKEN` or `~/.config/taiga/auth.json` is available, prefer the direct local path:

1. Validate the task locally.
2. Convert the Harbor task with local Harbor-to-Taiga tooling.
3. Build and push the image.
4. Upsert the problem in the mapped Taiga environment.
5. Submit runs and inspect env linter plus QA jobs through the Taiga API.
6. Fix the task and rerun until Taiga reports zero critical or error issues.

### Local Auth And Publish Preflight

Taiga auth and image publish auth are separate.

From this skill directory, run:

```bash
python scripts/direct_local_preflight.py
```

That verifies both:

- Taiga API auth from `TAIGA_TOKEN` or `~/.config/taiga/auth.json`
- local Artifact Registry auth for `us-east1-docker.pkg.dev`

If you need to force a new interactive Taiga login:

```bash
python scripts/direct_local_preflight.py --force-login
```

If Taiga auth fails, Docker is unavailable, or the GCP registry preflight fails, the direct local path is blocked. Switch to the platform PR/CI path instead of guessing.

Typical local tools:

- `python scripts/validate-task-structure.py tasks/<task-name>`
- `python -m harbor_to_taiga.convert ...`
- `harbor-to-taiga/scripts/publish_and_run.sh` or `harbor-to-taiga/scripts/taiga_api.py`
- `scripts/smithy_qa.py` (when working in the smithy conversion repo) or direct `/api/qa-jobs` calls

## Platform PR/CI Path

If direct Taiga execution is blocked, CI can supply the same validation loop:

On each PR, CI will:

1. Convert the Harbor task to Taiga format.
2. Build and push the container image.
3. Create the Taiga problem.
4. Run env linter v2, fetch auto QA, and submit agent trials in parallel.
5. Run the difficulty check after agent trials complete.
6. Post findings back to the PR.

## How To Use The Flow

- Validate locally first so the first PR run is meaningful.
- If direct Taiga auth and local publish access are available, use the direct local path first and use the API results as the source of truth.
- If direct Taiga execution is blocked, push the single-task branch and open or update the PR to trigger the workflow.
- Read the Taiga API results directly when running locally. In the fallback path, read the PR comments and workflow results for env linter, auto QA, and difficulty feedback.
- If Taiga reports critical or error issues, fix the task and rerun the same path. Escalate to the platform PR/CI path only when the direct path is blocked.
- Do not treat the task as done until Taiga feedback is clean or a blocker is clearly documented.

## Local Checks

- Install dependencies with `uv pip install -e .`.
- Validate task structure with `python scripts/validate-task-structure.py tasks/<task-name>`.

## Tracking

- Use validation and review notes to record what changed and what still needs attention.
- Add rewritten task files only after the validation bar has been met or the blocker is clearly documented.
