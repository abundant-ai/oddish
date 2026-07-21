# Pre-trial & post-trial QA analyzer — design

**Date:** 2026-07-21
**Branch:** `kate/pre-trial-analyzer`
**Status:** Design (pending implementation plan)

## Summary

Add a linked, two-stage QA analysis pipeline for Harbor tasks, built on the
existing prompt/`Block` primitives:

1. A **pre-trial QA agent** that audits a task's *source* (verifier, oracle,
   info leakage) before/independent of trials and emits **action items** that
   reference concrete `file` + `line` locations.
2. A **post-trial QA agent** — an expansion of the existing per-trial log
   analysis — that consumes the pre-trial action items, determines whether each
   was **exploited** in a trial trajectory (and whether trajectory behavior was
   **caused** by an identified weakness), and generates **additional** action
   items from the trajectory itself.

Supporting infrastructure: a **shared action-item schema**, a **structured
trajectory file-inspection metadata** parser (so pre-trial `file` refs match
trajectory activity reliably), a **DB-backed, versioned prompt registry**
configurable via the `oddish` CLI, new **task persistence** columns, and
**frontend** deep-linking + an action-items panel.

This is one spec with a **phased** build order (below); it is not decomposed
into separate specs because the components are tightly coupled through the
shared schema and the pre→post linkage.

## Goals

- Surface task-authoring defects (verifier completeness, oracle correctness,
  info leakage) as actionable, file/line-anchored items **before** relying on
  trial outcomes.
- Close the loop: show which pre-trial-identified weaknesses actually got
  exploited by agents, with evidence, and elevate those.
- Make analyzer prompts editable and versioned from the CLI without a code
  deploy.
- Render everything in the dashboard with clickable `file:line` deep-links.

## Non-goals

- Migrating the existing classify/verdict/probe prompts into the registry (only
  the two new prompts are seeded; others can migrate later).
- Changing the verdict taxonomy or the verdict's own output contract (the
  post-trial expansion adds fields; it does not remove/rename existing ones).
- Auto-fixing tasks. The agents *recommend*; humans act.

## Background / current state

(Verified against the tree on `kate/pre-trial-analyzer`, 2026-07-21.)

- **Post-trial verdict path** lives in `oddish/src/oddish/analyze/`
  (`classifier.py`, `models.py`), synthesized once per task in
  `oddish/src/oddish/workers/queue/qa_handler.py::run_task_qa_job` and persisted
  via `oddish/src/oddish/core/verdict_sync.py`. `TaskModel.verdict` +
  `verdict_status/_error/_started_at/_finished_at` at
  `oddish/src/oddish/db/models.py:780-791`.
- **`Block` primitive** and the analyzer-block port live in
  `backend/api/services/blocks/…` (`block.py`, `analyzer/analyzer_block.py`,
  `analyzer/verdict/verdict_block.py`, `analyzer/analyzer_llm_client.py`).
  `AnalyzerType` enum + `analyzer_blocks` DB table already persist each run's
  `prompt`/`input`/`output`/`type`/`llm_client_type`.
- **Classifier agent tools** are restricted to `Read,Glob` and rely on the
  handler **pre-pulling** task files from S3 (`resolve_task_directory`) mounted
  via `--add-dir` (`analyze/classifier.py:326-335`,
  `workers/queue/analysis_handler.py:79`).
- **AnalyzerBlock sandbox** (`SandboxAnalyzerLLMClient` →
  `cc_chat/claude_code_runtime.py`) has the **full toolset including Bash** and
  network, but does **not** install the `oddish` CLI and mints **no**
  credentials into that sandbox today.
- **Task source fetch** exists:
  `oddish pull <TASK_ID> --type task --include-task-files -o <DIR>`
  (`oddish/src/oddish/cli/pull.py:789-854`), landing files at
  `<DIR>/tasks/<TASK_ID>/files/…`.
- **Probe analyzer** already parses agent messages / tool uses
  (`oddish/src/oddish/worker/probe_analysis.py`) — reuse this for the
  trajectory file-inspection parser.
- **Frontend** file viewing is the `TaskFilesPanel` drawer
  (`frontend/src/components/task-files-panel.tsx`) with an `initialFilePath`
  prop seam but **no URL/hash routing**, and `renderers/code-highlight.tsx`
  renders line numbers as plain spans with **no per-line `id` anchors**. Both
  must be added for `file:line` deep-links.
- **No DB-backed prompt config exists** — greenfield.

## Architecture

Package boundaries are respected: pure schema/logic/persistence in `oddish/`,
block + sandbox + routers in `backend/`, UI in `frontend/`. `oddish/` does not
import `backend/`; the pre-trial synthesis is injected into the QA handler the
same way `verdict_synth_fn` is.

### Component 1 — Shared `ActionItem` schema

Location: `oddish/src/oddish/analyze/models.py` (pydantic/dataclass mirroring the
existing `TaskVerdictModel`/`TrialClassificationModel` split — a
`*Model` for LLM structured output and a dataclass for internal use).

```
ActionItem:
  id: str                       # stable, content-derived (source+dimension+file+line hash)
  source: "pre_trial" | "post_trial"
  problem_type: "incompleteness" | "mismatch"
  dimension: "verifier" | "oracle" | "info_leakage"
  file: str                     # task-relative path, e.g. "verifier.py"
  line_start: int
  line_end: int
  title: str
  detail: str                   # what's wrong
  recommendation: str           # what to do
  tier: "must_fix" | "should_fix" | "optional"

  # post_trial-only linkage fields (null/false for pre_trial items):
  links_to: str | None          # pre_trial ActionItem.id this relates to
  exploited: bool               # did the trajectory exploit this weakness?
  exploit_evidence: str | None  # quote / step ref
  causal: bool                  # did trajectory behavior result from this weakness?
```

The three pre-trial focuses map onto the grid: **verifier completeness** =
`verifier`+`incompleteness`; **oracle correctness** = `oracle`+`mismatch`;
**info leakage** = `info_leakage` dimension (problem_type as the model judges).
Both fields are kept so the taxonomy stays 2×3-expressible and filterable.

### Component 2 — Pre-trial block (`backend/`)

- New `AnalyzerType.PRE_TRIAL` and `PreTrialBlock`
  (`backend/api/services/blocks/analyzer/pre_trial/pre_trial_block.py`),
  `output_schema = list[ActionItem]` (pre_trial fields only).
- Runs on the **sandbox** LLM client (Bash-enabled).
- The prompt (loaded from the versioned registry, Component 5) is templated with
  the task id and instructs the agent to run **exactly**:
  `oddish pull <TASK_ID> --type task --include-task-files -o ./task_src`,
  then Read/Glob `./task_src/tasks/<TASK_ID>/files/**` and cite real
  `file`/`line_start`/`line_end`.
- **Sandbox provisioning (new plumbing):** install the `oddish` CLI into the
  block sandbox and inject a **scoped, read-only** API key/env so `oddish pull`
  authenticates. Follows the precedent of the `oddish-query` upload +
  minted-read-key pattern in the interactive-chat `orchestrator.py`, adapted to
  the `create_llm_client`/`Provisioner` path used by the block sandbox. The key
  is short-lived and read-scoped; document the credential surface in the plan.
- Auto-persists to `analyzer_blocks` (prompt/input/output) like other blocks,
  additionally recording the prompt registry `prompt_key`+`version` used
  (Component 5).

### Component 3 — Post-trial expansion (`oddish/` + block)

Extends the existing per-trial classifier and its block. Additional inputs per
trial:

- the task's **pre-trial action items** (from `TaskModel.pre_trial_analysis`), and
- the trial's **file-inspection metadata** (Component 4).

Additional outputs per trial (alongside the existing `TrialClassification`):

- an **exploitation assessment** for each pre-trial action item it can evaluate:
  `{ links_to, exploited, exploit_evidence, causal }`, and
- **new** trajectory-derived `ActionItem`s (`source="post_trial"`).

Exploited items are **elevated** (double-flagged) when rolled up: an
`exploited=true` pre-trial item is surfaced with escalated tier and an
"exploited in trial N" badge. The classifier prompt (registry-backed) is
extended to describe the linkage task; the file/line refs let the agent grep the
trajectory precisely, and Component 4 lets it match structurally.

### Component 4 — Trajectory file-inspection metadata (`oddish/`)

Parse each trajectory component (tool uses / agent messages) for the set of
files it inspected or wrote, extending the parsing already in
`oddish/src/oddish/worker/probe_analysis.py`
(`_parse_agent_messages`, `_classify_tool_use`). Produce, per trial, a compact
structure:

```
TrajectoryFileAccess:
  step_index: int
  tool: str
  files_read: [str]
  files_written: [str]
  commands: [str]      # e.g. grep/cat targets, best-effort path extraction
```

Stored alongside the trial's classification input so the post-trial agent (and
future consumers) can match a pre-trial item's `file` against actual trajectory
activity without fuzzy grep. **Fallback** if parsing misses a case: the agent
still greps the raw trajectory using the action-item refs.

### Component 5 — Versioned prompt registry (DB + backend + CLI)

New tables (`oddish/src/oddish/db/models.py` + Alembic migration):

```
prompts:
  key: str (PK)                 # e.g. "pre_trial_qa", "post_trial_qa"
  description: str
  active_version: int           # FK-ish pointer into prompt_versions
  created_at, updated_at

prompt_versions:
  id: PK
  prompt_key: FK -> prompts.key
  version: int                  # monotonic per key
  content: text
  created_at
  created_by: str | None
  (unique: prompt_key + version)
```

- **Semantics:** `set` appends a new immutable version and (by default)
  activates it; history is never mutated. `activate` flips `active_version` to
  roll forward/back. Blocks load the **active** version at run time and record
  `prompt_key`+`version` on the `analyzer_blocks` row (add columns) so every run
  is traceable to an exact prompt.
- **Backend router** (`backend/api/routers/prompts.py`, registered in
  `backend/api/app.py`): `GET /prompts`, `GET /prompts/{key}`,
  `GET /prompts/{key}/versions`, `PUT /prompts/{key}` (append+activate),
  `POST /prompts/{key}/activate`.
- **CLI** (`oddish/src/oddish/cli/prompt.py`, thin httpx client like
  `cli/report.py`, attached in `cli/__init__.py`):
  `oddish prompt list | get <key> [--version N] | set <key> --file f.txt |
  versions <key> | activate <key> <version> | diff <key> <vA> <vB>`.
- **Seeding:** the migration seeds `pre_trial_qa` and `post_trial_qa` (v1) from
  the prompt text checked into the repo (kept as the canonical source-of-truth
  fallback the migration reads from).

### Component 6 — Persistence & QA-job wiring (`oddish/`)

- New `TaskModel` columns mirroring verdict:
  `pre_trial_analysis` (JSONB, the action-item payload) +
  `pre_trial_analysis_status` (reuse `VerdictStatus`) +
  `_error` (Text) + `_started_at` / `_finished_at` (timestamptz). New migration.
- `build_pre_trial_analysis_payload` + `sync_pre_trial_analysis_to_task`
  (`oddish/src/oddish/core/`), analogous to `verdict_sync.py` but **must not**
  set `task.status = COMPLETED` and must not touch verdict columns.
- Injected `pre_trial_synth_fn` seam (mirrors `verdict_synth_fn`) so `backend/`
  supplies the block-backed implementation without `oddish` importing `backend`.
- In `run_task_qa_job`: run the pre-trial analysis **once, before** the
  per-trial loop (after the RUNNING-status setup), persist via its own sync
  under `asyncio.shield`. Its action items are then passed into
  `classify_trial_and_store` for the per-trial post-trial expansion. Post-trial
  action items + exploitation assessments ride on the existing per-trial
  classification storage and roll into the verdict payload.

### Component 7 — Frontend (`frontend/`)

- **Per-line anchors:** add `id="L{n}"` (and range highlight support) to each
  line in `renderers/code-highlight.tsx`.
- **URL-driven viewer:** drive `TaskFilesPanel`'s `initialFilePath` (and open
  state) from URL query/hash, so `…/tasks/{id}?file=verifier.py#L42` opens the
  drawer, selects the file, scrolls to and highlights the line(s).
- **Action-items panel:** render pre-trial + post-trial items grouped by
  `dimension` and `tier`; each item's `file:line` is a clickable deep-link;
  **exploited** items are visually elevated with an "exploited in trial N" badge
  and a link to the trajectory step. Reuse verdict/probe rendering patterns
  (`task-detail-client.tsx`, `probe-run-summary.tsx`).

## Data flow

```
QA job (once per task)
  └─ pre_trial_synth_fn  ──► PreTrialBlock (sandbox: `oddish pull` → Read/Glob)
        └─ list[ActionItem source=pre_trial]  ──► TaskModel.pre_trial_analysis
  └─ per trial:
       classify_trial_and_store(
           trial, pre_trial_items, trajectory_file_access )
         └─ TrialClassification (existing)
         └─ exploitation assessments  (links_to / exploited / causal)
         └─ list[ActionItem source=post_trial]
  └─ verdict_synth_fn  ──► verdict payload (unchanged contract + rolled-up items)

Frontend
  TaskModel.pre_trial_analysis + per-trial post_trial items
    └─ Action-items panel ──(file:line)──► TaskFilesPanel deep-link (#L42)
```

## Phased build order

1. **Schema + registry:** `ActionItem` model; `prompts`/`prompt_versions`
   tables + migration; backend router; `oddish prompt` CLI; seed both prompts.
2. **Pre-trial block:** `AnalyzerType.PRE_TRIAL` + `PreTrialBlock`; sandbox
   `oddish` CLI + scoped-key provisioning; prompt authored + seeded.
3. **Persistence + wiring:** `TaskModel` columns + migration;
   `sync_pre_trial_analysis_to_task`; `pre_trial_synth_fn` seam; run-once wiring
   in `run_task_qa_job`.
4. **Trajectory file-inspection metadata:** parser extending
   `probe_analysis.py`; per-trial storage.
5. **Post-trial linkage:** extend classifier + its block to consume pre-trial
   items + file-access metadata; emit exploitation assessments + new items;
   roll-up/elevation.
6. **Frontend:** line anchors; URL-driven viewer; action-items panel.

Each phase is independently testable; 1–3 deliver a working pre-trial audit
before the post-trial linkage lands.

## Testing

- **Schema:** round-trip `ActionItem` model↔dataclass; id stability.
- **Registry:** append creates a new version and activates; `activate` rolls
  back; blocks record the exact `prompt_key`+`version`; CLI verbs hit the router
  (httpx mock) and against a throwaway migrated Postgres (per the local-backend
  test DB gotcha — use a fresh DB, not the shared one).
- **Pre-trial block:** given a fixture task dir, the agent produces items with
  valid `file`/`line` within the source; sandbox provisioning smoke test that
  `oddish pull` authenticates and lands files.
- **Persistence:** `sync_pre_trial_analysis_to_task` writes its columns and does
  **not** complete the task or touch verdict columns; parity/shield behavior.
- **Trajectory parser:** unit tests over recorded tool-use fixtures
  (files_read/written/commands extraction), including the fallback path.
- **Post-trial linkage:** a fixture where a known verifier gap is exploited →
  `exploited=true`, correct `links_to`, elevated tier; and a clean trajectory →
  `exploited=false`.
- **Frontend:** line-anchor scroll/highlight; `?file=…#L42` deep-link opens and
  selects; action-items panel groups + elevates exploited items.

## Risks & open questions

- **Credential surface:** injecting an `oddish` read key into the block sandbox
  is new. Mitigate with a short-lived, read-scoped key; document in the plan.
  (This is the path chosen over handler-pre-pull, per the decision to have the
  agent run `oddish pull` itself.)
- **Cost/latency:** pre-trial adds one sandbox agent run per task; runs once, so
  amortized across trials. Gate behind a setting if needed (mirror
  `verdict_via_analyzer_block`).
- **Trajectory path extraction** from arbitrary shell commands is best-effort;
  the raw-grep fallback covers misses.
- **Verdict payload growth:** rolling post-trial items into the verdict payload
  must not break the existing verdict contract / parity test — additive only.

## Key files to touch

- `oddish/src/oddish/analyze/models.py` (ActionItem), `analyze/classifier.py`
  (post-trial expansion)
- `oddish/src/oddish/db/models.py` (+ Alembic migrations) — `prompts`,
  `prompt_versions`, `TaskModel.pre_trial_analysis*`, `analyzer_blocks` version
  columns
- `oddish/src/oddish/core/pre_trial_sync.py` (new),
  `oddish/src/oddish/workers/queue/qa_handler.py`,
  `workers/queue/analysis_handler.py`
- `oddish/src/oddish/worker/probe_analysis.py` (trajectory parser reuse)
- `oddish/src/oddish/cli/prompt.py` (new), `cli/__init__.py`
- `backend/api/services/blocks/analyzer/pre_trial/…` (new),
  `analyzer/analyzer_block.py` (AnalyzerType), `analyzer_llm_client.py` /
  sandbox provisioning
- `backend/api/routers/prompts.py` (new), `backend/api/app.py`,
  `backend/worker/…` (pre_trial_synth wiring, gated setting)
- `frontend/src/components/renderers/code-highlight.tsx`,
  `components/task-files-panel.tsx`,
  `app/(app)/tasks/[task_id]/task-detail-client.tsx`, new action-items component
