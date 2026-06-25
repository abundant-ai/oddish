---
name: harbor-task-harness-refactor
description: Refactor Harbor task harnesses in the smithy repo. Use when working on `tasks/` entries that need harness splitting, verifier hardening, local validation, review notes, or tracking updates.
---

# Harbor Task Harness Refactor

## Overview

Use this skill for Harbor-format tasks in the smithy repo that need harness cleanup, verifier hardening, review, or tracking updates. Keep local validation first, then use available repo checks, PR feedback, or CI artifacts for additional confirmation when needed.

## Load References Deliberately

- Read [references/smithy-conversion-workflow.md](references/smithy-conversion-workflow.md) first when you need the repo workflow, one-task-per-PR rule, or review/tracking context.
- Read [references/harbor-task-harness-spec.md](references/harbor-task-harness-spec.md) when you need the phase contract, trust boundaries, or salvage-vs-rewrite decision.
- Read [references/harbor-task-rewrite-process.md](references/harbor-task-rewrite-process.md) when rewriting `tests/test.sh` into the fixed helper-script shape.
- Read [references/inclusion-policy.md](references/inclusion-policy.md) before adding rewritten task files or staged bundles to the repo.
- Read [references/task-validation-note.md](references/task-validation-note.md) when recording a task's validation status for review and tracking.
- Read [references/nanopore-refactor-example.md](references/nanopore-refactor-example.md) for a concrete end-to-end refactor example.

## Working Rules

- Inspect `instruction.md`, `task.toml`, `environment/Dockerfile`, `tests/test.sh`, and the verifier files under `tests/` before changing the harness.
- Classify the task first:
  - Standard-shape or close to standard: apply the split-harness pattern and keep the rewrite mechanical.
  - Bespoke shell harness or multi-rerun orchestration: handle it as a task-side rewrite before splitting the harness.
- Keep candidate code isolated in `run_candidate.sh`, deterministic checks in `run_verifier.sh`, and judge logic only in `run_judge.sh` when needed.
- Keep canonical reward handling runtime-owned and avoid task-local UID, GID, or `/logs/verifier` workarounds.
- After local validation, use repo checks, PR comments, and workflow artifacts as the feedback surface when they are available.
- Keep iterating on task fixes until local validation is clean and any required repo or review checks are addressed.
- If an external blocker prevents progress, such as missing local dependencies, unavailable CI secrets, or missing push or PR permissions, report the blocker explicitly.
- Keep validation notes, tracking docs, and rewritten task files separate unless the repo history needs a combined step.

## Harness Shape

- Preserve or create this helper-script shape:

- `tests/test.sh`
- `tests/stage_data.sh`
- `tests/run_candidate.sh`
- `tests/run_verifier.sh`
- `tests/run_judge.sh` only when the task truly needs judge logic

Apply these ownership rules:

- Keep `tests/test.sh` as orchestration only.
- Keep `tests/stage_data.sh` for input restoration and scratch cleanup.
- Keep `tests/run_candidate.sh` for candidate-owned execution.
- Keep `tests/run_verifier.sh` for deterministic checks.
- Keep `tests/run_judge.sh` only for judge evaluation.
- Do not invent one-off harness layouts for a single task.
