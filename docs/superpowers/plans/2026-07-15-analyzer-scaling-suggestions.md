# Analyzer Scaling-Suggestions Section — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fifth analyzer-report section, `scaling_suggestions`, that recommends what tasks to build next based on the good failures — grounded in the specific tasks and models the reduce prompt currently cannot see.

**Architecture:** Two halves. First, enrich the reduce prompt: render `task_path` and `model` per finding, and add a task roster from `models_by_task` (the only record of which models *passed*). Second, register a new section key that flows through the existing machinery — the cohort/sandbox path derives its sections and JSON keys from `SECTION_KEYS_BY_BUCKET` and needs no changes; only the API path's `reduce.txt` hardcodes them, and we make it derive too.

**Tech Stack:** Python 3.11, SQLAlchemy + Alembic (Postgres), pytest, FastAPI/Pydantic, Next.js + TypeScript.

**Spec:** `docs/superpowers/specs/2026-07-15-analyzer-scaling-suggestions-design.md`

## Global Constraints

- Never commit to `main` (repo CLAUDE.md). Work on branch `analyzer-scaling-suggestions`, which already exists and holds the spec.
- Run Python tests from `oddish/`: `cd oddish && pytest`.
- Every report section is markdown. Every claim in a section cites a trajectory link verbatim — this is the report's existing contract, stated in `reduce.txt`.
- Section briefs live one-per-file in `oddish/src/oddish/evals/analyzer/prompts/sections/<key>.txt`. The filename must equal the section key.
- `SECTION_KEYS` order drives `sections_block` output order. Append; never reorder.
- New DB columns are nullable with no backfill. Legacy rows render the existing "No findings for this section." fallback.
- Suggestion counts are uncapped (spec decision).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `oddish/src/oddish/evals/analyzer/prompt_builder.py` | Renders reduce prompt blocks; owns the section registry | 1, 2, 4 |
| `oddish/src/oddish/evals/analyzer/prompts/reduce.txt` | API-path reduce template | 1, 2 |
| `oddish/src/oddish/evals/analyzer/prompts/sections/scaling_suggestions.txt` | The new section's brief | 4 |
| `backend/scripts/haiku_sandbox_bad_failures.py` | Ops script; hand-replaces reduce.txt placeholders | 1, 2 |
| `oddish/alembic/versions/analyzers_007_add_scaling_suggestions.py` | Column migration | 3 |
| `oddish/src/oddish/db/models.py` | `AnalyzerModel` column | 3 |
| `oddish/src/oddish/evals/analyzer/core.py` | Maps reduce JSON → short section keys | 4 |
| `backend/worker/analyzer_sandbox.py` | Same mapping, sandbox path | 4 |
| `oddish/src/oddish/workers/queue/analyzer_handler.py` | Persists sections to columns | 4 |
| `oddish/src/oddish/schemas.py` | `ReportResponse` | 5 |
| `oddish/src/oddish/core/analyzers.py` | `load_only` column list | 5 |
| `frontend/src/lib/types.ts` | `Report` type | 5 |
| `frontend/src/app/(app)/analyzers/[report]/report-detail-client.tsx` | Renders the section | 5 |

---

### Task 1: Enrich the reduce prompt with task, model, and roster

The reduce prompt renders findings without `task_path` or `model`, and never sees `models_by_task`. This is why the report cannot name a task. Fix that first — the new section is worthless without it.

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/prompt_builder.py:91-103`
- Modify: `oddish/src/oddish/evals/analyzer/prompts/reduce.txt`
- Modify: `backend/scripts/haiku_sandbox_bad_failures.py:191-197`
- Test: `oddish/tests/evals/analyzer/test_prompt_builder.py`

**Interfaces:**
- Consumes: `Finding` (`evals/analyzer/schemas.py`) — already carries `task_id`, `task_path`, `model`.
- Produces:
  - `task_roster_block(models_by_task: dict[str, list[str]] | None) -> str`
  - `build_reduce_prompt(findings: list[Finding], counts: dict, models_by_task: dict[str, list[str]] | None = None) -> str`
  — the new third parameter is **optional**; existing two-arg callers and tests keep working.

- [ ] **Step 1: Write the failing tests**

Append to `oddish/tests/evals/analyzer/test_prompt_builder.py`:

```python
from oddish.evals.analyzer.prompt_builder import task_roster_block


def _good_finding(**over):
    kw = dict(
        trial_id="t1", bucket="good", subcategory="3a", evidence_quote="q",
        step_ids=[1], root_cause="rc", headroom_signal="hs",
        trajectory_link="/tasks/t1/probe/x", model="claude-opus-4-8",
        task_path="tasks/redis/expiry", task_id="redis-expiry",
    )
    kw.update(over)
    return Finding(**kw)


def test_reduce_findings_block_carries_task_and_model():
    prompt = build_reduce_prompt(
        [_good_finding()], {"trials": 1, "bad": 0, "good": 1}
    )
    assert "task: tasks/redis/expiry" in prompt
    assert "model: claude-opus-4-8" in prompt


def test_reduce_findings_block_falls_back_when_task_path_missing():
    prompt = build_reduce_prompt(
        [_good_finding(task_path=None)], {"trials": 1, "bad": 0, "good": 1}
    )
    assert "task: redis-expiry" in prompt
    assert "task: None" not in prompt


def test_roster_block_lists_every_model_that_ran_including_passers():
    block = task_roster_block(
        {"redis-expiry": ["claude-haiku-4-5", "claude-opus-4-8"]}
    )
    assert "- redis-expiry: claude-haiku-4-5, claude-opus-4-8" in block


def test_roster_block_none_means_unknown_not_empty():
    # None = no roster persisted (pre-analyzers_006). It must not read as
    # "no models ran", which would invert the signal the section depends on.
    block = task_roster_block(None)
    assert "unknown" in block.lower()
    assert block.strip() != ""


def test_roster_block_empty_dict_means_no_trials():
    assert "no trials" in task_roster_block({}).lower()


def test_build_reduce_prompt_embeds_the_roster():
    prompt = build_reduce_prompt(
        [_good_finding()], {"trials": 1, "bad": 0, "good": 1},
        {"redis-expiry": ["claude-opus-4-8"]},
    )
    assert "- redis-expiry: claude-opus-4-8" in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd oddish && pytest tests/evals/analyzer/test_prompt_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'task_roster_block'`

- [ ] **Step 3: Add the block builders**

In `oddish/src/oddish/evals/analyzer/prompt_builder.py`, replace `build_reduce_prompt` (lines 91-103) with:

```python
def _reduce_findings_block(findings: list[Finding]) -> str:
    return "\n".join(
        f"- [{f.bucket}/{f.subcategory}] trial={f.trial_id} link={f.trajectory_link}\n"
        f"  task: {f.task_path or f.task_id or 'unknown'}\n"
        f"  model: {f.model or 'unknown'}\n"
        f"  quote: {f.evidence_quote}\n  root_cause: {f.root_cause}\n"
        f"  headroom_signal: {f.headroom_signal}"
        for f in findings
    )


def task_roster_block(models_by_task: dict[str, list[str]] | None) -> str:
    """Which models RAN each task, including the ones that PASSED. Findings
    record only failures, so this is the sole source for "every model passed" --
    without it, saturated and too-hard are indistinguishable to the synthesizer.

    None means no roster was persisted (pre-analyzers_006); it must not collapse
    into the empty case, which is the real answer "no trials"."""
    if models_by_task is None:
        return "(no roster persisted for this run — pass/fail coverage unknown)"
    if not models_by_task:
        return "(no trials)"
    return "\n".join(
        f"- {task}: {', '.join(sorted(models)) or '(none)'}"
        for task, models in sorted(models_by_task.items())
    )


def build_reduce_prompt(
    findings: list[Finding],
    counts: dict,
    models_by_task: dict[str, list[str]] | None = None,
) -> str:
    return REDUCE_PROMPT_TEMPLATE.format(
        counts_block=json.dumps(counts, indent=2),
        roster_block=task_roster_block(models_by_task),
        findings_block=_reduce_findings_block(findings),
        sections_block=sections_block(SECTION_KEYS),
    )
```

- [ ] **Step 4: Add the roster to the reduce template**

In `oddish/src/oddish/evals/analyzer/prompts/reduce.txt`, insert between the `## Counts` block and `## Findings`:

```
## Task roster — which models RAN each task, including the ones that PASSED
{roster_block}
```

- [ ] **Step 5: Keep the ops script's hand-rolled template in sync**

`backend/scripts/haiku_sandbox_bad_failures.py` replaces `reduce.txt` placeholders by hand, so a new placeholder would survive into the prompt as a literal `{roster_block}`. It analyzes the **bad** cohort, which has no roster, so `None` is the honest value.

In `backend/scripts/haiku_sandbox_bad_failures.py`, update the import at line 59 and the `reduce_template` block at lines 191-197:

```python
    reduce_template = (
        (prompts_dir / "reduce.txt")
        .read_text()
        .replace("{sections_block}", sections_block(SECTION_KEYS))
        .replace("{roster_block}", task_roster_block(None))
        .replace("{{", "{")
        .replace("}}", "}")
    )
```

Add `task_roster_block` to the existing `from oddish.evals.analyzer.prompt_builder import (...)` group at line 59.

- [ ] **Step 6: Run the full analyzer suite**

Run: `cd oddish && pytest tests/evals/ -v`
Expected: PASS. `test_build_reduce_prompt_still_contains_every_brief` in `tests/evals/test_section_fragments.py` still passes — `models_by_task` defaults to `None`.

- [ ] **Step 7: Verify the ops script still renders**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish-present-2/oddish && python -c "
import pathlib, sys
sys.path.insert(0, 'oddish/src')
from oddish.evals.analyzer.prompt_builder import sections_block, task_roster_block, SECTION_KEYS
t = pathlib.Path('oddish/src/oddish/evals/analyzer/prompts/reduce.txt').read_text()
t = t.replace('{sections_block}', sections_block(SECTION_KEYS)).replace('{roster_block}', task_roster_block(None)).replace('{{','{').replace('}}','}')
assert '{roster_block}' not in t, 'placeholder survived'
assert '{sections_block}' not in t, 'placeholder survived'
print('ops-script render OK')
"`
Expected: `ops-script render OK`

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/prompt_builder.py \
        oddish/src/oddish/evals/analyzer/prompts/reduce.txt \
        oddish/tests/evals/analyzer/test_prompt_builder.py \
        backend/scripts/haiku_sandbox_bad_failures.py
git commit -m "feat(analyzer): give the reduce prompt task, model, and roster context"
```

---

### Task 2: Derive reduce.txt's output keys from the registry

`reduce.txt` hardcodes "Write four markdown sections" and a literal output-JSON object. Both drift from `SECTION_KEYS` the moment a section is added. Make them derive — this is a no-op refactor while the count is still four, which is exactly why it lands before the new section.

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/prompt_builder.py`
- Modify: `oddish/src/oddish/evals/analyzer/prompts/reduce.txt`
- Modify: `backend/scripts/haiku_sandbox_bad_failures.py:191-197`
- Test: `oddish/tests/evals/test_section_fragments.py`

**Interfaces:**
- Produces: `output_keys_block(keys: Sequence[str]) -> str` — a JSON object mapping each key to `"...markdown..."`. Mirrors the cohort path's existing `json.dumps({k: "...markdown..." for k in section_keys})` (`backend/api/services/cc_chat/analyzer_prompt.py:170`).

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/evals/test_section_fragments.py`:

```python
from oddish.evals.analyzer.prompt_builder import output_keys_block


def test_output_keys_block_names_every_section_key():
    block = output_keys_block(SECTION_KEYS)
    for key in SECTION_KEYS:
        assert f'"{key}"' in block


def test_reduce_prompt_output_keys_track_the_registry():
    # The template must not carry a hand-written key list that can drift from
    # SECTION_KEYS -- that drift is what this test exists to prevent.
    prompt = build_reduce_prompt([], {"trials": 0, "bad": 0, "good": 0})
    for key in SECTION_KEYS:
        assert f'"{key}"' in prompt
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd oddish && pytest tests/evals/test_section_fragments.py -v`
Expected: FAIL — `ImportError: cannot import name 'output_keys_block'`

- [ ] **Step 3: Add the helper**

In `oddish/src/oddish/evals/analyzer/prompt_builder.py`, add after `sections_block`:

```python
def output_keys_block(keys: Sequence[str]) -> str:
    """The reduce output's JSON shape, derived from the registry so the template
    can't drift from SECTION_KEYS."""
    return json.dumps({k: "...markdown..." for k in keys})
```

Then add `output_keys_block=output_keys_block(SECTION_KEYS)` to the `REDUCE_PROMPT_TEMPLATE.format(...)` call in `build_reduce_prompt`.

- [ ] **Step 4: Update the template**

In `oddish/src/oddish/evals/analyzer/prompts/reduce.txt`, replace the trailing two blocks:

```
## Write these markdown sections:
{sections_block}

## Output — return ONLY JSON with exactly these keys:
{output_keys_block}
```

This removes the last `{{`/`}}`-escaped braces from the file. The wording now matches the cohort path (`analyzer_prompt.py:193`).

- [ ] **Step 5: Keep the ops script in sync**

In `backend/scripts/haiku_sandbox_bad_failures.py`, add the replacement and import `output_keys_block` alongside the others:

```python
    reduce_template = (
        (prompts_dir / "reduce.txt")
        .read_text()
        .replace("{sections_block}", sections_block(SECTION_KEYS))
        .replace("{roster_block}", task_roster_block(None))
        .replace("{output_keys_block}", output_keys_block(SECTION_KEYS))
        .replace("{{", "{")
        .replace("}}", "}")
    )
```

- [ ] **Step 6: Run the tests**

Run: `cd oddish && pytest tests/evals/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/prompt_builder.py \
        oddish/src/oddish/evals/analyzer/prompts/reduce.txt \
        oddish/tests/evals/test_section_fragments.py \
        backend/scripts/haiku_sandbox_bad_failures.py
git commit -m "refactor(analyzer): derive reduce.txt output keys from SECTION_KEYS"
```

---

### Task 3: Add the scaling_suggestions column

Ships before the section is registered, so the section has somewhere to land the moment it exists.

**Files:**
- Modify: `oddish/src/oddish/db/models.py:552`
- Create: `oddish/alembic/versions/analyzers_007_add_scaling_suggestions.py`
- Test: `oddish/tests/db/test_analyzer_model.py`

**Interfaces:**
- Produces: `AnalyzerModel.scaling_suggestions: Mapped[str | None]`

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/db/test_analyzer_model.py` (match the file's existing session/fixture style; if it builds models in-memory without a session, assert on the attribute directly):

```python
def test_scaling_suggestions_defaults_to_none_and_round_trips():
    from oddish.db.models import AnalyzerModel

    a = AnalyzerModel(id="a1", name="r")
    assert a.scaling_suggestions is None

    a.scaling_suggestions = "## New tasks to farm\n- ..."
    assert a.scaling_suggestions.startswith("## New tasks to farm")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd oddish && pytest tests/db/test_analyzer_model.py -v`
Expected: FAIL — `AttributeError` / `TypeError: 'scaling_suggestions' is an invalid keyword argument`

- [ ] **Step 3: Add the column**

In `oddish/src/oddish/db/models.py`, after line 552 (`headroom_analysis`):

```python
    scaling_suggestions: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Write the migration**

Create `oddish/alembic/versions/analyzers_007_add_scaling_suggestions.py`:

```python
"""add analyzers.scaling_suggestions

The report's fifth section: what tasks to build next, derived from the good
failures. Nullable with no backfill -- pre-existing analyzers render the
frontend's "No findings for this section." fallback.

Nullable and un-indexed: a plain ADD COLUMN takes no FK lock, so it cannot
deadlock. ``000_initial_schema`` runs ``create_all()``, so on a fresh DB the
column already exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "analyzers_007"
down_revision: Union[str, Sequence[str], None] = "analyzers_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analyzers",
        sa.Column("scaling_suggestions", sa.Text(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("analyzers", "scaling_suggestions")
```

- [ ] **Step 5: Run the tests**

Run: `cd oddish && pytest tests/db/test_analyzer_model.py -v`
Expected: PASS

- [ ] **Step 6: Verify the migration chain has one head**

Run: `cd oddish && alembic heads`
Expected: a single head — `analyzers_007` (or the repo's own head if other branches exist; there must not be two).

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/db/models.py \
        oddish/alembic/versions/analyzers_007_add_scaling_suggestions.py \
        oddish/tests/db/test_analyzer_model.py
git commit -m "feat(analyzer): add scaling_suggestions column"
```

---

### Task 4: Register the section and wire it through both reduce paths

Everything above exists so this task is small. The cohort/sandbox path needs **no prompt changes** — `analyzer_prompt.py:169` and `analyzer_parse.py:77` both derive from `SECTION_KEYS_BY_BUCKET`.

**Files:**
- Create: `oddish/src/oddish/evals/analyzer/prompts/sections/scaling_suggestions.txt`
- Modify: `oddish/src/oddish/evals/analyzer/prompt_builder.py:19-36`
- Modify: `oddish/src/oddish/evals/analyzer/core.py:279-284`
- Modify: `backend/worker/analyzer_sandbox.py:38-47`
- Modify: `oddish/src/oddish/workers/queue/analyzer_handler.py:344-347`
- Test: `oddish/tests/evals/test_section_fragments.py`, `oddish/tests/evals/analyzer/test_core.py`, `backend/tests/cc_chat/test_analyzer_parse.py`

**Interfaces:**
- Consumes: `output_keys_block` (Task 2), the enriched findings/roster blocks (Task 1), `AnalyzerModel.scaling_suggestions` (Task 3).
- Produces: short section key `"scaling"` in `AnalyzerEvalOutput.sections`; `_roster_from_bundles(bundles) -> dict[str, list[str]]` (private to `core.py`).

- [ ] **Step 1: Write the failing tests**

In `oddish/tests/evals/test_section_fragments.py`, **update** `test_section_keys_order_matches_reduce_txt` (it pins the old four and will otherwise fail):

```python
def test_section_keys_order_matches_reduce_txt():
    assert SECTION_KEYS == (
        "bad_failure_content",
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
        "scaling_suggestions",
    )
```

**Update** `test_sections_block_reassembles_the_original_bytes` — the guarantee it protects (the original four briefs stay byte-for-byte) still matters, so scope it to the original four rather than deleting it:

```python
def test_sections_block_reassembles_the_original_bytes():
    # The original four briefs must stay byte-for-byte; the live API path
    # depends on this prose. Sections added later are appended, never edits.
    assert sections_block(SECTION_KEYS[:4]) == _ORIGINAL_BLOCK
```

Then append:

```python
def test_scaling_suggestions_is_a_good_bucket_section():
    assert "scaling_suggestions" in SECTION_KEYS_BY_BUCKET["good"]
    assert "scaling_suggestions" not in SECTION_KEYS_BY_BUCKET["bad"]


def test_every_section_key_has_a_non_empty_brief():
    for key in SECTION_KEYS:
        assert section_brief(key).strip(), f"{key} has no brief"


def test_scaling_brief_reaches_the_reduce_prompt():
    prompt = build_reduce_prompt([], {"trials": 0, "bad": 0, "good": 0})
    assert "scaling_suggestions" in prompt
```

In `backend/tests/cc_chat/test_analyzer_parse.py`, append (match the file's existing helper style for building a reduce payload):

```python
def test_good_bucket_sections_include_scaling_suggestions():
    from oddish.evals.analyzer.prompt_builder import SECTION_KEYS_BY_BUCKET

    raw = {k: "body" for k in SECTION_KEYS_BY_BUCKET["good"]}
    sections = _sections_from(raw, "good")
    assert sections["scaling_suggestions"] == "body"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd oddish && pytest tests/evals/test_section_fragments.py -v`
Expected: FAIL — `test_section_keys_order_matches_reduce_txt` fails on the missing 5th key; `test_every_section_key_has_a_non_empty_brief` fails with `FileNotFoundError`.

- [ ] **Step 3: Write the brief**

Create `oddish/src/oddish/evals/analyzer/prompts/sections/scaling_suggestions.txt`:

```
- scaling_suggestions: based on the good failures, what to build next. Cite a task
  (the finding's `task`) and embed its trajectory_link for every claim; propose
  nothing you cannot ground in a finding or the task roster. Say "insufficient
  evidence" rather than speculate. Use these three subsections verbatim as h3s:
  ### New tasks to farm
  Task shapes worth authoring, each named concretely and anchored to the specific
  failure it would target. Generalize from a task that produced a good failure —
  do not invent domains absent from the findings.
  ### Harder variants
  For tasks that already produced good failures, concrete ways to raise difficulty
  (remove scaffolding, extend the step count, add an adversarial precondition).
  Name the task and say which observed failure the variant sharpens.
  ### Where to spend effort
  Rank using the task roster, which lists every model that RAN a task including
  those that PASSED: every model failed = too hard, no gradient, deprioritize;
  every model passed = saturated, stop farming it; a stronger model passed where a
  weaker one failed = live signal, scale it. State which bucket each task is in.
```

- [ ] **Step 4: Register the key**

In `oddish/src/oddish/evals/analyzer/prompt_builder.py`, append to `SECTION_KEYS` (line 19-24) and to the `"good"` tuple (line 31-35):

```python
SECTION_KEYS: tuple[str, ...] = (
    "bad_failure_content",
    "good_failure_content",
    "universal_capabilities_content",
    "headroom_analysis",
    "scaling_suggestions",
)

SECTION_KEYS_BY_BUCKET: dict[str, tuple[str, ...]] = {
    "bad": ("bad_failure_content",),
    "good": (
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
        "scaling_suggestions",
    ),
}
```

- [ ] **Step 5: Map the new key on both paths**

In `oddish/src/oddish/evals/analyzer/core.py`, replace the `sections` dict (lines 279-284):

```python
    sections = {
        "bad": sec.get("bad_failure_content", ""),
        "good": sec.get("good_failure_content", ""),
        "capabilities": sec.get("universal_capabilities_content", ""),
        "headroom": sec.get("headroom_analysis", ""),
        "scaling": sec.get("scaling_suggestions", ""),
    }
```

In `backend/worker/analyzer_sandbox.py`, update both constants (lines 38-47):

```python
_EMPTY_SECTIONS = {
    "bad": "", "good": "", "capabilities": "", "headroom": "", "scaling": "",
}

# reduce.txt's sections -> the AnalyzerModel columns _store writes.
_SECTION_COLUMN = {
    "bad_failure_content": "bad",
    "good_failure_content": "good",
    "universal_capabilities_content": "capabilities",
    "headroom_analysis": "headroom",
    "scaling_suggestions": "scaling",
}
```

In `oddish/src/oddish/workers/queue/analyzer_handler.py`, after line 347 (`analyzer.headroom_analysis = ...`):

```python
                analyzer.scaling_suggestions = output.sections["scaling"]
```

- [ ] **Step 6: Derive the roster in core and pass it to the reduce call**

The roster cannot come from the handler: `analyzer_handler.py:303` computes `_models_by_task(rows)` *after* the eval returns, so it does not exist at prompt-build time. Derive it in `core.py` from `inputs.bundles` instead — `build_analyzer_inputs` gives every trial a bundle (failures in full, the rest via `_stub_bundle`) and every bundle carries `task_id` and `model`, so passing trials are already in hand.

Add to `oddish/src/oddish/evals/analyzer/core.py`, above `run_analyzer_eval`:

```python
def _roster_from_bundles(bundles) -> dict[str, list[str]]:
    """task_id -> distinct models that ran it, including trials that PASSED.

    Mirrors analyzer_handler._models_by_task, which derives the same thing from
    rows for persistence and must keep working for eval strategies that never
    build AnalyzerEvalInputs. Same semantics, different input; keep them in step.
    """
    by_task: dict[str, set[str]] = {}
    for b in bundles:
        if b.model:
            by_task.setdefault(b.task_id, set()).add(b.model)
    return {k: sorted(v) for k, v in by_task.items()}
```

Then change line 258:

```python
    reduce_prompt = build_reduce_prompt(
        findings, counts, _roster_from_bundles(inputs.bundles)
    )
```

- [ ] **Step 6b: Test the roster reaches the prompt through core**

Add to `oddish/tests/evals/analyzer/test_core.py` (match the file's existing fixture style for building `AnalyzerEvalInputs`):

```python
def test_roster_from_bundles_includes_passing_trials():
    from oddish.evals.analyzer.core import _roster_from_bundles
    from oddish.evals.primitives import TrajectoryBundle

    def _b(trial_id, task_id, model):
        return TrajectoryBundle(
            trial_id=trial_id, task_id=task_id, task_path=f"tasks/{task_id}",
            agent="claude-code", model=model, reward=None, trajectory=[], logs={},
            trajectory_summary=None, oracle_context=None, trajectory_link="/l",
        )

    roster = _roster_from_bundles([
        _b("t1", "redis-expiry", "claude-opus-4-8"),
        _b("t2", "redis-expiry", "claude-haiku-4-5"),
        _b("t3", "redis-expiry", None),
    ])
    assert roster == {"redis-expiry": ["claude-haiku-4-5", "claude-opus-4-8"]}
```

- [ ] **Step 7: Run the tests**

Run: `cd oddish && pytest tests/evals/ tests/db/ -v && cd ../backend && pytest tests/cc_chat/ -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/prompt_builder.py \
        oddish/src/oddish/evals/analyzer/prompts/sections/scaling_suggestions.txt \
        oddish/src/oddish/evals/analyzer/core.py \
        oddish/src/oddish/workers/queue/analyzer_handler.py \
        oddish/tests/evals/test_section_fragments.py \
        backend/worker/analyzer_sandbox.py \
        backend/tests/cc_chat/test_analyzer_parse.py
git commit -m "feat(analyzer): add scaling_suggestions section to the report"
```

---

### Task 5: Expose and render the section

**Files:**
- Modify: `oddish/src/oddish/schemas.py:1832`
- Modify: `oddish/src/oddish/core/analyzers.py:151`
- Modify: `frontend/src/lib/types.ts:1031`
- Modify: `frontend/src/app/(app)/analyzers/[report]/report-detail-client.tsx:123-127`
- Test: `backend/tests/test_analyzers_router.py`

**Interfaces:**
- Consumes: `AnalyzerModel.scaling_suggestions` (Task 3).
- Produces: `ReportResponse.scaling_suggestions`, `Report.scaling_suggestions`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_analyzers_router.py` (match the file's existing client/seed fixtures):

```python
def test_report_response_exposes_scaling_suggestions():
    from oddish.schemas import ReportResponse

    r = ReportResponse(id="a1", name="r", status="success",
                       scaling_suggestions="## New tasks to farm\n- x")
    assert r.scaling_suggestions.startswith("## New tasks to farm")


def test_report_response_scaling_suggestions_defaults_to_none():
    from oddish.schemas import ReportResponse

    assert ReportResponse(id="a1", name="r", status="success").scaling_suggestions is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && pytest tests/test_analyzers_router.py -v`
Expected: FAIL — `AttributeError: 'ReportResponse' object has no attribute 'scaling_suggestions'`

- [ ] **Step 3: Add the response field**

In `oddish/src/oddish/schemas.py`, after line 1832 (`headroom_analysis`):

```python
    scaling_suggestions: str | None = None
```

- [ ] **Step 4: Add the column to load_only**

`ReportResponse` is built with `from_attributes=True`, so a column missing from `load_only` triggers a lazy-load on a closed async session. In `oddish/src/oddish/core/analyzers.py`, after line 151 (`AnalyzerModel.headroom_analysis,`):

```python
                AnalyzerModel.scaling_suggestions,
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && pytest tests/test_analyzers_router.py -v`
Expected: PASS

- [ ] **Step 6: Add the frontend type**

In `frontend/src/lib/types.ts`, after line 1031 (`headroom_analysis?: string | null;`):

```typescript
  scaling_suggestions?: string | null;
```

- [ ] **Step 7: Render the section**

In `frontend/src/app/(app)/analyzers/[report]/report-detail-client.tsx`, after the closing `/>` of the "Headroom analysis" `<Section>` (line 127):

```tsx
      <Section
        title="Scaling suggestions"
        content={report.scaling_suggestions}
        generating={generating}
      />
```

- [ ] **Step 8: Typecheck the frontend**

Run: `cd frontend && pnpm tsc --noEmit`
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add oddish/src/oddish/schemas.py \
        oddish/src/oddish/core/analyzers.py \
        frontend/src/lib/types.ts \
        "frontend/src/app/(app)/analyzers/[report]/report-detail-client.tsx" \
        backend/tests/test_analyzers_router.py
git commit -m "feat(analyzer): expose and render scaling suggestions"
```

---

### Task 6: Verify end-to-end and open the PR

**Files:** none — verification only.

- [ ] **Step 1: Run the full suites**

Run: `cd oddish && pytest && cd ../backend && pytest`
Expected: PASS. Report any failure with its output rather than proceeding.

- [ ] **Step 2: Inspect a real rendered reduce prompt**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish-present-2/oddish/oddish && python -c "
from oddish.evals.analyzer.prompt_builder import build_reduce_prompt
from oddish.evals.analyzer.schemas import Finding
f = Finding(trial_id='t1', bucket='good', subcategory='3a', evidence_quote='q',
            step_ids=[1], root_cause='rc', headroom_signal='hs',
            trajectory_link='/tasks/t1/probe/x', model='claude-opus-4-8',
            task_path='tasks/redis/expiry', task_id='redis-expiry')
print(build_reduce_prompt([f], {'trials': 2, 'bad': 0, 'good': 1},
      {'redis-expiry': ['claude-opus-4-8', 'claude-haiku-4-5']}))
"`
Expected, read with your eyes — this is the whole point of the change:
- a `task:` and `model:` line under the finding
- a `## Task roster` block naming both models
- `scaling_suggestions` in both the section briefs and the output-keys JSON
- **no** un-replaced `{placeholder}` anywhere

- [ ] **Step 3: Open the PR**

```bash
git push -u origin analyzer-scaling-suggestions
gh pr create --title "Analyzer report: scaling-suggestions section" --body "$(cat <<'EOF'
Adds a fifth analyzer-report section recommending what tasks to build next,
based on the good failures.

The load-bearing change is prompt enrichment. `Finding` carries `task_path` and
`model`, and the output carries `models_by_task`, but none of it reached the
reduce prompt — the synthesizer saw anonymous quotes and so could not name a
task or a model. That is why `headroom_analysis` reads vague, and why a new
section would have read vague too.

- reduce prompt now renders `task_path` + `model` per finding, plus a task roster
  from `models_by_task` (the only record of which models *passed*)
- `reduce.txt` derives its section list and output keys from `SECTION_KEYS`
  instead of hardcoding them
- new `scaling_suggestions` section: new tasks to farm / harder variants / where
  to spend effort, each grounded in a cited task
- `headroom_analysis` is deliberately unchanged — it gets sharper for free from
  the enrichment, and changing both at once would obscure which change did what

Spec: `docs/superpowers/specs/2026-07-15-analyzer-scaling-suggestions-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Enrich `_findings_block` with `task_path` + `model` | 1 |
| `task_roster_block` from `models_by_task` | 1 |
| `build_reduce_prompt` takes `models_by_task` | 1 |
| `models_by_task=None` degrades without raising | 1 (Step 1 test) |
| Roster derived in core from `inputs.bundles`, not the handler | 4 (Steps 6, 6b) |
| New brief, structured, three subsections, cites evidence | 4 |
| `SECTION_KEYS` + `SECTION_KEYS_BY_BUCKET["good"]` | 4 |
| Cohort path unchanged | 4 (verified by `test_analyzer_parse.py`) |
| `reduce.txt` derives count + keys from registry | 2 |
| Sections dict / sandbox / handler wiring | 4 |
| Column + `analyzers_007` migration | 3 |
| `load_only`, `ReportResponse`, `types.ts`, `<Section>` | 5 |
| Uncapped suggestions | 4 (brief states no cap) |
| `headroom_analysis` untouched | non-goal; no task touches it |
| Token-budget watch | 6 (Step 2 renders a real prompt) |

**Placeholder scan:** none — every code step carries the code, every command carries expected output.

**Type consistency:** `task_roster_block` and `output_keys_block` are named identically in their defining tasks (1, 2) and in every later use (2, 4). Short section key is `"scaling"` in `core.py`, `analyzer_sandbox.py`, and `analyzer_handler.py`; long key is `"scaling_suggestions"` in the registry, brief filename, DB column, schema, and TS type. `build_reduce_prompt`'s third parameter is optional in Task 1 and called positionally in Task 4 — consistent.

**Note for the implementer:** Task 3's and Task 5's tests say "match the file's existing fixture style." Read the surrounding test file first — those suites use session fixtures this plan does not reproduce.
