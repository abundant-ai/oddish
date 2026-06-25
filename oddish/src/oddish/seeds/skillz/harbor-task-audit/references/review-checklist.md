# Harbor Task Review Checklist

Use this checklist to drive the five required check groups. Do not skip a section just because the task looks familiar.

## 1. Instruction Clarity And Real-Worldness

Confirm that the prompt is a clear, well-defined real-world task.

Check for:

- a concrete business or engineering context rather than benchmark framing
- a clearly named deliverable
- explicit output files or implementation target when relevant
- enough detail to solve the task without guessing hidden conventions
- no contradictions between prose, starter files, and expected outputs
- no instructions that are impossible inside the provided environment
- no references to grading, training, hidden tests, or a golden solution
- no ambiguous words such as "correct", "reasonable", or "for that cycle" when the verifier demands an exact semantic
- no missing requirements on exact filenames, notebook cells, CLI interfaces, ports, schemas, or latency semantics

Look specifically for prompt-versus-verifier gaps such as:

- verifier expects a one-cycle response but the prompt only describes functional behavior
- verifier expects exact formatting but the prompt only asks for a report
- verifier assumes invalid events still count, but the prompt does not say so
- verifier checks a post-filter count while the prompt describes pre-filter scope

If the task needs rewriting suggestions, keep them realism-preserving:

- clarify the hidden requirement in the prompt, or
- change the verifier to match the written requirement

Do not suggest making the task easier just to silence a mismatch.

## 2. Verifier Consistency And Correctness

Confirm that the verifier is faithful to the task contract and internally coherent.

Check for:

- assertions that map cleanly to prompt-stated requirements
- no contradictory assertions across verifier files
- no missing checks for core required behavior
- no checks for unstated behavior
- deterministic evaluation where determinism is expected
- a harness layout that keeps phases separated when the task fits the standard split shape:
  - `tests/test.sh` as orchestration only
  - `tests/stage_data.sh` for input restore and scratch cleanup
  - `tests/run_candidate.sh` for candidate-owned execution only
  - `tests/run_verifier.sh` for deterministic checks only
  - `tests/run_judge.sh` only when judge logic is actually needed
- reward semantics that are clearly derived from verifier results
- infrastructure failures surfaced distinctly from model failures when possible
- timeout budgets that fit inside `task.toml` limits
- hidden fixtures and staged data that align with prompt scope
- for LLM/VLM judge tasks with `tests/agentic_judge.json`, the AgenticGrader spec and `agentic_rubric.json` pass `validate_agentic_jsons.py`
- for LLM/VLM judge tasks without `tests/agentic_judge.json`, the report flags the missing structured AgenticGrader input as a conversion-blocking issue for current harbor-to-taiga conversion

Common failure modes:

- `tests/test.sh` mixes orchestration, candidate execution, deterministic verification, and reward handling into one brittle wrapper
- `tests/test.sh` collapses pytest exit codes or verifier errors to `exit 1`, making infra failures look like task failures
- deterministic checks live in candidate-owned execution paths or ad hoc shell logic instead of a trusted verifier entrypoint
- verifier subprocess timeouts exceed `task.toml` `verifier.timeout_sec`
- judge rubric and deterministic checks disagree
- `agentic_judge.json` references missing trusted artifacts, invalid predicted paths, duplicate artifact names, or rubric keys that do not exist in `agentic_rubric.json`
- `agentic_rubric.json` mixes artifact path wiring, model/API instructions, or runtime details into scoring criteria
- AgenticGrader criteria are left in compatibility `rubric.json` for a new task, or deterministic MCP-only rubric data is not separated into `mcp_rubric.json`
- a detected LLM/VLM judge task relies on `--llm-judge-grading mcp` or another MCP-hosted legacy judge fallback instead of structured AgenticGrader inputs
- verifier trusts candidate-produced summaries instead of recomputing truth
- reward files are written opportunistically rather than as a controlled final verdict

## 3. Golden Solution Cleanliness And Correctness

Confirm that the golden solution is a legitimate solution to the task, not a verifier-specific exploit.

Check for:

- the solution follows the prompt instead of abusing verifier quirks
- the solution only touches allowed outputs
- the solution does not depend on hidden files or verifier-only paths
- the solution is readable and consistent with the task's expected implementation style
- the solution stages the right files into `/app` or equivalent runtime locations
- the solution produces a full local pass through the actual verifier path

Red flags:

- solution writes reward artifacts directly
- solution imports verifier code or reads hidden fixtures
- solution depends on exact test names or hidden output formatting
- solution only passes because of environment leftovers or one-off local state

## 4. Docker And Dependency Readiness

Confirm that the environment supports both solving and verifying.

Check for:

- all system packages and language dependencies needed by starter code, golden solution, and verifier
- all required data and starter artifacts copied into the image
- working directories and output directories created as expected
- no missing runtime tools such as `pytest`, `jupyter`, `nbconvert`, `verilator`, `bash`, `Rscript`, or compilers
- no flaky network fetches at build time unless unavoidable
- no inconsistent base-image or permission assumptions
- resource requests in `task.toml` that are plausible for the verifier and golden runtime

Look at both build-time and run-time consistency:

- does the Dockerfile copy the files that `solution/solve.sh` expects?
- does the verifier assume paths that the image never creates?
- do the timeout and CPU or memory settings match the actual workload?

## 5. Reward-Hacking And Trust-Boundary Resistance

Confirm that untrusted candidate code cannot fake success without actually solving the task.

Check for:

- candidate code cannot write or replace canonical reward artifacts
- verifier does not import, execute, or parse candidate-owned code in a trusted phase
- verifier does not trust candidate-controlled JSON, logs, or stdout as authoritative without independent cross-checks
- runtime-owned directories such as `/logs/verifier`, `/grader`, `/data`, or hidden fixture roots are not writable by the candidate
- shared simulation or notebook environments do not give the candidate a path to forge verifier-owned outputs
- task-local harness scripts do not write canonical `/logs/verifier/reward.json` or `/logs/verifier/reward.txt` directly when runtime-owned finalization should do that
- static scanners are robust enough for the task language, or the verifier uses stronger enforcement than raw regexes
- run nonces or equivalent integrity checks are strict if structured reward files are accepted

Use [reward-hacking-patterns.md](reward-hacking-patterns.md) for common exploit patterns.

## Severity Guidance

Use severity labels consistently in the report:

- `Critical`: can cause false pass, false fail, broken local validation, or task invalidity
- `Warning`: real mismatch or weakness that can misgrade or confuse solvers, but not always a guaranteed exploit
- `Note`: non-blocking cleanup or robustness improvement
