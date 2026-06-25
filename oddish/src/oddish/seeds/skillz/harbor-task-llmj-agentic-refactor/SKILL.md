---
name: harbor-task-llmj-agentic-refactor
description: Refactor existing Harbor task PRs that use legacy tests/llm_judge.py, tests/visual_judge.py, or tests/vlm_judge.py into the declarative agentic_judge.json plus criteria-only agentic_rubric.json format supported by the new harbor-to-taiga AgenticGrader conversion pipeline.
---

# Harbor Task LLMJ Agentic Refactor

## Overview

Use this skill when updating existing Harbor task branches or PRs that still encode LLM/VLM judge artifact wiring in Python. The goal is to make the task convertible by the new harbor-to-taiga agentic grading path without weakening deterministic verifier coverage or opening/publishing PRs unless the user explicitly asks.

Current harbor-to-taiga behavior: detected Harbor LLM/VLM judge tasks must provide structured AgenticGrader inputs. `llm_judge_grading=agentic` is the default for detected judge tasks, and `llm_judge_grading=mcp` is no longer a compatibility path for Python-only LLM judges.

## Load References

- Read [references/new-agentic-judge-format.md](references/new-agentic-judge-format.md) before editing a task. It is the local copy of the new format contract.
- Read [references/migration-examples.md](references/migration-examples.md) when mapping a legacy judge shape to `agentic_judge.json`.
- Run this skill's `scripts/inspect_legacy_judge.py <task-dir>` for a best-effort draft of artifact wiring, then review the output manually before writing it into the task.
- Run this skill's `scripts/validate_agentic_jsons.py <task-dir>` after editing `tests/agentic_judge.json`, `tests/agentic_rubric.json`, compatibility `tests/rubric.json`, or `tests/mcp_rubric.json`.

## Workflow

1. Identify the task branch and task directory.
   - If the user gives a GitHub PR link, inspect the PR files with `gh pr view`, `gh pr diff --name-only`, or `gh api` first.
   - Work in the repo or explicit worktree the user names.
   - Check `git status --short` before editing and do not overwrite unrelated local changes.

2. Inspect the full verifier surface.
   - Read `instruction.md`, `task.toml`, `tests/test.sh`, `tests/agentic_rubric.json` or fallback `tests/rubric.json`, any `tests/mcp_rubric.json`, the legacy judge file, deterministic verifier files, and `tests/gold_results/`.
   - Find every judged artifact path, reference artifact path, MIME type, rubric key, and any judge-specific prompt nuance.
   - Keep deterministic checks in deterministic verifier files. The AgenticGrader should judge only the former LLM/VLM portion.
   - Treat a Python-only judge with no `agentic_judge.json` as needing migration. Do not leave it as MCP-hosted legacy judge behavior.

3. Add `tests/agentic_judge.json`.
   - Use `version: 1`, `scoring_mode: "rubric"`, and explicit artifact entries.
   - Use `mode: "visual_artifact_comparison"` for image/reference comparison tasks, which is the supported v1 mode in the current format.
   - Set each artifact's `predicted_path`, optional `reference_path`, `rubric_key`, `media_type`, and concise `description`.
   - Use `/app`, `/repo`, `/workspace`, or `/workdir` for predicted paths and `/tests/...` for trusted reference paths.
   - Do not put glob patterns or path wiring in `agentic_rubric.json`.

4. Normalize `tests/agentic_rubric.json`.
   - Make rubric keys match `artifact.rubric_key`.
   - Prefer `{"artifact_key": ["criterion", ...]}`.
   - Keep criteria only: no predicted paths, reference paths, client details, model names, or execution instructions.
   - New structured tasks should use `tests/agentic_rubric.json`; `tests/rubric.json` is only a compatibility fallback during migration.
   - If deterministic verifier code needs a rubric or threshold file in the MCP tests payload, put that data in `tests/mcp_rubric.json`. The converter materializes it as `/grader/tests/rubric.json` for MCP-side compatibility.
   - If the old rubric has one bucket such as `visual_requirements`, rename it to the artifact key unless multiple artifacts truly share that one rubric key.

5. Refactor the legacy judge into a compatibility runner.
   - Keep Harbor local execution working.
   - Make the judge read `/tests/agentic_judge.json` and `/tests/agentic_rubric.json`, with `/tests/rubric.json` only as a temporary fallback for older branches.
   - Remove task-specific hard-coded maps such as `PLOTS = {...}` from the judge.
   - Preserve infra-error semantics: missing API keys, malformed judge responses, and missing trusted reference files should exit as grader infrastructure failures, not model failures.
   - The Taiga AgenticGrader is the real converted judge; do not add converter-only behavior that shells out to the legacy judge.

6. Keep `tests/test.sh` phase boundaries clear.
   - Candidate execution, deterministic verification, and LLM/VLM judge execution should be separate blocks.
   - Do not remove the judge from Harbor `test.sh`; the converter needs the boundary so it can bypass only the judge phase for MCP grading.
   - Expect the converter's MCP tests payload to remove AgenticGrader-only files: `llm_judge.py`, `vlm_judge.py`, `check_llm_scores.py`, `agentic_judge.json`, `agentic_rubric.json`, and compatibility `rubric.json`.
   - Do not suggest `--llm-judge-grading mcp` as a workaround for detected LLM/VLM judge tasks; the converter rejects that path for judge tasks.
   - If the old harness blends deterministic and judge scores, preserve Harbor reward behavior while keeping separate deterministic and judge report files where possible.

7. Validate locally.
   - Run `python3 <this-skill>/scripts/inspect_legacy_judge.py <task-dir>` and compare it with the committed `agentic_judge.json`.
   - Run `python3 <this-skill>/scripts/validate_agentic_jsons.py <task-dir>` to check schema shape, rubric-key alignment, path rules, and trusted reference files.
   - Run the task's local verifier path when feasible. If no judge API key is available, at least run deterministic checks or a no-API smoke path and report the limitation.

## Done Conditions

- The task has `tests/agentic_judge.json`.
- `tests/agentic_rubric.json` is criteria-only and aligned with artifact rubric keys, or `tests/rubric.json` is intentionally left only as a legacy fallback.
- Any deterministic MCP-only rubric data lives in `tests/mcp_rubric.json`, not in the AgenticGrader rubric.
- `scripts/validate_agentic_jsons.py <task-dir>` passes.
- The legacy judge reads the declarative spec instead of owning task-specific artifact wiring.
- The task is not relying on MCP-hosted legacy LLM judge conversion.
- Deterministic verifier logic remains intact and is not moved into the AgenticGrader.
- No new GitHub PR is opened unless the user explicitly requests publishing.
