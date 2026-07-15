# Dynamic capability taxonomy

## Problem

The capability taxonomy used by the good-failure half of the analyzer is
hardcoded in four places:

- `oddish/src/oddish/evals/analyzer/prompts/fragments/map_rubric.txt` — the seed
  rubric shown to the map agent (`3a` problem identification, `3b`
  implementation, `3c` syntax, `emergent:<label>`)
- `oddish/src/oddish/evals/analyzer/bucketing.py:SUBCATEGORY_OF` — the
  deterministic `Subtype` → code map
- `oddish/src/oddish/evals/analyzer/prompts/sections/universal_capabilities_content.txt`
  — instructs reduce to organize under 3a/3b/3c
- `oddish/src/oddish/evals/analyzer/schemas.py:Finding.subcategory`

Three consequences:

1. Changing the taxonomy requires a code change and a deploy.
2. `3a`/`3b`/`3c` is too coarse. "Hypothesis fixation" and "narrow evidence
   gathering" are distinct capability gaps that both collapse into `3a`.
3. The `emergent:<label>` escape hatch produces a bare string with no
   description, no example, and no category. Emergent labels are discarded
   rather than accumulated, so the taxonomy never learns.

## Goals

- Taxonomy lives in Postgres; editable without a deploy.
- Seed it with 15 capabilities across 5 failure categories (below).
- Agents author complete new capabilities, which a human promotes.
- Reduce organizes good failures by category and by capability.
- Existing deterministic breakdown does not regress.

## Non-goals

- Per-org taxonomies. The taxonomy is global.
- Agent-authored *categories*. Categories are curated by hand.
- Replacing the bad bucket's `1a`/`1b`. Untouched.
- Automatic dedup/merge of near-duplicate capabilities. Manual for now.

## Key decisions

### The evolving enum is a table, not an enum

`Subtype` and `Classification` stay hardcoded Python enums — the classifier's
contract is fixed. Capabilities are the part that must evolve, so they become
rows. Adding a capability is an INSERT, not a migration.

### Capabilities are orthogonal to `subcategory`, not a replacement

`bucketing.py` derives `breakdown` deterministically from the classifier's
`Subtype`, explicitly *not* from the LLM's label. `schemas.py` states the rule:

> Classifier facts, carried so the rollup can derive lanes host-side instead of
> trusting the LLM-assigned bucket/subcategory above.

The new capabilities are finer-grained than `Subtype` and therefore cannot be
derived host-side — only the map agent, reading the trajectory, can distinguish
`hypothesis-fixation` from `narrow-evidence-gathering` (both are `Wrong
Approach` → `3a` today).

So `capability_slug` is added as a **new, LLM-assigned field** and
`subcategory` keeps its deterministic derivation. The trustworthy coarse
breakdown survives as a cross-check, and disagreement between the two is
visible and measurable rather than silently resolved.

### Agents propose; humans promote

The agent authors a complete capability record — name, description, example
with `trajectory_link`, category tags — into `capability_proposals` with status
`PENDING`. Nothing enters the live rubric without review.

`parse_cohort_result` distrusts the model wherever a host fact exists to
override it (`bucket`, `trajectory_link`, classifier columns). A proposal has
no host fact to check against; its content is model-authored by definition.
Human review is what stands in for the host-fact override.

### Each run snapshots the taxonomy it used

`AnalyzerEvalConfig.taxonomy_version` already exists and is unread. Without a
snapshot, editing a capability silently rewrites the meaning of every
historical breakdown. Each analyzer row stores the taxonomy it ran against.

## Data model

Four tables, global (no `org_id`), in `oddish/src/oddish/db/models.py`.
Migration follows `analyzers_001`'s autocommit/idempotent style.

### `capability_categories`

Curated only; seeded with 5.

| column | type | notes |
|---|---|---|
| `slug` | VARCHAR(64) PK | `verification`, `perception`, … |
| `name` | VARCHAR(255) | display |
| `description` | TEXT | |
| `sort_order` | INTEGER | render order |
| `created_at` / `updated_at` | TIMESTAMPTZ | |
| `deleted_at` | TIMESTAMPTZ | soft delete |

### `capabilities`

The live taxonomy; seeded with 15.

| column | type | notes |
|---|---|---|
| `slug` | VARCHAR(64) PK | `agent-early-stop` |
| `name` | VARCHAR(255) | "Agent Early Stop" |
| `description` | TEXT | the failure mode |
| `example` | TEXT | observed instances |
| `created_at` / `updated_at` | TIMESTAMPTZ | |
| `deleted_at` | TIMESTAMPTZ | soft delete |

`slug` is the PK rather than a surrogate integer id: findings reference
capabilities by slug and the LLM emits slugs, so an integer would force a
lookup the agent cannot perform. **Slugs are immutable once promoted** — `name`
is the editable display field. Renaming a slug would orphan historical findings.

### `capability_category_tags`

The many-to-many. Makes "Failure Categories" a list rather than a column.

| column | type | notes |
|---|---|---|
| `capability_slug` | VARCHAR(64) | FK → `capabilities.slug` |
| `category_slug` | VARCHAR(64) | FK → `capability_categories.slug` |
| `is_primary` | BOOLEAN | exactly one true per capability |

PK `(capability_slug, category_slug)`. Partial unique index on
`(capability_slug) WHERE is_primary` enforces exactly one primary.

`is_primary` exists because category rollup and multi-tag are in tension: a
capability tagged both `verification` and `long-horizon` would be counted twice
under a category grouping, and category totals would exceed
`num_good_failures`. Rollup groups by the primary tag, so counts stay an exact
partition. Non-primary tags remain for filtering and render as
cross-references in the reduce output.

### `capability_proposals`

The agent's authoring surface.

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(64) PK | |
| `slug_suggestion` | VARCHAR(64) | dedup key within a job |
| `name` | VARCHAR(255) | |
| `description` | TEXT | |
| `example` | TEXT | |
| `category_slugs` | JSONB | list of strings |
| `analyzer_id` | VARCHAR(64) | FK → `analyzers.id` |
| `trial_ids` | JSONB | every trial that triggered it |
| `trajectory_link` | TEXT | first citing trial |
| `status` | VARCHAR(16) | `PENDING` \| `PROMOTED` \| `REJECTED` |
| `promoted_capability_slug` | VARCHAR(64) | target on promote, or merge target on reject |
| `created_at` / `reviewed_at` | TIMESTAMPTZ | |
| `reviewed_by` | VARCHAR(64) | |

`category_slugs` is JSONB here but a join table on `capabilities` because a
proposal is a draft that may name categories which do not exist. An FK would
reject exactly the rows most worth seeing. It hardens into join-table rows at
promotion.

### `analyzers` (altered)

| column | type | notes |
|---|---|---|
| `taxonomy_version` | VARCHAR(64) | short content hash of the snapshot |
| `taxonomy_snapshot` | JSONB | capabilities + tags as rendered into the rubric |

### `Finding` (altered)

```python
capability_slug: str | None = None   # LLM-assigned, plain text — NOT FK'd
```

`Finding` gains **only** this field. The agent's JSON also carries an optional
`capability_proposal` object, but that is not a per-trial fact and does not
belong on a per-trial record — `parse_cohort_result` lifts it out into a
separate `proposals` list. Storing it on `Finding` would duplicate one proposal
across every trial that cited it and reintroduce the dedup problem downstream.

`capability_slug` is deliberately **not** a foreign key. When the agent proposes
`hypothesis-fixation` and self-assigns that slug, no such row exists yet — an FK
would reject the write. Because the slug is free text, promoting a proposal
makes every historical finding that cited it **resolve** against the live
taxonomy, with no backfill.

**Scope of that claim (corrected against the code).** Findings are stored as
JSONB on `analyzers.findings`, and the reduce output is frozen markdown in
`universal_capabilities_content`. So promotion:

- **does** make any *new* aggregate query joining `finding.capability_slug` →
  `capabilities.slug` resolve historical rows under the promoted capability;
- **does not** re-render already-generated analyzer reports. Past
  `universal_capabilities_content` still shows the capability under its
  `Proposed capabilities` heading until that analyzer is re-run.

Re-rendering past reports would mean re-running reduce, which is out of scope.

Rejection is the mirror case: `promoted_capability_slug` doubles as a merge
target, so rejecting `early-stop` *into* `agent-early-stop` makes orphaned
findings resolve to the survivor. A reject with no merge target leaves findings
pointing at a dead slug; the rollup counts those as `unclassified`.

## Flow

1. **Host** (`analyzer_cohort.py`'s caller) reads `capabilities` + tags →
   `Taxonomy` → snapshots it onto the analyzer row → renders the rubric into
   the map prompt.
2. **Agent** classifies each trial against the rubric. If nothing fits, it
   authors a full proposal inline and self-assigns the proposed slug.
3. **Host** parses `findings.jsonl` at job end, writes `PENDING` proposals.
4. **Human** reviews and promotes or merges.
5. Next run's rubric includes the promoted capability.

### Prompt assembly stays pure

`prompt_builder.py`'s docstring promises no I/O beyond packaged template reads.
The taxonomy is fetched by the caller and passed down:

```python
def map_rubric(taxonomy: Taxonomy) -> str:   # pure: renders, does not fetch
```

`map_rubric.txt` becomes a template with the capability list interpolated,
grouped by category.

### Dedup at the end-of-job write

Every MAP batch runs in a **fresh claude process** (`claude_session_id=None` —
deliberately, it is what keeps context `O(batch)` instead of `O(cohort)`).
Batch 3 has no memory that batch 1 already proposed `hypothesis-fixation`, so
**the same capability will be proposed repeatedly.**

The write dedups by `slug_suggestion` scoped to `analyzer_id`: first proposal
for a slug wins; later ones append their `trial_id` to `trial_ids`. Blind
insert would produce one row per batch.

### Write location

`run_cohort`'s docstring makes a point of *"nothing here reads S3"* — it takes
no DB or storage handle. `parse_cohort_result` gains a third return element
(`proposals`), and the DB write happens one level up in
`backend/worker/analyzer_sandbox.py`, where findings are already persisted.

### Proposals are good-bucket-only

Gated structurally on `bucket == "good"`, mirroring how `_cohort_block` gates
`oracle_context` on `bucket == "bad"`:

> Oracle context is bad-bucket-only; gate structurally so a caller mistake can't
> leak it into a good-cohort prompt.

Same reasoning inverted: the bad bucket classifies task defects, not agent
capabilities, so it must be unable to propose one even if the prompt drifts.

## Reduce output

`universal_capabilities_content.txt` is rewritten to organize good failures by
category (the 5 headings, ordered by `sort_order`), with capabilities nested
under their **primary** category. Each capability cites its findings by
`trajectory_link`. Non-primary tags render as cross-references
("also: long-horizon"). Findings whose `capability_slug` resolves to no live
row are grouped under a trailing `Proposed capabilities` heading.

## Seed taxonomy

Categories: `verification`, `perception`, `tool`, `world-knowledge`,
`long-horizon`.

Capabilities seed with a primary tag only; cross-tags are a curation action.

### verification — Verification failures

| slug | name | description | example |
|---|---|---|---|
| `agent-early-stop` | Agent Early Stop | The agent stops early and assumes it's done; declares done without checking outputs against reality. | apex-swe: models patch the fix then issue a solve without verifying. DevOps-Gym: models reach premature conclusions. |
| `hypothesis-fixation` | Hypothesis Fixation | The model fixates on its initial approach or first plausible cause, generating no competing hypotheses and discarding contradicting evidence. | SREGym "greedy approach"; valkey `requirepass` case; gc/GOGC case; APEX "single-source reliance." |
| `specification-non-compliance` | Specification Non-Compliance | Fixates on the natural-language description instead of extracting hard constraints. | APEX "Specification Non-Compliance" 14%; DevOps-Gym Maven migration: "253 tests completed, 88 failed." |

### perception — Perception failures (signal is present, context is mismanaged)

| slug | name | description | example |
|---|---|---|---|
| `volume-mismanagement` | Volume Mismanagement | Truncation ignored, unfiltered queries, context exhaustion, lossy overflow summarization. | APEX "Bad Context Handling" 38% (DeepSeek 16,515-char one-shot); DevOps-Gym context-exhaustion case. |
| `temporal-mismanagement` | Temporal Mismanagement | Cannot sustain attention on an evolving stream; wrong sampling policy. | DevOps-Gym "inadequate monitoring methodology" 37%, "insufficient temporal granularity" 11%; distraction onto past observations. |
| `narrow-evidence-gathering` | Narrow Evidence Gathering | Single-source diagnosis, missing triangulation. | APEX single-source (6%); SREGym noise-induced tunnel vision. |

### tool — Tool failures (wrong tools, or the tool is OOD)

| slug | name | description | example |
|---|---|---|---|
| `tool-selection-error` | Tool Selection Error | Picks the familiar-but-wrong tool over the correct provided one. | APEX agents pick HTTP curl over MCP. |
| `brittle-tool-mechanics` | Brittle Tool Mechanics | Malformed calls, argument-format errors, retry cascades that burn the budget. | APEX "Execution Failure" 12%/16%; Grok 4 `str_replace` (>⅓ fail); DeepSeek `view_file` (~½ fail); SREGym "Infrastructure Failure" 18%. |
| `ood-tool-unfamiliarity` | OOD Tool Unfamiliarity | Lacks the causal model to even look in the right place. | — |

### world-knowledge — World knowledge gaps

| slug | name | description | example |
|---|---|---|---|
| `cross-layer-reasoning-gap` | Cross-Layer Reasoning Gap | Can't connect hardware/OS/control-plane to application symptoms. | SREGym hardware faults (0% characterization); control-plane-vs-data-plane confusion. |
| `coupled-fault-reasoning` | Coupled / Compound-Fault Reasoning | Can't reason about interacting causes. | SREGym metastable (trigger × constraint); concurrent/correlated failures. |
| `non-python-degradation` | Non-Python Degradation | Outside Python, the agent performs poorly on agentic tasks. | DevOps-Gym Java/Go collapse (70.4% → 23.87%); APEX Java/C++ 0%. |

### long-horizon — Long-horizon failures

| slug | name | description | example |
|---|---|---|---|
| `multi-step-planning-collapse` | Multi-Step Planning Collapse | Fixes one error, loses track of the rest, terminates early. | DevOps-Gym "multi-step reasoning" 23%; APEX "stalling without progress." |
| `cross-stage-context-propagation` | Cross-Stage Context Propagation | Can't carry context downstream to multi-stage faults. | DevOps-Gym E2E 0%. |
| `implementation-breakdown` | Implementation Breakdown | Compile/import/syntax errors block an otherwise-correct solution. | APEX Observability "Execution Failure" (import/compile/syntax); DevOps-Gym compiled-language difficulty. |

## Testing

- `bucketing.py` unchanged → existing `test_analyzer_*` suites must pass
  untouched. This is the regression gate for the orthogonality decision.
- `map_rubric(taxonomy)` renders a known taxonomy to expected text (pure).
- `parse_cohort_result` extracts proposals; findings without
  `capability_proposal` yield none.
- Proposal dedup: the same slug across three batches yields one row with three
  `trial_ids`.
- Bad-bucket findings carrying a `capability_proposal` are dropped, not written.
- Promotion resolves historical findings: a finding citing a proposed slug joins
  against the promoted capability, with no finding row rewritten. (Data-level
  only — already-rendered reduce markdown is not re-generated.)
- Migration is idempotent (re-runnable).

## Known costs

**The rubric grows with every promotion.** At 15 capabilities it is a
comfortable prompt. At ~60 it becomes a wall of text that degrades
classification, and the agent will reach for proposals again because it cannot
hold the list. Not designed for now (YAGNI); `taxonomy_snapshot` is what will
let this be measured when it bites.

**Capability assignment is LLM-trusted.** This is a real departure from the
invariant `bucketing.py` was built around. It is unavoidable — the distinction
is not derivable from `Subtype` — but it means capability counts are
softer evidence than `breakdown` counts. Keeping both is what makes the
softness visible.
