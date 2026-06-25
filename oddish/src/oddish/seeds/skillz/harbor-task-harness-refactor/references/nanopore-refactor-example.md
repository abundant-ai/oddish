# Nanopore Refactor Example

## Summary

This example shows the pattern for converting a Harbor task with a brittle harness into the fixed split shape used by the smithy conversion workflow.

## What Changed

- `tests/test.sh` became orchestration only.
- Candidate execution moved into `tests/run_candidate.sh`.
- Deterministic checks moved into `tests/run_verifier.sh`.
- Input staging moved into `tests/stage_data.sh`.
- Canonical reward handling stayed runtime-owned through the bound reward path.
- The verifier stopped trusting a model-written or sim-written results side channel.

## Why It Matters

- Keep solve-time and verify-time behavior separate.
- Remove writable scoring side channels when the verifier can compute the result directly.
- Keep the task Dockerfile and workspace permissions simple instead of patching around ownership problems inside the task.

## Validation Pattern

Use the same pattern for similar tasks:

1. validate the task structure
2. run the golden solution
3. run a broken or empty submission path
4. run any exploit-shape regressions
5. rebuild the image if the Dockerfile changed

## Lessons

- A passing golden path is not enough.
- An empty or broken transcript scoring as success is a harness bug.
- Local validation does not replace Taiga reruns or review tracking.
