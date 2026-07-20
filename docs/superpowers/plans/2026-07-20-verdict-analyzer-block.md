# Verdict via AnalyzerBlock — Revised Plan (port onto upstream primitive)

**Goal:** Run task-verdict synthesis through the existing `AnalyzerBlock`
primitive behind a switch, with byte-identical stored payloads to the legacy
path.

**Why this plan replaces `2026-07-19-analyzer-box-foundation.md`:** that plan
was written against a stale working-tree copy of `analyzer_block.py`. The same
primitive had already landed on `main` (#768), moved to
`backend/api/services/blocks/`, gained a shared `Block` base (#790), and had the
sandbox cohort migrated onto it (#783). Its Tasks 1 and 2 are therefore already
done upstream, and its Task 6 violated a documented package boundary.

## What upstream already provides

- `backend/api/services/blocks/analyzer/analyzer_block.py` — `AnalyzerBlock`,
  keyword-only `__init__`, cancellation-safe shielded persist, and
  **`output_transform: Callable[[str], Any]`** (the old plan's `parse_output`).
- `backend/api/services/blocks/block.py` — `Block` base: `sections()` +
  `output_schema` are the subclass contract; `build_prompt()` and
  `parse()` are concrete; `BlockParseError` on unparseable output.
- `oddish/src/oddish/db/models.py` `AnalyzerBlockModel` + migration
  `analyzers_007`. **Do not add a model or a migration.**
- `AnalyzerType` in `analyzer_block.py` — a new type is one enum member; `type`
  is a plain `String(64)`, so no migration is needed for a new member.

## Already landed on this branch (cherry-picked, tests green)

- `oddish.analyze.build_verdict_prompt` — extracted so both paths build one prompt.
- `oddish.core.verdict_sync.build_verdict_payload` / `sync_verdict_to_task` —
  the single writer of a synthesized verdict; counts always recomputed
  host-side from classifications.

## Global Constraints

- **`oddish` must not import from `backend/`** (`AGENTS.md:188`). The
  block-based synthesis lives in `backend/` and is injected into `oddish`
  through a seam, never imported by it.
- `VERDICT_MODEL = "gpt-5.4"` unchanged. Both paths reach the same model, the
  same token cap (`VERDICT_MAX_TOKENS = 4096`), and the same timeout.
- The four verdict counts are always recomputed host-side by
  `build_verdict_payload`. Never from a model.
- The switch defaults to **off**.
- Upstream's block clients are **async** and Anthropic-only. The verdict judge
  is OpenAI/Azure, so an OpenAI client must be added to the block layer.
- `AnalyzerBlock`'s `model=` argument only sets `block_metadata`; it is **not**
  forwarded to `create_llm_client`. To control the model, inject a client.
- An **injected** client is never closed by the block — the caller must close it.
- Backend tests: `cd backend && uv run pytest`. Core: `cd oddish && uv run pytest`.
  A fresh worktree needs `uv sync --all-extras` in each package.

## Known-red baseline (also fails on `origin/main`)

11 `oddish` failures, 13 `backend` failures. Do not chase or count them.

---

### Task A: OpenAI/Azure `AnalyzerLLMClient`

**Files:** `backend/api/services/blocks/analyzer/analyzer_llm_client.py`;
test `backend/tests/test_analyzer_llm_client.py`.

Add `LLMClientType.OPENAI = "OpenAi"` and an `OpenAIAnalyzerLLMClient`
implementing the existing protocol — `stream(prompt, *, system_prompt=None)`
and `aclose()` — streaming `content.delta` events from
`beta.chat.completions.stream`. It must accept `response_format` and
`max_tokens`, and resolve provider/deployment exactly as the sync
`_build_verdict_openai_client` in `oddish/src/oddish/analyze/classifier.py`
does, including its public-OpenAI `warnings.warn`.

Construction must never require live credentials in tests: resolve the client
through a module-level builder function the tests can patch.

Wire `LLMClientType.OPENAI` into `create_llm_client`, whose signature is
already `(llm_client_type, *, model=None, api_key=None)`. Do **not** hardcode
verdict-specific defaults into that generic factory — a caller that asks for
OPENAI without a model should get a clear error, not a silent verdict default.

**Done when:** the new client streams only content deltas, forwards
`response_format` and `max_tokens` when set and omits them when not, and the
public-OpenAI warning is asserted by a test.

---

### Task B: the synthesis seam in `oddish`

**Files:** `oddish/src/oddish/workers/queue/qa_handler.py`; test
`oddish/tests/test_task_qa_pipeline.py`.

`run_task_qa_job` currently calls `compute_task_verdict` inline. Introduce a
seam mirroring `analyzer_handler.py:228`'s `eval_rows_fn: EvalRowsFn =
default_eval_rows`:

- a `VerdictSynthFn` type alias,
- a `default_verdict_synth(...)` wrapping today's `compute_task_verdict` call,
- a `verdict_synth_fn: VerdictSynthFn = default_verdict_synth` parameter.

The seam's return value feeds `build_verdict_payload`, so both implementations
converge on one writer. Pass `baseline`, `quality_check_passed`, and `timeout`
through the seam rather than letting each implementation re-derive them — that
is what keeps the two paths from silently diverging.

`oddish` gains no `backend` import. The default path must be byte-identical to
today's behavior.

**Done when:** the default path is unchanged and a test drives the seam with a
stub to prove the injection point works.

---

### Task C: `VerdictBlock`, registration, and the parity test

**Files:** `backend/api/services/blocks/analyzer/verdict/{__init__,verdict_block,verdict_prompts}.py`;
`backend/worker/` registration; tests
`backend/tests/test_verdict_block.py`, `backend/tests/test_verdict_parity.py`.

Follow `trajectory/` as the worked example: a `Block` subclass owning
`output_schema` and `sections()`, prompts in a sibling `*_prompts.py`, and a
`to_*` adapter passed to `AnalyzerBlock(output_transform=...)`. Reuse
`oddish.analyze.build_verdict_prompt` for the prompt body rather than
re-authoring it — the shared prompt is what makes parity meaningful.

Add `AnalyzerType.TASK_VERDICT = "task_verdict"`. Inject an
`OpenAIAnalyzerLLMClient` configured with `TaskVerdictModel` and
`VERDICT_MAX_TOKENS`, and close it in a `finally` (the block will not).

Register a backend verdict-synth implementation over the Task B seam, gated on
a new `settings.verdict_via_analyzer_block` (default **False**), following
`backend/worker/analyzer_sandbox.py:202-206` — gate *registration*, not
internals, so unsetting the flag reverts to the core path with no branching.

**The parity test is the point of the plan.** Assert both synth implementations
produce identical `build_verdict_payload` output from the same classifications,
using a fixture whose four counts are mutually distinguishable (not all 1) and
where the legacy-side object carries a garbage count that must be recomputed
away.

**Done when:** the flag is off by default, registration flips the path, the
parity payloads match, and a test proves the block's wiring — `output_transform`,
`response_format`, and `max_tokens` — rather than only its return value.

---

## Carried-forward lessons (from the superseded branch's reviews)

- `sa.Enum(..., create_type=False)` silently drops the flag; only the
  postgresql dialect `ENUM` honors it. Upstream's `AnalyzerBlockModel` already
  uses `PGEnum` correctly.
- A test that only asserts a return value will not catch dropped wiring. Prove
  each guard by breaking the thing it protects and confirming the failure.
- Run `ruff format`/`ruff check` scoped to changed files only.
