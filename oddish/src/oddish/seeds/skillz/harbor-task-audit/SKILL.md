---
name: harbor-task-audit
description: Review a Harbor task end-to-end and write a markdown QA report covering instruction clarity, verifier consistency, golden-solution correctness, Docker and dependency readiness, and reward-hacking resistance. Use when working in any branch with a Harbor-format task under `tasks/`, especially before review, after QA feedback, or when auditing `instruction.md`, `tests/`, `solution/`, `task.toml`, or `environment/Dockerfile`.
---

# Harbor Task Audit

## Overview

Use this skill to audit one Harbor-format task and produce a comprehensive markdown report. Read the task files first, build an explicit model of the task contract and grading flow, then assess the task against the five required check groups without weakening the task.

## Load References Deliberately

- Read [references/review-checklist.md](references/review-checklist.md) for the detailed criteria for each check group.
- Read [references/local-validation.md](references/local-validation.md) before claiming that the golden solution passes locally.
- Read [references/reward-hacking-patterns.md](references/reward-hacking-patterns.md) when reviewing trust boundaries or exploitability.
- Read [references/report-format.md](references/report-format.md) before writing the final report.
- For tasks with `tests/llm_judge.py`, `tests/visual_judge.py`, `tests/vlm_judge.py`, or `tests/agentic_judge.json`, also use `../harbor-task-llmj-agentic-refactor/scripts/validate_agentic_jsons.py` when the declarative AgenticGrader JSON files are present.

## Working Rules

- Assume the branch should contain exactly one Harbor task. If the branch contains multiple task directories, resolve that ambiguity before continuing.
- Read the task artifacts before making any judgment. At minimum inspect:
  - `task.toml`
  - `instruction.md`
  - `environment/Dockerfile`
  - `solution/solve.sh` plus any solution assets it stages
  - `tests/test.sh`
  - every verifier, judge, fixture, helper, notebook, or hidden-data staging file under `tests/`
- Build a concrete model of:
  - the deliverable the agent must produce
  - the files the agent is allowed to modify or create
  - how candidate code is executed
  - how the verifier computes reward
  - whether any LLM/VLM judge is legacy Python-only or declarative `agentic_judge.json` plus `agentic_rubric.json`
  - whether the harness follows the expected split verifier layout
  - which files and directories are trusted versus candidate-controlled
- Keep review findings tied to evidence. Prefer file and line references over paraphrase-only claims.
- Distinguish three states explicitly:
  - confirmed issue
  - plausible risk that still needs proof
  - unverified assumption or blocker
- Treat normal Harbor agent execution as the reward-hacking visibility boundary. Do not flag task-source metadata or provenance files that exist only in the host-side task package as a reward-hacking issue unless they are copied into the image, mounted into the agent workspace, exposed through task instructions, or otherwise available during candidate execution. If the user explicitly asks about source-distribution policy outside Harbor runtime, discuss it separately from reward hacking.
- Keep suggested fixes difficulty-preserving. Do not suggest relaxing checks in a way that lowers task realism, lowers verification coverage, or turns the task into a fake toy exercise.
- Treat the report as the deliverable unless the user explicitly asks for task edits.

## Workflow

### 1. Identify The Task And Inventory The Files

- Find the task directory under `tasks/`.
- Run the repo structure check when available:
  - `python scripts/validate-task-structure.py <task_dir>`
- Read the task files in full or in enough detail to understand the end-to-end solve and verify path.
- Record the expected deliverable, output paths, runtime limits, verifier entrypoints, and harness layout before analyzing quality.
- If the task uses an LLM/VLM judge, record whether it has `tests/agentic_judge.json` and `tests/agentic_rubric.json`, or only legacy fallback `tests/rubric.json`, and identify the artifact paths and rubric keys that the judge is supposed to use.
- If the task also has `tests/mcp_rubric.json`, treat it as deterministic MCP-owned data, not AgenticGrader criteria.
- Compare the task's `tests/` layout against the expected split shape:
  - `tests/test.sh` for orchestration only
  - `tests/stage_data.sh` for input restoration and scratch cleanup
  - `tests/run_candidate.sh` for candidate-owned execution only
  - `tests/run_verifier.sh` for deterministic verification only
  - `tests/run_judge.sh` only when the task truly needs judge logic

### 2. Reconstruct The Task Contract

- Summarize the task in plain language:
  - what a solver is supposed to do
  - what artifacts count as success
  - what the verifier actually measures
- Compare the written prompt with the effective contract enforced by the verifier.
- Pay attention to hidden assumptions about latency, exact formatting, environment state, output filenames, or side effects.

### 3. Run The Five Check Groups

- Evaluate the task using the criteria in [references/review-checklist.md](references/review-checklist.md):
  - instruction clarity and real-worldness
  - verifier consistency and correctness
  - golden-solution cleanliness and correctness
  - Docker and dependency readiness
  - reward-hacking and trust-boundary resistance
- For each issue, capture:
  - what is wrong
  - where it appears
  - why it matters
  - what fix options preserve task quality

### 4. Validate LLMJ Agentic JSONs When Present

- If the task has `tests/agentic_judge.json`, run:
  - `python3 <skills-root>/harbor-task-llmj-agentic-refactor/scripts/validate_agentic_jsons.py <task_dir>`
- Treat validator errors as verifier consistency findings because they can break or miswire Taiga AgenticGrader conversion.
- If the task has a legacy LLM/VLM judge file but no `tests/agentic_judge.json`, do not claim AgenticGrader JSON validation passed. Treat this as a conversion-blocking verifier consistency finding for tasks expected to go through current harbor-to-taiga conversion, because detected LLM/VLM judge tasks must now provide structured AgenticGrader inputs.
- If the validator script is unavailable, manually check the same invariants and state that the scripted validation could not be run.

### 5. Validate The Golden Solution Locally

- Follow [references/local-validation.md](references/local-validation.md).
- Prefer the closest available execution path to the real Harbor or converted runtime.
- Do not claim the golden passes unless you actually ran the relevant local validation path and inspected the resulting reward or test output.
- If the full local path is unavailable, say exactly what was run, what was not run, and what blocker prevented stronger validation.

### 6. Write The Report

- Use the structure in [references/report-format.md](references/report-format.md).
- Include a section for each required check group even if that section has no findings.
- Order findings by severity inside each section.
- Keep recommendations concrete and scoped. If there are multiple viable fixes, list the tradeoffs.
- End with a short verdict that states:
  - whether the task is currently shippable
  - which findings are blocking
  - whether the golden solution was locally validated

## Harbor Task Expectations

- Prefer real-world task framing. Instructions should read like an internal engineering request, analysis request, or implementation ticket, not like a benchmark prompt.
- Flag any prompt language that leaks evaluation framing, mentions a hidden golden solution, or otherwise breaks the real-world illusion.
- Flag verifier logic that enforces requirements absent from the prompt, including hidden timing or formatting constraints.
- Flag harness layouts that collapse orchestration, candidate execution, deterministic verification, and reward handling into one script when the split helper-script shape would separate those phases cleanly.
- Treat canonical reward artifacts, verifier-only scratch, hidden inputs, and runtime-owned directories as trusted-only surfaces. If candidate code can write or meaningfully influence them, treat that as a serious finding.
- When judging hidden-data or source-metadata leakage, first prove candidate runtime visibility. Host-side task files that Harbor never copies or mounts for the agent are not leaks under the normal Harbor threat model.
