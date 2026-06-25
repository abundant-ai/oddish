# Taiga Validation Loop

## Goal

Take one Harbor task, convert it, run it in Taiga, and keep iterating until the conversion is clean or the blocker is clearly documented.

## Loop

1. Inspect the task and the harness.
2. Edit the task into the fixed split shape when needed.
3. Validate locally.
4. If a valid `TAIGA_TOKEN` or `~/.config/taiga/auth.json` is available, convert the task locally, upsert it to Taiga, submit runs, and inspect env linter and QA jobs through the API.
5. If the direct Taiga path is blocked, push the single-task branch and open or update the PR so smithy CI converts the task and runs it in Taiga.
6. Review Taiga feedback from the API first when running locally, or from PR comments, checks, and artifacts in the fallback path.
7. Fix the task and repeat until Taiga reports zero critical or error issues.

## Done Condition

The task is done only when Taiga feedback reports zero critical or error issues, whether that feedback came from direct API access or the PR-backed fallback path.

## Final Step

When the task is clean:

- commit the task
- push the task
- leave a review or validation note if the workflow expects tracking

Do not stop at "looks good locally." Local success is necessary but not sufficient.
