# Unify Skills & Probe Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the "probe presets" feature into the existing "skills" feature so there is one **Skills** page: a mountable file-bundle plus optional probe-directive fields, with agent/model chosen at probe run-time, seeded with the `abundant-ai/skillz` skills + the harbor-lh task-review guide.

**Architecture:** Keep `SkillModel`/`SkillFileModel` (which already own the multi-file bundle + the `stage_org_skills` mount pipeline). Add three nullable directive columns (`operator_prompt`, `result_focus`, `evaluation_metric`) to `SkillModel`. Migrate `probe_presets` rows into `skills`, then retire the preset model/router/core/schemas/UI. Thread a per-probe `skill_ids` selection through the sweep → queue → worker so a skill's bundle mounts **only when selected** (replacing today's mount-all). Repoint the probe-submit UI and auto-probe at skills.

**Tech Stack:** Python 3.11, FastAPI, async SQLAlchemy, Alembic, Pydantic v2, pytest (backend); Next.js 14 App Router, React, TypeScript, Tailwind, shadcn/ui (frontend).

## Global Constraints

- **Never commit to `main`.** Work on branch `feat/unify-skills-presets` (already checked out).
- Backend tests: run `pytest` from `oddish/` (and `backend/` for router tests). Frontend has **no test suite** — frontend tasks use explicit manual verification steps instead of automated tests.
- Core layer functions receive an `AsyncSession` and **never commit**; the calling router owns the transaction.
- New `SkillModel` columns are nullable; existing rows and the SKILL.md-only upload path must keep working with all three NULL.
- Per repo gotcha (`oddish/CLAUDE.md`): if any compact/`load_only` query path enumerates skill columns, new columns must be added there. The skills list path (`list_skills_core`) uses no `load_only`, so it is safe — but do not introduce one.
- Seeds are global (`org_id=NULL`, `is_seed=True`) and read-only (core raises 403 on mutation).
- Skill bundle layout for mounting: `skills_root/<name>/SKILL.md` (+ nested `references/`, `scripts/`). Every skill must have a valid root `SKILL.md` with YAML frontmatter providing `name` + `description` (enforced by `parse_skill`).
- Preserve original `probe_presets.id` values as the migrated `skills.id` so references survive.

---

## File Structure

**Backend (oddish package):**
- `oddish/src/oddish/db/models.py` — add 3 columns to `SkillModel`.
- `oddish/src/oddish/schemas.py` — extend Skill schemas; add `skill_ids` to `TaskSweepSubmission` + `TaskSubmission`; remove `ProbePreset*` schemas (Task 11).
- `oddish/src/oddish/core/skills.py` — persist + validate new fields.
- `oddish/src/oddish/core/sweeps.py` — pass `skill_ids` through.
- `oddish/src/oddish/queue.py` — store `skill_ids` in `harbor_config`.
- `oddish/src/oddish/worker/probe_staging.py` — `stage_org_skills(skill_ids=...)` filter.
- `oddish/src/oddish/worker/local_runner.py`, `oddish/src/oddish/workers/harbor/runner.py` — pass `skill_ids` to `stage_org_skills`.
- `oddish/src/oddish/core/probe/auto_probe.py` — read directive from a seed skill.
- `oddish/src/oddish/core/probe/presets.py` — deleted (Task 11).
- `oddish/alembic/versions/` — new migrations: merge heads, add columns, data-migrate + drop `probe_presets`, seed skills.
- `oddish/src/oddish/seeds/skillz/` — vendored skill bundles (Task 10).

**Backend (hosted layer):**
- `backend/api/routers/skills.py` — unchanged surface (carries new fields automatically via schemas).
- `backend/api/routers/probe_presets.py` — deleted; unregister in the app factory (Task 11).

**Frontend:**
- `frontend/src/app/(app)/qa/skills/skills-client.tsx` — add 3 directive fields.
- `frontend/src/components/probe-submit-form.tsx` — preset picker → skill picker; send `skill_ids`.
- `frontend/src/app/(app)/qa/presets/` — deleted; redirect.
- `frontend/src/app/api/probe-presets/` — deleted.
- `frontend/src/app/(app)/qa/layout.tsx` — remove "Presets" nav tab.

---

## Task 1: Add directive columns to SkillModel + merge Alembic heads + add-columns migration

**Files:**
- Modify: `oddish/src/oddish/db/models.py:1669-1676` (SkillModel columns)
- Create: `oddish/alembic/versions/skills_merge_heads_001_merge.py`
- Create: `oddish/alembic/versions/skills_directive_001_add_directive_columns.py`
- Test: `oddish/tests/test_skill_directive_columns.py`

**Interfaces:**
- Produces: `SkillModel.operator_prompt: str | None`, `SkillModel.result_focus: str | None`, `SkillModel.evaluation_metric: str | None`.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_skill_directive_columns.py`:

```python
from oddish.db import SkillModel, SkillFileModel


def test_skill_model_has_directive_columns():
    skill = SkillModel(
        name="x",
        description="d",
        operator_prompt="do the thing",
        result_focus="what happened?",
        evaluation_metric="result_focus",
        files=[SkillFileModel(relative_path="SKILL.md", content="---\nname: x\ndescription: d\n---\nbody")],
    )
    assert skill.operator_prompt == "do the thing"
    assert skill.result_focus == "what happened?"
    assert skill.evaluation_metric == "result_focus"


def test_skill_model_directive_columns_default_none():
    skill = SkillModel(name="y", description="d")
    assert skill.operator_prompt is None
    assert skill.result_focus is None
    assert skill.evaluation_metric is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_skill_directive_columns.py -v`
Expected: FAIL — `TypeError: 'operator_prompt' is an invalid keyword argument for SkillModel`.

- [ ] **Step 3: Add the columns to SkillModel**

In `oddish/src/oddish/db/models.py`, inside `class SkillModel`, after the `is_seed` column (line 1676) and before the `files` relationship (line 1678), add:

```python
    operator_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_focus: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

(`Text` and `String` are already imported in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_skill_directive_columns.py -v`
Expected: PASS.

- [ ] **Step 5: Merge the existing Alembic heads**

The repo currently has multiple heads. Discover them:

Run: `cd oddish && alembic heads`

If more than one head is printed, create `oddish/alembic/versions/skills_merge_heads_001_merge.py` listing **all** current head revision ids in `down_revision` (replace the example tuple with the exact ids `alembic heads` prints):

```python
"""merge open heads before skills directive columns

Revision ID: skills_merge_heads_001
Revises: apk01dropfk, run_probe_001, merge_tag_documents_heads, merge_tag_mine_filter_heads
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

revision: str = "skills_merge_heads_001"
down_revision: Union[str, Sequence[str], None] = (
    "apk01dropfk",
    "run_probe_001",
    "merge_tag_documents_heads",
    "merge_tag_mine_filter_heads",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

> If `alembic heads` shows exactly one head, skip this file and set the next migration's `down_revision` to that single head instead of `skills_merge_heads_001`.

- [ ] **Step 6: Create the add-columns migration**

Create `oddish/alembic/versions/skills_directive_001_add_directive_columns.py`:

```python
"""add operator_prompt/result_focus/evaluation_metric to skills

Revision ID: skills_directive_001
Revises: skills_merge_heads_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "skills_directive_001"
down_revision: Union[str, Sequence[str], None] = "skills_merge_heads_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS operator_prompt TEXT")
    op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS result_focus TEXT")
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS evaluation_metric VARCHAR(32)"
    )


def downgrade() -> None:
    op.drop_column("skills", "evaluation_metric")
    op.drop_column("skills", "result_focus")
    op.drop_column("skills", "operator_prompt")
```

- [ ] **Step 7: Verify the migration applies to a single head**

Run: `cd oddish && alembic upgrade head && alembic heads`
Expected: upgrade succeeds; `alembic heads` prints exactly one head (`skills_directive_001`).

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/tests/test_skill_directive_columns.py oddish/alembic/versions/skills_merge_heads_001_merge.py oddish/alembic/versions/skills_directive_001_add_directive_columns.py
git commit -m "feat(skills): add operator_prompt/result_focus/evaluation_metric columns"
```

---

## Task 2: Extend Skill schemas + core to persist & validate directive fields

**Files:**
- Modify: `oddish/src/oddish/schemas.py:1474-1507` (SkillCreate/Update/Response)
- Modify: `oddish/src/oddish/core/skills.py` (create/update cores)
- Test: `oddish/tests/test_skills_directive_core.py`

**Interfaces:**
- Consumes: `SkillModel.operator_prompt/result_focus/evaluation_metric` (Task 1); `parse_result_focus`, `normalize_findings_schema`, `UnsupportedSchemaError` from `oddish.core.result_focus_schema`.
- Produces: `SkillCreate`/`SkillUpdate`/`SkillResponse` carry `operator_prompt: str | None`, `result_focus: str | None`, `evaluation_metric: str | None`; `create_skill_core`/`update_skill_core` persist and validate them.

- [ ] **Step 1: Write the failing tests**

Create `oddish/tests/test_skills_directive_core.py`:

```python
import pytest
from fastapi import HTTPException

from oddish.core.skills import create_skill_core, update_skill_core
from oddish.schemas import SkillCreate, SkillFile, SkillUpdate

SKILL_MD = "---\nname: probe-skill\ndescription: a probe\n---\nbody text"


def _files():
    return [SkillFile(relative_path="SKILL.md", content=SKILL_MD)]


@pytest.mark.asyncio
async def test_create_skill_persists_directive_fields(session):
    skill = await create_skill_core(
        session,
        data=SkillCreate(
            name="probe-skill",
            description="a probe",
            files=_files(),
            operator_prompt="probe the verifier",
            result_focus="what bug?",
            evaluation_metric="result_focus",
        ),
        org_id="org1",
    )
    assert skill.operator_prompt == "probe the verifier"
    assert skill.result_focus == "what bug?"
    assert skill.evaluation_metric == "result_focus"


@pytest.mark.asyncio
async def test_create_skill_rejects_bad_result_focus_schema(session):
    with pytest.raises(HTTPException) as exc:
        await create_skill_core(
            session,
            data=SkillCreate(
                name="probe-skill",
                description="a probe",
                files=_files(),
                result_focus='{"type": "nonsense-type"}',
            ),
            org_id="org1",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_skill_sets_directive_fields(session):
    created = await create_skill_core(
        session,
        data=SkillCreate(name="probe-skill", description="a probe", files=_files()),
        org_id="org1",
    )
    updated = await update_skill_core(
        session,
        created.id,
        data=SkillUpdate(operator_prompt="new directive"),
        org_id="org1",
    )
    assert updated.operator_prompt == "new directive"
```

> Reuse the existing async `session` fixture used by other `oddish/tests` (e.g. as in `tests/test_probe_presets_schema.py`); if no shared fixture exists, copy the in-memory async-session fixture from a neighboring core test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd oddish && pytest tests/test_skills_directive_core.py -v`
Expected: FAIL — `SkillCreate` has no `operator_prompt` field (validation error / TypeError).

- [ ] **Step 3: Extend the schemas**

In `oddish/src/oddish/schemas.py`, update the three Skill models:

`SkillCreate` (after `files: list[SkillFile]`):
```python
    operator_prompt: str | None = None
    result_focus: str | None = None
    evaluation_metric: str | None = None
```

`SkillUpdate` (after `files: list[SkillFile] | None = None`):
```python
    operator_prompt: str | None = None
    result_focus: str | None = None
    evaluation_metric: str | None = None
```

`SkillResponse` (after `files: list[SkillFile]`):
```python
    operator_prompt: str | None = None
    result_focus: str | None = None
    evaluation_metric: str | None = None
```

- [ ] **Step 4: Validate + persist in the core**

In `oddish/src/oddish/core/skills.py`, add imports near the top:

```python
from oddish.core.result_focus_schema import (
    UnsupportedSchemaError,
    normalize_findings_schema,
    parse_result_focus,
)
```

Add a helper above `create_skill_core`:

```python
def _validate_result_focus(result_focus: str | None) -> None:
    """Raise HTTPException(422) if a JSON-schema result_focus is malformed."""
    spec = parse_result_focus(result_focus)
    if spec is not None:
        try:
            normalize_findings_schema(spec)
        except UnsupportedSchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
```

In `create_skill_core`, after `name, description = parse_skill(data.files)`, add `_validate_result_focus(data.result_focus)`, and pass the new fields to the `SkillModel(...)` constructor:

```python
    name, description = parse_skill(data.files)
    _validate_result_focus(data.result_focus)
    skill = SkillModel(
        org_id=org_id,
        created_by_user_id=user_id,
        name=name,
        description=description,
        is_seed=False,
        operator_prompt=data.operator_prompt,
        result_focus=data.result_focus,
        evaluation_metric=data.evaluation_metric,
        files=[
            SkillFileModel(relative_path=f.relative_path, content=f.content)
            for f in data.files
        ],
    )
```

In `update_skill_core`, after the existing `files`/`name`/`description` handling and before `await session.flush()`, add directive-field updates honoring `exclude_unset`:

```python
    if "result_focus" in payload:
        _validate_result_focus(data.result_focus)
        skill.result_focus = data.result_focus
    if "operator_prompt" in payload:
        skill.operator_prompt = data.operator_prompt
    if "evaluation_metric" in payload:
        skill.evaluation_metric = data.evaluation_metric
```

(`payload = data.model_dump(exclude_unset=True)` already exists at the top of `update_skill_core`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && pytest tests/test_skills_directive_core.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/src/oddish/core/skills.py oddish/tests/test_skills_directive_core.py
git commit -m "feat(skills): persist + validate directive fields in schemas and core"
```

---

## Task 3: Thread `skill_ids` through the sweep submission into harbor_config

**Files:**
- Modify: `oddish/src/oddish/schemas.py` (`TaskSweepSubmission`, `TaskSubmission`)
- Modify: `oddish/src/oddish/core/sweeps.py:80-106` (`build_task_submission_from_sweep`)
- Modify: `oddish/src/oddish/queue.py:506-536` (`_build_harbor_config_for_trial`)
- Test: `oddish/tests/test_skill_ids_threading.py`

**Interfaces:**
- Produces: `TaskSweepSubmission.skill_ids: list[str] | None`, `TaskSubmission.skill_ids: list[str] | None`; when non-empty, `_build_harbor_config_for_trial` writes `harbor_config["skill_ids"] = [...]`.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_skill_ids_threading.py`:

```python
from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _submission(**kw):
    return TaskSubmission(
        task_path="/tmp/task",
        trials=[TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")],
        **kw,
    )


def test_skill_ids_stored_in_harbor_config():
    sub = _submission(extra_instructions="probe it", skill_ids=["s1", "s2"])
    cfg = _build_harbor_config_for_trial(sub, sub.trials[0])
    assert cfg["skill_ids"] == ["s1", "s2"]


def test_no_skill_ids_key_when_empty():
    sub = _submission(extra_instructions="probe it")
    cfg = _build_harbor_config_for_trial(sub, sub.trials[0])
    assert "skill_ids" not in cfg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_skill_ids_threading.py -v`
Expected: FAIL — `TaskSubmission` has no `skill_ids` field.

- [ ] **Step 3: Add `skill_ids` to both submission schemas**

In `oddish/src/oddish/schemas.py`, add to `TaskSweepSubmission` (next to `extra_instructions`):

```python
    skill_ids: list[str] | None = Field(
        default=None,
        description=(
            "IDs of skills to mount into the probe agent's workspace for every "
            "trial in this submission. Only these skills are mounted (not all "
            "org skills)."
        ),
    )
```

Find the `TaskSubmission` class in the same file and add the identical field (mirror `extra_instructions`'s placement):

```python
    skill_ids: list[str] | None = None
```

- [ ] **Step 4: Pass `skill_ids` through `build_task_submission_from_sweep`**

In `oddish/src/oddish/core/sweeps.py`, in the `TaskSubmission(...)` return of `build_task_submission_from_sweep`, add (next to `extra_instructions=submission.extra_instructions`):

```python
        skill_ids=submission.skill_ids,
```

- [ ] **Step 5: Store `skill_ids` in `harbor_config`**

In `oddish/src/oddish/queue.py`, in `_build_harbor_config_for_trial`, after the `if submission.result_focus:` block and before `return base or None`, add:

```python
    if submission.skill_ids:
        base["skill_ids"] = list(submission.skill_ids)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_skill_ids_threading.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/src/oddish/core/sweeps.py oddish/src/oddish/queue.py oddish/tests/test_skill_ids_threading.py
git commit -m "feat(probe): thread skill_ids from sweep submission into harbor_config"
```

---

## Task 4: Mount only selected skills in `stage_org_skills`

**Files:**
- Modify: `oddish/src/oddish/worker/probe_staging.py:75-111` (`stage_org_skills`)
- Test: `oddish/tests/test_stage_org_skills_filter.py`

**Interfaces:**
- Consumes: `list_skills_core` (unchanged).
- Produces: `stage_org_skills(skills_root, *, org_id, skill_ids: list[str] | None = None) -> int`. When `skill_ids` is provided, only skills whose `id` is in that set are materialized; `None` preserves the previous "all visible skills" behavior; an empty list mounts nothing.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_stage_org_skills_filter.py`:

```python
import pytest

from oddish.worker import probe_staging


class _Skill:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.files = []  # no SKILL.md -> materialize is a no-op stub below


@pytest.mark.asyncio
async def test_stage_filters_to_selected_ids(tmp_path, monkeypatch):
    skills = [_Skill("a", "alpha"), _Skill("b", "beta"), _Skill("c", "gamma")]

    async def fake_list(session, *, org_id=None):
        return skills

    materialized = []

    def fake_materialize(bundles, root):
        materialized.extend(b.name for b in bundles)

    # list_skills_core is imported into probe_staging's namespace.
    monkeypatch.setattr(probe_staging, "list_skills_core", fake_list)
    monkeypatch.setattr(probe_staging, "materialize_skills", fake_materialize)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(probe_staging, "get_session", lambda: _Sess())

    n = await probe_staging.stage_org_skills(
        tmp_path, org_id="org1", skill_ids=["b"]
    )
    assert n == 1
    assert materialized == ["beta"]


@pytest.mark.asyncio
async def test_stage_empty_ids_mounts_nothing(tmp_path, monkeypatch):
    async def fake_list(session, *, org_id=None):
        return [_Skill("a", "alpha")]
    monkeypatch.setattr(probe_staging, "list_skills_core", fake_list)
    monkeypatch.setattr(probe_staging, "materialize_skills", lambda b, r: None)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(probe_staging, "get_session", lambda: _Sess())

    n = await probe_staging.stage_org_skills(tmp_path, org_id="org1", skill_ids=[])
    assert n == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_stage_org_skills_filter.py -v`
Expected: FAIL — `stage_org_skills()` got an unexpected keyword argument `skill_ids`.

- [ ] **Step 3: Add the filter**

In `oddish/src/oddish/worker/probe_staging.py`, change the signature and add filtering:

```python
async def stage_org_skills(
    skills_root: Path, *, org_id: str | None, skill_ids: list[str] | None = None
) -> int:
```

Inside the `async with get_session()` block, after `skills = await list_skills_core(session, org_id=org_id)`, add:

```python
            if skill_ids is not None:
                wanted = set(skill_ids)
                skills = [s for s in skills if s.id in wanted]
```

Update the docstring's first line to note: "Materialize the selected org skills (or all visible skills when ``skill_ids`` is None)…".

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_stage_org_skills_filter.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/worker/probe_staging.py oddish/tests/test_stage_org_skills_filter.py
git commit -m "feat(probe): stage_org_skills mounts only selected skill_ids"
```

---

## Task 5: Pass `skill_ids` to `stage_org_skills` in both runners

**Files:**
- Modify: `oddish/src/oddish/worker/local_runner.py` (~line 379 read; ~line 427 call)
- Modify: `oddish/src/oddish/workers/harbor/runner.py:236-240` (cloud call)

**Interfaces:**
- Consumes: `harbor_config["skill_ids"]` (Task 3); `stage_org_skills(..., skill_ids=...)` (Task 4).

> No automated test: these are integration glue over Harbor/Modal not exercised by unit tests. Verified by reading + the manual probe smoke in Task 13.

- [ ] **Step 1: Read `skill_ids` in the local runner**

In `oddish/src/oddish/worker/local_runner.py`, where `extra_instructions = harbor_config.get("extra_instructions")` and `probe_scope = harbor_config.get("probe_scope", "task")` are read (~line 379), add:

```python
    skill_ids = harbor_config.get("skill_ids")
```

- [ ] **Step 2: Pass it to `stage_org_skills` in the local runner**

In the same file, change the staging call (~line 427) from:

```python
        n_skills = await stage_org_skills(skills_root, org_id=trial_org_id)
```

to:

```python
        n_skills = await stage_org_skills(
            skills_root, org_id=trial_org_id, skill_ids=skill_ids
        )
```

- [ ] **Step 3: Pass it in the cloud harbor runner**

In `oddish/src/oddish/workers/harbor/runner.py`, locate the `stage_org_skills` call (~line 236). Determine where the trial's `harbor_config` dict is available in that scope (it is the same dict the overlay reads). Change:

```python
        if org_id is not None:
            skills_root = unique_parent / "agent_skills"
            n_skills = await stage_org_skills(skills_root, org_id=org_id)
```

to read `skill_ids` from that harbor_config and forward it:

```python
        if org_id is not None:
            skills_root = unique_parent / "agent_skills"
            skill_ids = (harbor_config or {}).get("skill_ids")
            n_skills = await stage_org_skills(
                skills_root, org_id=org_id, skill_ids=skill_ids
            )
```

> If `harbor_config` is not already in scope at that point in `runner.py`, thread it in from the caller (the same place `org_id` is obtained). Grep the function for how `org_id` arrives and mirror it.

- [ ] **Step 4: Verify imports + syntax**

Run: `cd oddish && python -c "import oddish.worker.local_runner, oddish.workers.harbor.runner"`
Expected: no ImportError.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/worker/local_runner.py oddish/src/oddish/workers/harbor/runner.py
git commit -m "feat(probe): forward selected skill_ids to stage_org_skills in both runners"
```

---

## Task 6: Data migration — `probe_presets` → `skills` (preserve ids, synthesize SKILL.md)

**Files:**
- Create: `oddish/src/oddish/core/probe/preset_migration.py` (pure conversion helper, unit-testable)
- Create: `oddish/alembic/versions/skills_from_presets_001_migrate_presets.py`
- Test: `oddish/tests/test_preset_to_skill_conversion.py`

**Interfaces:**
- Produces: `preset_row_to_skill(preset: dict) -> tuple[dict, dict]` returning `(skill_row, skill_md_file_row)` — pure, no DB. `skill_row` keys: `id, org_id, created_by_user_id, name, description, is_seed, operator_prompt, result_focus, evaluation_metric, created_at, updated_at, deleted_at`. The SKILL.md row: `{id, skill_id, relative_path: "SKILL.md", content}`.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_preset_to_skill_conversion.py`:

```python
from oddish.core.probe.preset_migration import preset_row_to_skill


def test_converts_preset_to_skill_with_synthesized_skill_md():
    preset = {
        "id": "cheat-detector",
        "org_id": None,
        "name": "Cheat detector",
        "operator_prompt": "You are a security researcher. Find a cheat.\nMore detail.",
        "result_focus": "Did a cheat succeed?",
        "evaluation_metric": "ratio",
        "is_seed": True,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00",
        "deleted_at": None,
    }
    skill, skill_md = preset_row_to_skill(preset)

    assert skill["id"] == "cheat-detector"          # id preserved
    assert skill["name"] == "Cheat detector"
    assert skill["operator_prompt"] == preset["operator_prompt"]
    assert skill["result_focus"] == "Did a cheat succeed?"
    assert skill["evaluation_metric"] == "ratio"
    assert skill["is_seed"] is True
    # description is the first line of the prompt, truncated
    assert skill["description"].startswith("You are a security researcher")
    # SKILL.md is valid frontmatter with name + description, body = prompt
    assert skill_md["skill_id"] == "cheat-detector"
    assert skill_md["relative_path"] == "SKILL.md"
    assert skill_md["content"].startswith("---\n")
    assert "name: Cheat detector" in skill_md["content"]
    assert preset["operator_prompt"] in skill_md["content"]


def test_description_truncated_to_255():
    preset = {
        "id": "x", "org_id": None, "name": "Long",
        "operator_prompt": "A" * 400, "result_focus": None,
        "evaluation_metric": None, "is_seed": False,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00", "deleted_at": None,
    }
    skill, _ = preset_row_to_skill(preset)
    assert len(skill["description"]) <= 255
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_preset_to_skill_conversion.py -v`
Expected: FAIL — module `oddish.core.probe.preset_migration` does not exist.

- [ ] **Step 3: Write the conversion helper**

Create `oddish/src/oddish/core/probe/preset_migration.py`:

```python
"""Pure conversion of a probe_presets row into a skills (+ SKILL.md) row.

Kept DB-free so the Alembic data migration and unit tests share one
implementation. ``agent``/``model`` are intentionally dropped — agent/model
are chosen at probe run-time, not stored on the directive.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from oddish.db import generate_id


def _description_from_prompt(prompt: str) -> str:
    first = (prompt or "").strip().splitlines()[0] if prompt and prompt.strip() else ""
    first = first.strip() or "Migrated probe preset"
    return first[:255]


def _sanitize_name(name: str) -> str:
    """SKILL.md frontmatter name: lowercase, hyphenated, safe for a dir name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "preset"


def preset_row_to_skill(preset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    description = _description_from_prompt(preset.get("operator_prompt", ""))
    frontmatter = yaml.safe_dump(
        {"name": _sanitize_name(preset["name"]), "description": description},
        sort_keys=False,
    ).strip()
    content = f"---\n{frontmatter}\n---\n\n{preset.get('operator_prompt', '')}"

    skill = {
        "id": preset["id"],
        "org_id": preset.get("org_id"),
        "created_by_user_id": None,
        "name": preset["name"],
        "description": description,
        "is_seed": preset.get("is_seed", False),
        "operator_prompt": preset.get("operator_prompt"),
        "result_focus": preset.get("result_focus"),
        "evaluation_metric": preset.get("evaluation_metric"),
        "created_at": preset.get("created_at"),
        "updated_at": preset.get("updated_at"),
        "deleted_at": preset.get("deleted_at"),
    }
    skill_md = {
        "id": generate_id(),
        "skill_id": preset["id"],
        "relative_path": "SKILL.md",
        "content": content,
    }
    return skill, skill_md
```

> Note the synthesized frontmatter `name` is a slug, while `skills.name` keeps the human label — `parse_skill` only reads the frontmatter `name`, and the row's `name` is authoritative for display, so they may differ without breaking anything.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_preset_to_skill_conversion.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the data migration**

Create `oddish/alembic/versions/skills_from_presets_001_migrate_presets.py`:

```python
"""migrate probe_presets rows into skills (+ SKILL.md), de-duplicating names

Revision ID: skills_from_presets_001
Revises: skills_directive_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.core.probe.preset_migration import preset_row_to_skill

revision: str = "skills_from_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_directive_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("probe_presets"):
        return  # fresh DB without the legacy table

    presets = bind.execute(
        sa.text(
            "SELECT id, org_id, name, operator_prompt, result_focus, "
            "evaluation_metric, is_seed, created_at, updated_at, deleted_at "
            "FROM probe_presets WHERE deleted_at IS NULL"
        )
    ).mappings().all()

    # Names already taken in skills (for the partial unique (org_id, name) index).
    taken = {
        (r["org_id"], r["name"])
        for r in bind.execute(
            sa.text("SELECT org_id, name FROM skills WHERE deleted_at IS NULL")
        ).mappings().all()
    }

    for preset in presets:
        skill, skill_md = preset_row_to_skill(dict(preset))
        key = (skill["org_id"], skill["name"])
        if key in taken:
            skill["name"] = f"{skill['name']} (preset)"
        taken.add((skill["org_id"], skill["name"]))

        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, :org_id, :created_by_user_id, :name, :description, "
                ":is_seed, :operator_prompt, :result_focus, :evaluation_metric, "
                ":created_at, :updated_at, :deleted_at) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            skill,
        )
        bind.execute(
            sa.text(
                "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                "VALUES (:id, :skill_id, :relative_path, :content)"
            ),
            skill_md,
        )


def downgrade() -> None:
    # Non-reversible data migration; presets are re-derivable only from backup.
    pass
```

- [ ] **Step 6: Verify the migration applies**

Run: `cd oddish && alembic upgrade head`
Expected: success (no-op on a fresh DB lacking `probe_presets`; on a seeded DB, rows appear in `skills`).

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/core/probe/preset_migration.py oddish/tests/test_preset_to_skill_conversion.py oddish/alembic/versions/skills_from_presets_001_migrate_presets.py
git commit -m "feat(skills): data-migrate probe_presets into skills"
```

---

## Task 7: Repoint auto-probe at a seed skill directive

**Files:**
- Modify: `oddish/src/oddish/core/probe/auto_probe.py`
- Test: `oddish/tests/test_auto_probe_uses_skill.py`

**Interfaces:**
- Consumes: `SkillModel` with `operator_prompt`; `next_probe_model` (unchanged).
- Produces: auto-probe reads its directive from a seed `SkillModel` (`DEFAULT_PROBE_SKILL_ID = "cheat-detector"`), uses agent `"claude-code"`, mounts that skill (`skill_ids=[skill.id]`).

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_auto_probe_uses_skill.py`:

```python
from oddish.core.probe import auto_probe


def test_default_probe_skill_id_is_a_seed_skill():
    # The auto-probe must reference a skill, not a preset.
    assert hasattr(auto_probe, "DEFAULT_PROBE_SKILL_ID")
    assert not hasattr(auto_probe, "DEFAULT_PROBE_PRESET_ID")


def test_auto_probe_does_not_import_probe_preset_model():
    import inspect
    src = inspect.getsource(auto_probe)
    assert "ProbePresetModel" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_auto_probe_uses_skill.py -v`
Expected: FAIL — `DEFAULT_PROBE_PRESET_ID` still present / `ProbePresetModel` imported.

- [ ] **Step 3: Rewrite the auto-probe to use a skill**

In `oddish/src/oddish/core/probe/auto_probe.py`:

Change the import line:
```python
from oddish.db import ExperimentModel, SkillModel, TaskModel, TrialModel
```

Replace the `DEFAULT_PROBE_PRESET_ID` constant + comment with:
```python
# Auto-probes adopt the directive (operator prompt, result-focus, metric) from
# this seed skill, editable in one place. Agent is fixed to claude-code; the
# model still rotates per task version (``next_probe_model``). Missing -> the
# probe is logged and skipped (the real sweep is unaffected).
DEFAULT_PROBE_SKILL_ID = "cheat-detector"
DEFAULT_PROBE_AGENT = "claude-code"
```

Replace the preset fetch + `submission` block. The fetch:
```python
        skill = await session.scalar(
            select(SkillModel).where(SkillModel.id == DEFAULT_PROBE_SKILL_ID)
        )
        if skill is None or not skill.operator_prompt:
            logger.error(
                "Default probe skill %r not found (or has no operator_prompt); "
                "skipping auto-probe for task %s. Seed the skill to enable "
                "auto-probing.",
                DEFAULT_PROBE_SKILL_ID,
                task.id,
            )
            return
```

The submission:
```python
        submission = TaskSweepSubmission(
            task_id=task.id,
            append_to_task=True,
            name=task.name,
            configs=[AgentModelPair(agent=DEFAULT_PROBE_AGENT, model=model, n_trials=1)],
            extra_instructions=skill.operator_prompt,
            probe_name=skill.name,
            result_focus=skill.result_focus,
            evaluation_metric=skill.evaluation_metric,
            skill_ids=[skill.id],
            experiment_id=(experiment.id if experiment is not None else None),
            user=task.user,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_auto_probe_uses_skill.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/probe/auto_probe.py oddish/tests/test_auto_probe_uses_skill.py
git commit -m "feat(probe): auto-probe reads its directive from a seed skill"
```

---

## Task 8: Seed the auto-probe default skill + the migrated preset seeds

**Files:**
- Create: `oddish/alembic/versions/skills_seed_directives_001_seed.py`
- Test: `oddish/tests/test_seed_directive_skill_md_valid.py`

**Interfaces:**
- Consumes: `parse_skill` (validates the seeded SKILL.md).
- Produces: a global seed skill `id="cheat-detector"` exists on **fresh** DBs (where the preset data migration found nothing), so auto-probe and the picker have a directive out of the box.

> The Task 6 data migration only moves presets that already exist (deployed DBs). Fresh/OSS DBs have no `probe_presets` rows, so this task seeds the four built-in directive skills idempotently using the same prompts as `probe_presets_001`.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_seed_directive_skill_md_valid.py`:

```python
from oddish.alembic_seed_directives import SEED_DIRECTIVE_SKILLS  # see step 3
from oddish.core.skills import parse_skill
from oddish.schemas import SkillFile


def test_each_seed_directive_has_valid_skill_md():
    for s in SEED_DIRECTIVE_SKILLS:
        files = [SkillFile(relative_path="SKILL.md", content=s["skill_md"])]
        name, description = parse_skill(files)  # raises if invalid
        assert name and description
        assert s["operator_prompt"]


def test_cheat_detector_seed_present():
    ids = {s["id"] for s in SEED_DIRECTIVE_SKILLS}
    assert "cheat-detector" in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_seed_directive_skill_md_valid.py -v`
Expected: FAIL — module `oddish.alembic_seed_directives` does not exist.

- [ ] **Step 3: Define the seed data module**

Create `oddish/src/oddish/alembic_seed_directives.py` containing the four directive prompts (copy `_CHEAT_DETECTOR_PROMPT`, `_VERIFIER_CRITIC_PROMPT`, `_AMBIGUITY_FINDER_PROMPT`, `_RUST_C_COMPILER_PROMPT` verbatim from `oddish/alembic/versions/probe_presets_001_add_probe_presets.py`) and a `SEED_DIRECTIVE_SKILLS` list. Each entry:

```python
SEED_DIRECTIVE_SKILLS = [
    {
        "id": "cheat-detector",
        "name": "Cheat detector",
        "operator_prompt": _CHEAT_DETECTOR_PROMPT,
        "result_focus": "Did any cheat attempt successfully bypass the verifier? Which one and why?",
        "evaluation_metric": "ratio",
        "skill_md": _skill_md("cheat-detector", "Probe whether the task/verifier is gameable.", _CHEAT_DETECTOR_PROMPT),
    },
    # ... verifier-critic, ambiguity-finder, rust-c-compiler-targeted (ids match probe_presets_001) ...
]
```

with a local helper:

```python
import yaml

def _skill_md(name: str, description: str, body: str) -> str:
    fm = yaml.safe_dump({"name": name, "description": description}, sort_keys=False).strip()
    return f"---\n{fm}\n---\n\n{body}"
```

Use the exact `result_focus`/`evaluation_metric`/ids from `_SEEDS` in `probe_presets_001` for the other three entries (`verifier-critic`, `ambiguity-finder`, `rust-c-compiler-targeted`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_seed_directive_skill_md_valid.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the seed migration**

Create `oddish/alembic/versions/skills_seed_directives_001_seed.py` that, for each entry in `SEED_DIRECTIVE_SKILLS`, inserts a global seed skill (`org_id=NULL`, `is_seed=True`) + its SKILL.md file, **skipping** any whose `id` already exists (deployed DBs already got them via Task 6):

```python
"""seed built-in directive skills (cheat-detector, verifier-critic, ...)

Revision ID: skills_seed_directives_001
Revises: skills_from_presets_001
Create Date: 2026-06-25 00:00:00.000000
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.alembic_seed_directives import SEED_DIRECTIVE_SKILLS
from oddish.db import generate_id

revision = "skills_seed_directives_001"
down_revision: Union[str, Sequence[str], None] = "skills_from_presets_001"
branch_labels = None
depends_on = None

_TS = datetime(2026, 6, 25, tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        r[0]
        for r in bind.execute(sa.text("SELECT id FROM skills")).all()
    }
    for s in SEED_DIRECTIVE_SKILLS:
        if s["id"] in existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, NULL, NULL, :name, :description, true, :operator_prompt, "
                ":result_focus, :evaluation_metric, :ts, :ts, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["skill_md"].split("description:", 1)[-1].split("\n", 1)[0].strip()[:255] or s["name"],
                "operator_prompt": s["operator_prompt"],
                "result_focus": s["result_focus"],
                "evaluation_metric": s["evaluation_metric"],
                "ts": _TS,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                "VALUES (:id, :skill_id, 'SKILL.md', :content)"
            ),
            {"id": generate_id(), "skill_id": s["id"], "content": s["skill_md"]},
        )


def downgrade() -> None:
    bind = op.get_bind()
    ids = tuple(s["id"] for s in SEED_DIRECTIVE_SKILLS)
    bind.execute(sa.text("DELETE FROM skill_files WHERE skill_id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)), {"ids": list(ids)})
    bind.execute(sa.text("DELETE FROM skills WHERE id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)), {"ids": list(ids)})
```

- [ ] **Step 6: Verify migration applies + seeds exist**

Run: `cd oddish && alembic upgrade head`
Then verify (psql or a quick script): `SELECT id, is_seed FROM skills WHERE id='cheat-detector';` returns one seed row.
Expected: upgrade succeeds; `cheat-detector` seed present.

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/alembic_seed_directives.py oddish/tests/test_seed_directive_skill_md_valid.py oddish/alembic/versions/skills_seed_directives_001_seed.py
git commit -m "feat(skills): seed built-in directive skills on fresh DBs"
```

---

## Task 9: Vendor + seed the skillz + harbor-lh bundle skills

**Files:**
- Create: `oddish/src/oddish/seeds/skillz/<skill>/...` (vendored bundle files)
- Create: `oddish/src/oddish/seeds/loader.py` (filesystem → bundle list)
- Create: `oddish/alembic/versions/skills_seed_bundles_001_seed.py`
- Test: `oddish/tests/test_seed_bundle_loader.py`

**Interfaces:**
- Produces: `load_seed_bundles() -> list[dict]` — each `{"name": str, "description": str, "files": list[tuple[str, str]]}` read from `seeds/skillz/`; nine global bundle-only seed skills (no `operator_prompt`).

- [ ] **Step 1: Vendor the content**

Fetch the 8 skillz skills + the harbor-lh guide into `oddish/src/oddish/seeds/skillz/`:

```bash
cd oddish/src/oddish/seeds
mkdir -p skillz
for s in harbor-task-audit harbor-task-harness-refactor harbor-task-llmj-agentic-refactor harbor-task-taiga-validate oddish sauron-cli taiga-pull-problem-artifacts take-home-pregrader; do
  for path in $(gh api "repos/abundant-ai/skillz/git/trees/HEAD?recursive=1" --jq ".tree[] | select(.path|startswith(\"skills/$s/\")) | select(.type==\"blob\") | .path"); do
    rel="skillz/${path#skills/}"
    mkdir -p "$(dirname "$rel")"
    gh api "repos/abundant-ai/skillz/contents/$path" --jq '.content' | base64 -d > "$rel"
  done
done
# harbor-lh single-file guide -> a bundle-only skill with a generated SKILL.md
mkdir -p skillz/task-review-agent-guide
gh api repos/abundant-ai/harbor-lh/contents/resources/task-review-agent-guide.md --jq '.content' | base64 -d > skillz/task-review-agent-guide/SKILL.md
```

Then ensure `skillz/task-review-agent-guide/SKILL.md` begins with valid frontmatter. If the raw guide has no `---` frontmatter, prepend:

```
---
name: task-review-agent-guide
description: Guide for reviewing Harbor tasks as an agent (from harbor-lh).
---

```

Verify every vendored skill has a root `SKILL.md`:
Run: `cd oddish/src/oddish/seeds/skillz && for d in */; do test -f "$d/SKILL.md" && echo "ok $d" || echo "MISSING $d"; done`
Expected: `ok` for all nine; no `MISSING`.

- [ ] **Step 2: Write the failing loader test**

Create `oddish/tests/test_seed_bundle_loader.py`:

```python
from oddish.core.skills import parse_skill
from oddish.schemas import SkillFile
from oddish.seeds.loader import load_seed_bundles


def test_loads_nine_bundles_each_with_valid_skill_md():
    bundles = load_seed_bundles()
    names = {b["name"] for b in bundles}
    assert len(bundles) == 9
    assert "task-review-agent-guide" in names
    for b in bundles:
        files = [SkillFile(relative_path=p, content=c) for p, c in b["files"]]
        parse_skill(files)  # raises if SKILL.md missing/invalid


def test_bundles_have_no_operator_prompt():
    for b in load_seed_bundles():
        assert "operator_prompt" not in b or b.get("operator_prompt") is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd oddish && pytest tests/test_seed_bundle_loader.py -v`
Expected: FAIL — `oddish.seeds.loader` does not exist.

- [ ] **Step 4: Write the loader**

Create `oddish/src/oddish/seeds/__init__.py` (empty) and `oddish/src/oddish/seeds/loader.py`:

```python
"""Read vendored seed skill bundles from ``seeds/skillz/`` via importlib.resources.

Each child dir of ``skillz/`` is one bundle whose ``name``/``description`` come
from its SKILL.md frontmatter (parsed by the caller); files are returned as
``(relative_path, content)`` pairs so they insert straight into skill_files.
"""

from __future__ import annotations

from importlib import resources

import yaml


def _frontmatter_name_desc(skill_md: str) -> tuple[str, str]:
    parts = skill_md.lstrip().split("---", 2)
    meta = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
    return str(meta.get("name", "")), str(meta.get("description", ""))


def load_seed_bundles() -> list[dict]:
    root = resources.files("oddish.seeds").joinpath("skillz")
    bundles: list[dict] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        files: list[tuple[str, str]] = []
        skill_md = ""
        for f in _walk(child):
            rel = str(f).split(f"skillz/{child.name}/", 1)[-1]
            content = f.read_text(encoding="utf-8")
            files.append((rel, content))
            if rel == "SKILL.md":
                skill_md = content
        name, description = _frontmatter_name_desc(skill_md)
        bundles.append(
            {"name": name or child.name, "description": description, "files": files}
        )
    return bundles


def _walk(traversable):
    for entry in traversable.iterdir():
        if entry.is_dir():
            yield from _walk(entry)
        else:
            yield entry
```

> If `importlib.resources` traversal of nested package data is awkward in this layout, fall back to a `pathlib` walk rooted at `Path(__file__).parent / "skillz"`. Confirm the vendored dir ships in the wheel (add to `pyproject.toml`/`MANIFEST.in` package-data if needed so it is present in the Modal image).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd oddish && pytest tests/test_seed_bundle_loader.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Write the bundle seed migration**

Create `oddish/alembic/versions/skills_seed_bundles_001_seed.py` that inserts each bundle as a global seed skill (`org_id=NULL`, `is_seed=True`, directive fields NULL) + all its files, skipping by `name` if a global skill with that name already exists:

```python
"""seed vendored skillz + harbor-lh bundle skills

Revision ID: skills_seed_bundles_001
Revises: skills_seed_directives_001
Create Date: 2026-06-25 00:00:00.000000
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db import generate_id
from oddish.seeds.loader import load_seed_bundles

revision = "skills_seed_bundles_001"
down_revision: Union[str, Sequence[str], None] = "skills_seed_directives_001"
branch_labels = None
depends_on = None

_TS = datetime(2026, 6, 25, tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    taken = {
        r[0]
        for r in bind.execute(
            sa.text("SELECT name FROM skills WHERE org_id IS NULL AND deleted_at IS NULL")
        ).all()
    }
    for b in load_seed_bundles():
        if b["name"] in taken:
            continue
        skill_id = generate_id()
        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, NULL, NULL, :name, :description, true, NULL, NULL, NULL, "
                ":ts, :ts, NULL)"
            ),
            {"id": skill_id, "name": b["name"], "description": b["description"][:255] or b["name"], "ts": _TS},
        )
        for rel, content in b["files"]:
            bind.execute(
                sa.text(
                    "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                    "VALUES (:id, :skill_id, :rel, :content)"
                ),
                {"id": generate_id(), "skill_id": skill_id, "rel": rel, "content": content},
            )
        taken.add(b["name"])


def downgrade() -> None:
    pass
```

- [ ] **Step 7: Verify migration applies + seeds present**

Run: `cd oddish && alembic upgrade head`
Then: `SELECT name FROM skills WHERE is_seed AND org_id IS NULL ORDER BY name;` should include the 8 skillz names + `task-review-agent-guide` + the 4 directive seeds.
Expected: all present; `alembic heads` shows one head.

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/seeds/ oddish/tests/test_seed_bundle_loader.py oddish/alembic/versions/skills_seed_bundles_001_seed.py
git commit -m "feat(skills): vendor + seed skillz and harbor-lh bundle skills"
```

---

## Task 10: Remove the probe-presets backend (router, core, schemas, model, table)

**Files:**
- Delete: `backend/api/routers/probe_presets.py`, `oddish/src/oddish/core/probe/presets.py`
- Modify: the app factory that registers the presets router (grep `probe_presets` / `probe-presets`)
- Modify: `oddish/src/oddish/schemas.py` (remove `ProbePreset*`)
- Modify: `oddish/src/oddish/db/models.py` (remove `ProbePresetModel`) + `oddish/src/oddish/db/__init__.py` export
- Create: `oddish/alembic/versions/drop_probe_presets_001_drop.py`
- Modify/delete: `backend/tests/test_probe_presets.py`, `oddish/tests/test_probe_presets_schema.py`

**Interfaces:**
- Consumes: Task 6 (rows already migrated) + Task 7 (auto_probe no longer imports `ProbePresetModel`).

> Sequencing: this runs **after** Tasks 6–8 so no live code references the model/table when they are removed.

- [ ] **Step 1: Find every reference**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish-present/oddish && grep -rn "ProbePreset\|probe_presets\|probe-presets\|core.probe.presets\|probe.presets" --include=*.py backend oddish | grep -v alembic/versions | grep -v test_`
Record each hit; each must be removed or repointed.

- [ ] **Step 2: Delete router + core, unregister router**

```bash
git rm backend/api/routers/probe_presets.py oddish/src/oddish/core/probe/presets.py
```
In the app factory (the file that does `from api.routers import probe_presets` / `app.include_router(probe_presets.router ...)` — find via the grep), remove the import and the `include_router` line.

- [ ] **Step 3: Remove schemas + model + export**

In `oddish/src/oddish/schemas.py`, delete `ProbePresetCreate`, `ProbePresetUpdate`, `ProbePresetResponse` (lines ~1212–1250).
In `oddish/src/oddish/db/models.py`, delete `class ProbePresetModel` (lines ~1216–1248).
In `oddish/src/oddish/db/__init__.py`, remove `ProbePresetModel` from imports/`__all__`.

- [ ] **Step 4: Delete/replace the preset tests**

```bash
git rm backend/tests/test_probe_presets.py oddish/tests/test_probe_presets_schema.py
```

- [ ] **Step 5: Write the drop-table migration**

Create `oddish/alembic/versions/drop_probe_presets_001_drop.py`:

```python
"""drop probe_presets table (rows migrated into skills)

Revision ID: drop_probe_presets_001
Revises: skills_seed_bundles_001
Create Date: 2026-06-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "drop_probe_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_seed_bundles_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS probe_presets")


def downgrade() -> None:
    pass
```

- [ ] **Step 6: Verify nothing imports the removed names**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish-present/oddish && grep -rn "ProbePreset\|core.probe.presets" --include=*.py backend oddish | grep -v alembic/versions`
Expected: no output.
Run: `cd oddish && python -c "import oddish.schemas, oddish.db, oddish.core.probe.auto_probe"` and `cd backend && python -c "import api.app"` (or the actual factory module).
Expected: no ImportError.

- [ ] **Step 7: Run the backend suites**

Run: `cd oddish && pytest -q` then `cd backend && pytest -q`
Expected: PASS (no references to removed preset code remain).

- [ ] **Step 8: Apply migrations**

Run: `cd oddish && alembic upgrade head && alembic heads`
Expected: success; single head `drop_probe_presets_001`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(probe): remove probe_presets router/core/schemas/model/table"
```

---

## Task 11: Skills page — add the directive fields to the create/edit form

**Files:**
- Modify: `frontend/src/app/(app)/qa/skills/skills-client.tsx`

**Interfaces:**
- Consumes: backend `SkillResponse`/`SkillCreate` now carry `operator_prompt`/`result_focus`/`evaluation_metric` (Task 2).

> No automated test (no FE suite). Verified manually in Step 6.

- [ ] **Step 1: Extend the `Skill` type**

In `skills-client.tsx`, add to the `Skill` type (after `description: string;`):

```typescript
  operator_prompt: string | null;
  result_focus: string | null;
  evaluation_metric: string | null;
```

- [ ] **Step 2: Add form state**

In the `SkillForm` component, next to the existing `useState` hooks (lines ~207–226), add:

```typescript
  const [operatorPrompt, setOperatorPrompt] = useState(
    editingSkill?.operator_prompt ?? "",
  );
  const [resultFocus, setResultFocus] = useState(
    editingSkill?.result_focus ?? "",
  );
  const [evaluationMetric, setEvaluationMetric] = useState(
    editingSkill?.evaluation_metric ?? "none",
  );
```

- [ ] **Step 3: Add the form fields**

In the form JSX, after the `SKILL.md body` textarea block and before the `{/* Extra files */}` comment, insert a collapsible-style "Probe directive (optional)" section:

```tsx
      <div className="space-y-3 rounded-md border border-[#6f88b4]/15 p-3">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
          Probe directive (optional)
        </p>
        <div className="space-y-1.5">
          <Label htmlFor="skill-operator-prompt" className="text-xs font-medium">
            Operator prompt
          </Label>
          <textarea
            id="skill-operator-prompt"
            value={operatorPrompt}
            onChange={(e) => setOperatorPrompt(e.target.value)}
            rows={6}
            placeholder="If set, this skill can drive a probe: prompt prepended to the task instruction…"
            className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 font-mono text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="skill-metric" className="text-xs font-medium">
              Evaluation metric
            </Label>
            <Select value={evaluationMetric} onValueChange={setEvaluationMetric}>
              <SelectTrigger id="skill-metric" className="h-8 border-[#6f88b4]/20">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="result_focus">Result focus</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="skill-result-focus" className="text-xs font-medium">
            Result focus <span className="text-muted-foreground">(optional)</span>
          </Label>
          <textarea
            id="skill-result-focus"
            value={resultFocus}
            onChange={(e) => setResultFocus(e.target.value)}
            rows={3}
            placeholder="A question (prose) or a JSON Schema for structured output."
            className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 font-mono text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
      </div>
```

Ensure `Select`, `SelectTrigger`, `SelectValue`, `SelectContent`, `SelectItem` are imported (copy the import line from `presets-client.tsx` if missing).

- [ ] **Step 4: Include the fields in the save payload**

In `handleSave`, extend the `payload` object (after `files,`):

```typescript
      operator_prompt: operatorPrompt.trim() || null,
      result_focus: resultFocus.trim() || null,
      evaluation_metric: evaluationMetric === "none" ? null : evaluationMetric,
```

- [ ] **Step 5: Build the frontend**

Run: `cd frontend && pnpm build`
Expected: build succeeds (no type errors).

- [ ] **Step 6: Manual verification**

Run `cd frontend && pnpm dev` (and the backend per `backend/README.md`). At `/qa/skills`: create a skill, fill the Operator prompt + Result focus + metric, save; reopen it and confirm the values round-trip. Confirm a skill with all directive fields blank still saves (bundle-only).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/app/\(app\)/qa/skills/skills-client.tsx
git commit -m "feat(skills-ui): add optional probe-directive fields to the skill form"
```

---

## Task 12: Probe submit form — pick a Skill instead of a Preset; send `skill_ids`

**Files:**
- Modify: `frontend/src/components/probe-submit-form.tsx`

**Interfaces:**
- Consumes: `/api/skills` (existing proxy) returning skills with directive fields; sweep endpoint accepting `skill_ids`.

> No automated test. Verified manually in Step 5.

- [ ] **Step 1: Load skills instead of presets**

In `probe-submit-form.tsx`, repoint the data source from `/api/probe-presets` to `/api/skills` and rename the picker state: `presets`→`skills`, `selectedPresetId`→`selectedSkillId`, `selectedPreset`→`selectedSkill`, `presetsLoaded`→`skillsLoaded`. The skill objects now carry `operator_prompt`/`result_focus`/`evaluation_metric` (nullable) and `files`.

Update `loadPreset` → `loadSkill`:

```typescript
function loadSkill(id: string) {
  setSelectedSkillId(id);
  if (!id) return;
  const s = skills.find((x) => x.id === id);
  if (!s) return;
  setExtraInstructions(s.operator_prompt ?? "");
  setResultFocus(s.result_focus ?? "");
}
```

(Do **not** set agent/model from the skill — agent/model stay user-driven via their own selectors. Remove the `setAgent`/`setModel` calls that previously came from the preset.)

- [ ] **Step 2: Update the picker JSX**

In the picker (lines ~317–363), change the label to "Skill", iterate `skills`, call `loadSkill`, and keep the "+ Create" button pointing at the skills page. Since skill creation/edit now lives on `/qa/skills`, replace the inline create/edit/delete buttons with a link:

```tsx
    <Button type="button" variant="outline" asChild>
      <a href="/qa/skills" target="_blank" rel="noreferrer">Manage skills</a>
    </Button>
```

Optionally label directive-bearing skills: `{s.name}{s.operator_prompt ? "" : " (bundle)"}{s.is_seed ? " (built-in)" : ""}`.

- [ ] **Step 3: Send `skill_ids` (and skill name) in the submit payload**

In `onSubmit`, update the body (lines ~286–295): replace `probe_name: selectedPreset?.name ?? null` and add `skill_ids`:

```typescript
        probe_name: selectedSkill?.name ?? null,
        result_focus: result_focus.trim() || null,
        evaluation_metric: selectedSkill?.evaluation_metric ?? null,
        skill_ids: selectedSkillId ? [selectedSkillId] : null,
```

- [ ] **Step 4: Build the frontend**

Run: `cd frontend && pnpm build`
Expected: build succeeds (no type/lint errors; no remaining references to `presets`/`selectedPresetId`).

- [ ] **Step 5: Manual verification**

With backend + `pnpm dev` running: open a task's probe submit form, pick a directive skill (e.g. "Cheat detector"), confirm the operator prompt populates and agent/model are independently selectable; submit and confirm a probe trial is created. In the probe run, confirm only the selected skill's bundle is mounted (check the staged `agent_skills/` dir / probe logs).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/probe-submit-form.tsx
git commit -m "feat(probe-ui): select a skill (not preset) and send skill_ids on submit"
```

---

## Task 13: Delete the presets page, proxy routes, and nav tab; redirect `/qa/presets`

**Files:**
- Delete: `frontend/src/app/(app)/qa/presets/presets-client.tsx`
- Replace: `frontend/src/app/(app)/qa/presets/page.tsx` (with a redirect)
- Delete: `frontend/src/app/api/probe-presets/route.ts`, `frontend/src/app/api/probe-presets/[preset_id]/route.ts`
- Modify: `frontend/src/app/(app)/qa/layout.tsx` (remove the Presets tab)

> No automated test. Verified manually in Step 5.

- [ ] **Step 1: Remove the Presets nav tab**

In `frontend/src/app/(app)/qa/layout.tsx`, change `CONFIG_TABS` to drop the presets entry:

```typescript
const CONFIG_TABS = [
  { href: "/qa/skills", label: "Skills" },
  { href: "/qa/documents", label: "Documents" },
];
```

- [ ] **Step 2: Replace the presets page with a redirect**

```bash
git rm frontend/src/app/\(app\)/qa/presets/presets-client.tsx
```

Overwrite `frontend/src/app/(app)/qa/presets/page.tsx`:

```typescript
import { redirect } from "next/navigation";

export default function PresetsPage() {
  redirect("/qa/skills");
}
```

- [ ] **Step 3: Delete the preset API proxy routes**

```bash
git rm frontend/src/app/api/probe-presets/route.ts frontend/src/app/api/probe-presets/\[preset_id\]/route.ts
```

- [ ] **Step 4: Build the frontend**

Run: `cd frontend && pnpm build`
Expected: build succeeds; grep shows no remaining imports of the deleted files.
Run: `cd frontend && grep -rn "probe-presets\|presets-client\|PresetsClient" src` → expected: no output.

- [ ] **Step 5: Manual verification**

With `pnpm dev`: the QA nav shows only "Probe Runs / Skills / Documents". Visiting `/qa/presets` redirects to `/qa/skills`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ui): retire the presets page, proxy routes, and nav tab"
```

---

## Task 14: Full verification + branch wrap-up

**Files:** none (verification only).

- [ ] **Step 1: Backend tests**

Run: `cd oddish && pytest -q` and `cd backend && pytest -q`
Expected: PASS.

- [ ] **Step 2: Migrations apply cleanly from scratch**

Run (against a scratch DB): `cd oddish && alembic upgrade head && alembic heads`
Expected: single head `drop_probe_presets_001`; `skills` has the 13 seed skills (8 skillz + harbor-lh guide + 4 directive seeds); no `probe_presets` table.

- [ ] **Step 3: Frontend build + lint**

Run: `cd frontend && pnpm build && pnpm lint`
Expected: both succeed.

- [ ] **Step 4: End-to-end probe smoke (local mode)**

Per `backend/README.md`, run the backend in local mode and submit a probe from `/qa/skills` selecting a directive skill. Confirm: trial created, only the selected skill bundle staged into `agent_skills/`, directive applied to `instruction.md`, result-focus reaches the analyzer.

- [ ] **Step 5: Push branch + open PR**

```bash
git push -u origin feat/unify-skills-presets
gh pr create --title "Unify skills & probe presets into one Skills feature" --body "Implements docs/superpowers/specs/2026-06-25-unify-skills-presets-design.md"
```

---

## Self-Review notes (addressed)

- **Spec coverage:** delete presets page (Task 13), rename/keep Skills page (Tasks 11/13), use skills Description UI (Task 11), drop agent/model from directive (Tasks 2/7/12 — never added to skills; agent/model come from the launch form), seed skillz + harbor-lh (Task 9), keep result_focus/evaluation_metric (Tasks 2/11), mount only when selected (Tasks 3–5/12), keep+migrate existing data (Tasks 6/8), retire probe_presets (Task 10). All mapped.
- **Mount-all behavior change:** `stage_org_skills(skill_ids=None)` still mounts all visible skills as a fallback, but every probe path now passes an explicit `skill_ids` (the selected skill, or `None`→none for non-probe trials that never call it), so seeds do not bloat runs. Documented in Tasks 4–5.
- **Ordering:** model/columns (1) → schemas/core (2) → skill_ids plumbing (3–5) → data migration (6) → auto-probe repoint (7) → seeds (8–9) → remove presets (10) → frontend (11–13) → verify (14). Removal happens only after all readers are repointed.
- **Type consistency:** `stage_org_skills(skills_root, *, org_id, skill_ids=None)`, `harbor_config["skill_ids"]`, `skill_ids` on both submission schemas, `preset_row_to_skill`, `load_seed_bundles`, `SEED_DIRECTIVE_SKILLS`, `DEFAULT_PROBE_SKILL_ID` used consistently across tasks.
