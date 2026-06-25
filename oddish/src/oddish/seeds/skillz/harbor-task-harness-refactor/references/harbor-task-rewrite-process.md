# Harbor Task Rewrite Process

## Goal

Shrink `tests/test.sh` into orchestration only and move each responsibility into the fixed helper-script shape.

## Rewrite Shape

Keep or create:

- `tests/test.sh`
- `tests/stage_data.sh`
- `tests/run_candidate.sh`
- `tests/run_verifier.sh`
- `tests/run_judge.sh` only when a task actually needs judge logic

## Ownership Rules

- `tests/test.sh` handles orchestration only.
- `tests/stage_data.sh` restores inputs and clears task-owned scratch.
- `tests/run_candidate.sh` runs candidate-owned code only.
- `tests/run_verifier.sh` runs deterministic verification only.
- `tests/run_judge.sh` runs judge logic only.
- Do not let the wrapper own grading logic or candidate execution directly.

## Triage Order

When reviewing a task, check:

1. Does `test.sh` execute model code directly?
2. Does `test.sh` stage hidden data with a mode-preserving copy?
3. Can candidate execution move into `run_candidate.sh`?
4. Can deterministic verification move into `run_verifier.sh`?
5. Does any judge logic belong in `run_judge.sh`?

## Common Fixes

- Keep hidden-input staging from preserving restrictive verifier-owned metadata into the candidate workspace.
- Keep canonical reward handling runtime-owned.
- Keep the wrapper readable as a high-level flow, not a place for business logic.
