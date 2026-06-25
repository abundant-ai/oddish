# Taiga Validation Loop

## Goal

Take one Harbor task, convert it, run it in Taiga, and keep iterating until the conversion is clean or the blocker is clearly documented.

## Local Auth And Registry Preflight

The direct local path has two separate auth requirements:

- Taiga API auth
- Artifact Registry push auth

Do not treat them as the same thing.

From this skill directory, run:

```bash
python scripts/direct_local_preflight.py
```

That checks both:

- local Taiga auth from `TAIGA_TOKEN` or `~/.config/taiga/auth.json`
- local GCP/Artifact Registry auth for `us-east1-docker.pkg.dev`

If you need a fresh interactive Taiga login:

```bash
python scripts/direct_local_preflight.py --force-login
```

If the Taiga CLI is not installed, the script will fail explicitly instead of guessing. If any of these checks fail, the direct local path is blocked and the platform PR/CI path is the correct fallback.

## Loop

There is one validation loop. It can execute through either the direct local path or the platform PR/CI path.

1. Inspect the task and the harness.
2. Edit the task into the fixed split shape when needed.
3. Validate locally.
4. Use the direct local path when a valid `TAIGA_TOKEN` or `~/.config/taiga/auth.json`, Docker, Artifact Registry push access, and local conversion tooling are available. Convert the task locally, upsert it to Taiga, submit runs, and inspect env linter and QA jobs through the API.
5. Use the platform PR/CI path when the direct local path is blocked. Push the single-task branch and open or update the PR so CI converts the task and runs it in Taiga.
6. Review Taiga feedback from the API first when running locally, or from PR comments, checks, and artifacts in the fallback path.
7. Fix the task and repeat until Taiga reports zero critical or error issues.

## Done Condition

The task is done only when Taiga feedback reports zero critical or error issues, whether that feedback came from direct API access or the platform PR/CI path.

## Final Step

When the task is clean:

- commit the task
- push the task
- leave a review or validation note if the workflow expects tracking

Do not stop at "looks good locally." Local success is necessary but not sufficient.
