# Analyzer report: scaling-suggestions section

**Date:** 2026-07-15
**Status:** Design — awaiting review

## Problem

The analyzer report ends at `headroom_analysis`, whose entire brief is:

> based on the good failures, where is the most capability headroom?

It reads vague, and the vagueness is not a prompt-wording problem. `build_reduce_prompt`
(`oddish/src/oddish/evals/analyzer/prompt_builder.py:91`) renders each finding as:

```
- [good/3a] trial=<id> link=<url>
  quote: <evidence_quote>
  root_cause: <root_cause>
  headroom_signal: <headroom_signal>
```

`Finding` also carries `task_id`, `task_path`, and `model`, and `AnalyzerEvalOutput`
carries `models_by_task` (which models ran each task, **including those that passed**).
None of it reaches the prompt. The synthesizing model sees a pile of anonymous quotes,
so it cannot say which *task shape* produced signal or which *model* failed — only that
"models struggle with implementation."

Readers want the next question answered: **given where models fail, what should we build
next?** That requires naming tasks and models.

## Goals

Add a fifth section, `scaling_suggestions`, at the bottom of the report, covering three
things in one section:

1. **New tasks to farm** — task shapes worth authoring, anchored to observed failures
2. **Harder variants** — specific ways to push tasks that already produce good failures
3. **Prioritization** — which categories are live signal vs. saturated vs. too hard

Every claim cites a `task_path` and a trajectory link, per the report's existing
citation contract.

## Non-goals

- Rewriting `headroom_analysis`. It keeps its brief and gets sharper for free from the
  enrichment below. Changing both at once would make it impossible to attribute the
  improvement. Revisit after we see the enriched output.
- Charts / structured section data. The sections stay markdown. (Considered and dropped:
  `GET /reports/{id}/rollup` already returns per-model and per-task counts and the
  frontend never calls it — a real opportunity, but out of scope here.)
- Changing the taxonomy or the map stage.

## Design

### 1. Prompt enrichment (the load-bearing part)

Without this the new section is as vague as the one it sits below, so it ships in the
same change.

- `_findings_block` gains `task_path` and `model` per finding.
- New `_roster_block(models_by_task)`: a compact `task_path -> [models that ran it]`
  dump. This is the only source for **passes**; findings record failures only, so
  "saturated" and "too hard" are underivable without it.
- `build_reduce_prompt(findings, counts, models_by_task)` — new third argument.

The roster is what makes prioritization evidence-based rather than vibes:

| Roster signal | Reading |
|---|---|
| every model failed the task | too hard — no gradient, deprioritize |
| every model passed | saturated — stop farming this shape |
| stronger model passed, weaker failed | live signal — scale this shape |

### 2. The section brief

New file `oddish/src/oddish/evals/analyzer/prompts/sections/scaling_suggestions.txt`.
Unlike the existing one-line briefs, it is structured — three labeled subsections
matching the three goals, each required to cite `task_path` + trajectory link, and
explicitly forbidden from proposing domains with no evidence in the findings.

Registration:
- `SECTION_KEYS` += `"scaling_suggestions"` (append — order drives `sections_block`)
- `SECTION_KEYS_BY_BUCKET["good"]` += `"scaling_suggestions"` (derived from good
  failures, so per-cohort runs need no cross-cohort synthesis)

### 3. Both reduce paths

- **Cohort/sandbox path** (`backend/api/services/cc_chat/analyzer_prompt.py:169`,
  `analyzer_parse.py:77`) — already generic: derives its section list and JSON output
  keys from `SECTION_KEYS_BY_BUCKET`. **No change needed.**
- **API path** (`prompts/reduce.txt`) — hardcodes "Write four markdown sections" and a
  literal output-JSON object. Make both derive from `SECTION_KEYS` so the count and keys
  can't drift from the registry again.

### 4. Plumbing

| Layer | File | Change |
|---|---|---|
| sections dict | `evals/analyzer/core.py:280` | map `"scaling"` → `scaling_suggestions` |
| sandbox map | `backend/worker/analyzer_sandbox.py:42` | same mapping |
| handler | `workers/queue/analyzer_handler.py:344` | assign `output.sections["scaling"]` |
| DB | `db/models.py:552` | `scaling_suggestions: Mapped[str \| None]`, Text, nullable |
| migration | `alembic/versions/analyzers_007_add_scaling_suggestions.py` | add column |
| core query | `core/analyzers.py:148` | add to `load_only` |
| response | `schemas.py:1829` | `scaling_suggestions: str \| None = None` |
| API type | `frontend/src/lib/types.ts:1023` | `scaling_suggestions?: string \| null` |
| render | `analyzers/[report]/report-detail-client.tsx:127` | `<Section>` after headroom |

Nullable column + optional field: existing reports render "No findings for this section."
via the current `Section` fallback. No backfill.

### 5. Cost

The reduce prompt grows by roughly two short fields per finding plus the roster.
`AnalyzerEvalConfig.token_budget` is 6000. Watch it on the first real run against a large
cohort; do not pre-optimize.

## Testing

- `tests/evals/analyzer/test_prompt_builder.py` — findings block contains `task_path` and
  `model`; roster block renders; `models_by_task=None` degrades without raising.
- `tests/evals/test_section_fragments.py` — the new brief file exists and is non-empty for
  every key in `SECTION_KEYS`.
- `tests/cc_chat/test_analyzer_parse.py` — `_sections_from` accepts a good-bucket payload
  carrying `scaling_suggestions`, and still raises when all values are blank.
- `tests/db/test_analyzer_model.py` — column round-trips; `None` for legacy rows.
- Migration: upgrade/downgrade against a seeded row.

## Decisions

- **Suggestion count is uncapped.** A cap chosen before seeing real output would be
  guesswork, and the risk it guards against (a wall of text) is cheaper to fix than the
  risk it creates (silently dropping the long tail, which is where a novel task idea is
  most likely to live). Revisit once we've read output from a real cohort.
