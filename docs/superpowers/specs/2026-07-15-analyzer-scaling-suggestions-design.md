# Analyzer report: scaling suggestions

**Date:** 2026-07-15
**Status:** Implemented

## Problem

The report's headroom section had a one-line brief:

> based on the good failures, where is the most capability headroom?

It answered vaguely — but that was not a wording problem. `build_reduce_prompt`
rendered each finding as quote + root_cause + headroom_signal and **dropped `task_path`
and `model` on the floor**, though `Finding` carries both. The model writing the report
saw a pile of anonymous quotes, so it could not name a task or a model even if asked.

Readers want the next question answered: **given where models fail, what should we build
next?** That requires naming tasks.

## What shipped

Three commits, no schema change.

1. **Prompt enrichment** (`ef65ace5`) — the reduce prompt now renders `task_path` and
   `model` per finding, plus a task roster from `models_by_task`.
2. **The brief** (`0696c0ca`) — `headroom_analysis` now asks where the headroom is *and*
   what to build next, in three grounded subsections: new tasks to farm, harder variants,
   where to spend effort. `core.py` derives and passes the roster.
3. **The live path** (`e0c028f4`) — the sandbox cohort path gets the same roster.

### The roster is the load-bearing input

`models_by_task` records which models **passed** a task. Findings record only failures, so
prioritization is underivable without it:

| Roster signal | Reading |
|---|---|
| every model failed | too hard — no gradient, deprioritize |
| every model passed | saturated — stop farming this shape |
| stronger passed, weaker failed | live signal — scale this shape |

`None` means no roster was persisted (pre-`analyzers_006`) and must not collapse into the
empty dict, which is the real answer "no trials".

### Where it lands

Output goes to the **existing `headroom_analysis` column**, which the frontend already
renders. No fifth section, no migration, no API field, no frontend change.

### Both reduce paths

`analyzer_sandbox_enabled` defaults to `True`, so the **cohort/sandbox path is what
actually runs** — the API path is the fallback. Both had to be fed:

- API path: `{roster_block}` in `reduce.txt`; roster derived in `core.py` from
  `inputs.bundles` (every trial gets a bundle, passers included as stubs, and bundles
  carry `model`).
- Sandbox path: roster threaded `rows → sandbox_eval_rows → run_cohort →
  build_reduce_only_prompt`, rendered **only for the good cohort** — it alone owns
  `headroom_analysis`, and the bad cohort would get a block it cannot use.

`models_by_task_from_rows` lives in `core/analyzer_inputs.py` so the handler and sandbox
share one derivation. `core.py` keeps a bundles-based twin (different input, same
semantics) because the pure core cannot import from `workers/queue`.

## Non-goals

- A fifth report section, a column, or any frontend change — the content lands in the
  section that already exists.
- Charts. `GET /reports/{id}/rollup` already returns per-model and per-task counts and the
  frontend never calls it; `recharts` is already a dependency. Deferred, not rejected.

## Decisions

- **Suggestions are uncapped.** A cap chosen before seeing real output is guesswork, and a
  wall of text is cheaper to fix than a silently dropped long tail.
- **Known collision, accepted.** `feat/capability-taxonomy` (complete, unmerged) also
  rewrites `build_reduce_prompt` (required `taxonomy` param) and the same findings block,
  and its `captax_001` is a sibling of `analyzers_006`. Whoever merges second audits every
  `build_*_prompt` call site.

## Verification

- oddish: 10 failed / 2164 passed — the identical failing-test set reproduces at the fork
  point `68d25303`. Zero regressions.
- backend: 5 failed / 588 passed (dummy `ANTHROPIC_API_KEY`) — same set at the fork point.
- Rendered a real reduce prompt on both paths and read it: roster present, `task:`/`model:`
  per finding, three subsections, no unreplaced placeholders, bad cohort roster-free.
