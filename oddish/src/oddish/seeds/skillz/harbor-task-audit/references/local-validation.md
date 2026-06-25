# Local Validation

Do not claim the golden solution passes unless you actually run a local validation path and inspect the result.

## Minimum Evidence To Collect

Record:

- the exact commands you ran
- whether the task Docker image built successfully
- whether the golden solution ran successfully
- whether the verifier ran successfully
- the reward or pass or fail result
- any blocker that prevented a closer-to-real runtime path

## Baseline Precheck

When the repo provides a task structure validator, run it:

```bash
python scripts/validate-task-structure.py <task_dir>
```

This is only a structure check. It is not sufficient to validate the golden solution.

## Determine The Execution Shape Before Running Anything

Inspect:

- `solution/solve.sh`
- `tests/test.sh`
- any `tests/run_candidate.sh`, `tests/run_verifier.sh`, `tests/run_judge.sh`

Figure out which of these is true:

1. `tests/test.sh` already runs candidate code from `/solution`
2. `tests/test.sh` only verifies final artifacts in `/app`
3. the task needs a converted Harbor or Taiga wrapper to mirror the real runtime

Do not assume one fixed command works for every task.

## Standard Docker Validation Path

For source Harbor tasks, the task image is usually built from `environment/`:

```bash
docker build -t <tag> <task_dir>/environment
```

Then stage `tests/` and `solution/` into the running container and choose the runtime path based on the harness shape:

- if `tests/test.sh` already runs the candidate, mount `/tests` and `/solution`, then run `bash /tests/test.sh`
- if `tests/test.sh` only verifies outputs, run `bash /solution/solve.sh` first, then `bash /tests/test.sh`

Example skeleton:

```bash
docker run --rm \
  -v "<task_dir>/tests:/tests" \
  -v "<task_dir>/solution:/solution" \
  <tag> \
  bash -lc '<chosen commands>'
```

After the run, inspect:

- verifier stdout and stderr
- `/logs/verifier/reward.txt`
- `/logs/verifier/reward.json`

If the container exits successfully but the reward is not a full pass, treat that as a failed golden validation.

## Prefer Closer-To-Real Harbor Or Converted Runtime When Available

If local Harbor-to-Taiga tooling is available, prefer the more realistic path because it exercises the generated runtime and `grade_problem` behavior:

- convert the task locally with the installed `harbor-to-taiga` tooling
- build the generated image
- run the generated validation path or `grade_problem`-equivalent local test flow

Use this when the source Harbor harness is known to differ materially from the runtime that will actually grade the task.

## Golden Validation Failure Handling

If the golden does not pass:

- treat it as a blocking issue unless you can prove the local path was wrong
- record the exact failure mode
- say whether the failure came from the solution, the verifier, the environment, or an unresolved local-run mismatch

If you cannot complete validation:

- say exactly what you ran
- say exactly what failed or was missing
- do not upgrade the result to "passes locally"
