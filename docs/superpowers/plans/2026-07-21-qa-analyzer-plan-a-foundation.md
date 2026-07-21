# QA Analyzer — Plan A: Foundation (ActionItem schema + versioned prompt registry)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared `ActionItem` schema and a DB-backed, versioned, CLI-configurable prompt registry that the pre-trial and post-trial QA analyzers (Plans B–D) depend on.

**Architecture:** Two SQLAlchemy tables (`prompts` parent + `prompt_versions` child) in the `oddish/` package, fronted by pure `core/prompts.py` functions, a thin FastAPI `/prompts` router in `backend/`, and an `oddish prompt` Typer sub-app. Prompt edits append immutable versions; an `active_version` pointer selects which one runs. `ActionItem` is a pydantic model living beside the existing verdict models.

**Tech Stack:** Python 3.11, SQLAlchemy (async, `Mapped`/`mapped_column`), Alembic, FastAPI, Typer + httpx, pydantic v2, pytest + pytest-asyncio.

## Global Constraints

- Package boundary: `oddish/` must NOT import from `backend/`. Routers in `backend/` import `oddish` freely and delegate to `oddish/src/oddish/core/*` functions. (AGENTS.md)
- Core functions never commit; the router calls `await session.commit()`. Core ends writes with `await session.flush()`. (mirror `oddish/src/oddish/core/skills.py`)
- Pydantic response models set `model_config = {"from_attributes": True}` to serialize ORM rows.
- New soft-deletable models MUST be registered in `register_soft_delete_models(...)` at the bottom of `oddish/src/oddish/db/models.py`.
- New models + `utcnow` must be re-exported from `oddish/src/oddish/db/__init__.py` (import block + `__all__`).
- Migrations: core-stack migrations live in `oddish/alembic/versions/`, run `uv run alembic upgrade head` from `oddish/`. Filenames use readable slugs (`prompts_001_add_prompt_registry.py`), revision strings match the slug (not a hash). Include an inspector guard and a paired `downgrade()`.
- The core stack has MULTIPLE alembic heads — never assume linear history; resolve the parent revision from `uv run alembic heads` (see Task 2).
- DB tests hit a real Postgres. Bring one up per AGENTS.md (`docker run -d --name oddish-db -e POSTGRES_PASSWORD=... -p 5432:5432 postgres:16-alpine`), migrate with `uv run python -m oddish.db setup`, and run with `set -a && source .env.local && set +a && uv run pytest ...` from `backend/`. Use a fresh throwaway DB — do not reuse the shared local one (it may be stale/blocked on migrations).
- Enum classes are `class X(str, Enum)` with `UPPER = "value"` members; structured-output pydantic fields use `Field(description=...)`.

---

### Task 1: `ActionItem` schema + enums + stable id

**Files:**
- Modify: `oddish/src/oddish/analyze/models.py` (append; reuse existing imports at lines 1–7: `dataclass`, `Enum`, `Literal`, `BaseModel`, `Field`)
- Test: `oddish/tests/analyze/test_action_item.py`

**Interfaces:**
- Produces:
  - `ActionItemSource` (`str, Enum`): `PRE_TRIAL="pre_trial"`, `POST_TRIAL="post_trial"`
  - `ProblemType` (`str, Enum`): `INCOMPLETENESS="incompleteness"`, `MISMATCH="mismatch"`
  - `Dimension` (`str, Enum`): `VERIFIER="verifier"`, `ORACLE="oracle"`, `INFO_LEAKAGE="info_leakage"`
  - `ActionTier` (`str, Enum`): `MUST_FIX="must_fix"`, `SHOULD_FIX="should_fix"`, `OPTIONAL="optional"`
  - `ActionItem(BaseModel)` — the structured-output + storage schema
  - `compute_action_item_id(item: ActionItem) -> str` — deterministic 12-char id

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/analyze/test_action_item.py
from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    Dimension,
    ProblemType,
    ActionTier,
    compute_action_item_id,
)


def _item(**over):
    base = dict(
        source=ActionItemSource.PRE_TRIAL,
        problem_type=ProblemType.INCOMPLETENESS,
        dimension=Dimension.VERIFIER,
        file="verifier.py",
        line_start=10,
        line_end=12,
        title="Verifier ignores stderr",
        detail="grade() only checks stdout",
        recommendation="Also assert on stderr",
        tier=ActionTier.MUST_FIX,
    )
    base.update(over)
    return ActionItem(**base)


def test_defaults_for_post_trial_linkage_fields():
    item = _item()
    assert item.links_to is None
    assert item.exploited is False
    assert item.exploit_evidence is None
    assert item.causal is False


def test_id_is_stable_for_equal_content():
    a = compute_action_item_id(_item())
    b = compute_action_item_id(_item())
    assert a == b
    assert len(a) == 12


def test_id_changes_when_location_changes():
    a = compute_action_item_id(_item(line_start=10))
    b = compute_action_item_id(_item(line_start=99))
    assert a != b


def test_enum_values_serialize_as_strings():
    item = _item()
    dumped = item.model_dump(mode="json")
    assert dumped["source"] == "pre_trial"
    assert dumped["dimension"] == "verifier"
    assert dumped["tier"] == "must_fix"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_action_item.py -v`
Expected: FAIL with `ImportError: cannot import name 'ActionItem'`.

- [ ] **Step 3: Write minimal implementation**

Append to `oddish/src/oddish/analyze/models.py` (add `import hashlib` to the top-of-file imports):

```python
class ActionItemSource(str, Enum):
    PRE_TRIAL = "pre_trial"
    POST_TRIAL = "post_trial"


class ProblemType(str, Enum):
    INCOMPLETENESS = "incompleteness"
    MISMATCH = "mismatch"


class Dimension(str, Enum):
    VERIFIER = "verifier"
    ORACLE = "oracle"
    INFO_LEAKAGE = "info_leakage"


class ActionTier(str, Enum):
    MUST_FIX = "must_fix"
    SHOULD_FIX = "should_fix"
    OPTIONAL = "optional"


class ActionItem(BaseModel):
    """A single QA finding with a file/line anchor. Emitted by both the
    pre-trial and post-trial analyzers; the ``id`` is computed server-side
    (LLM output omits it)."""

    id: str | None = Field(
        default=None, description="Stable id; computed server-side, leave null"
    )
    source: ActionItemSource = Field(description="Which analyzer produced this item")
    problem_type: ProblemType = Field(description="incompleteness or mismatch")
    dimension: Dimension = Field(
        description="verifier, oracle, or info_leakage"
    )
    file: str = Field(description="Task-relative path, e.g. 'verifier.py'")
    line_start: int = Field(description="1-indexed first line")
    line_end: int = Field(description="1-indexed last line (== line_start if one line)")
    title: str = Field(description="Short one-line summary")
    detail: str = Field(description="What is wrong")
    recommendation: str = Field(description="Concrete fix")
    tier: ActionTier = Field(description="must_fix, should_fix, or optional")

    # post_trial-only linkage fields (defaults keep pre_trial items clean)
    links_to: str | None = Field(
        default=None, description="pre_trial ActionItem.id this relates to"
    )
    exploited: bool = Field(
        default=False, description="Did the trajectory exploit this weakness?"
    )
    exploit_evidence: str | None = Field(
        default=None, description="Quote or step reference showing exploitation"
    )
    causal: bool = Field(
        default=False, description="Did trajectory behavior result from this weakness?"
    )


def compute_action_item_id(item: ActionItem) -> str:
    """Deterministic id from the item's identity fields (not its linkage state)."""
    raw = "|".join(
        [
            item.source.value,
            item.dimension.value,
            item.problem_type.value,
            item.file,
            str(item.line_start),
            str(item.line_end),
            item.title.strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/analyze/test_action_item.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/models.py oddish/tests/analyze/test_action_item.py
git commit -m "feat(analyze): shared ActionItem schema + stable id"
```

---

### Task 2: `prompts` + `prompt_versions` tables + migration

**Files:**
- Modify: `oddish/src/oddish/db/models.py` (append two models; update `register_soft_delete_models(...)`)
- Modify: `oddish/src/oddish/db/__init__.py` (import block + `__all__`)
- Create: `oddish/alembic/versions/prompts_001_add_prompt_registry.py`
- Test: `oddish/tests/db/test_prompt_models.py`

**Interfaces:**
- Produces:
  - `PromptModel` — `id, key (unique), description, active_version: int | None`, timestamps, `versions` relationship
  - `PromptVersionModel` — `id, prompt_id (FK cascade), version: int, content, created_at, created_by`
  - unique `(prompt_id, version)`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/db/test_prompt_models.py
from oddish.db import PromptModel, PromptVersionModel


def test_models_expose_expected_columns():
    assert PromptModel.__tablename__ == "prompts"
    assert PromptVersionModel.__tablename__ == "prompt_versions"
    cols = set(PromptModel.__table__.columns.keys())
    assert {"id", "key", "description", "active_version", "created_at", "deleted_at"} <= cols
    vcols = set(PromptVersionModel.__table__.columns.keys())
    assert {"id", "prompt_id", "version", "content", "created_at", "created_by"} <= vcols


def test_unique_constraint_on_prompt_id_version():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in PromptVersionModel.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("prompt_id", "version") in uniques
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_prompt_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'PromptModel'`.

- [ ] **Step 3a: Add the models**

Append to `oddish/src/oddish/db/models.py` (before the `register_soft_delete_models(...)` call near line 2112). Reuse existing imports (`String, Integer, Text, ForeignKey, DateTime, UniqueConstraint, Index, text`, `Mapped, mapped_column, relationship`, `TimestampedMixin, Base, generate_id, utcnow`):

```python
class PromptModel(TimestampedMixin, Base):
    """A named, versioned analyzer prompt. ``active_version`` points at the
    ``prompt_versions.version`` that runs. Editing appends a new version."""

    __tablename__ = "prompts"
    __table_args__ = (
        Index(
            "idx_prompts_unique_key",
            "key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    versions: Mapped[list["PromptVersionModel"]] = relationship(  # type: ignore[assignment]
        "PromptVersionModel",
        back_populates="prompt",
        cascade="all, delete-orphan",
        order_by="PromptVersionModel.version",
        lazy="selectin",
    )


class PromptVersionModel(Base):
    """One immutable revision of a prompt's content."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    prompt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prompt: Mapped["PromptModel"] = relationship(  # type: ignore[assignment]
        "PromptModel", back_populates="versions"
    )
```

Confirm `UniqueConstraint` is in the `from sqlalchemy import (...)` block at the top of `models.py`; if absent, add it there.

Then add `PromptModel` to the `register_soft_delete_models(...)` argument list (near line 2112). Do NOT register `PromptVersionModel` (it cascades with the parent and has no `deleted_at`).

- [ ] **Step 3b: Re-export from the db package**

In `oddish/src/oddish/db/__init__.py`, add `PromptModel, PromptVersionModel` to the models import block and to `__all__`.

- [ ] **Step 4: Run the model test to verify it passes**

Run: `cd oddish && uv run pytest tests/db/test_prompt_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Write the migration**

First resolve the parent revision:

Run: `cd oddish && uv run alembic heads`
- If it prints exactly one revision `R`, use `R` as `down_revision` below.
- If it prints multiple revisions, first merge them:
  Run: `cd oddish && uv run alembic merge -m prompts_base heads` — note the new revision id `M` it creates, then use `M` as `down_revision`.

Create `oddish/alembic/versions/prompts_001_add_prompt_registry.py` (replace `PARENT_REVISION` with `R` or `M` from above):

```python
"""add prompt registry tables

Two tables for DB-backed, versioned analyzer prompts. ``000_initial_schema``
runs ``create_all()`` on fresh DBs, so guard with the inspector.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_001"
down_revision: Union[str, Sequence[str], None] = "PARENT_REVISION"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names()
    if "prompts" not in existing:
        op.create_table(
            "prompts",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column("description", sa.Text, nullable=False, server_default=""),
            sa.Column("active_version", sa.Integer, nullable=True),
        )
        op.create_index(
            "idx_prompts_unique_key",
            "prompts",
            ["key"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    if "prompt_versions" not in existing:
        op.create_table(
            "prompt_versions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("prompt_id", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.ForeignKeyConstraint(
                ["prompt_id"], ["prompts.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "prompt_id", "version", name="uq_prompt_versions_prompt_version"
            ),
        )
        op.create_index(
            "ix_prompt_versions_prompt_id", "prompt_versions", ["prompt_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_prompt_versions_prompt_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("idx_prompts_unique_key", table_name="prompts")
    op.drop_table("prompts")
```

- [ ] **Step 6: Apply the migration against a throwaway DB**

Run: `cd oddish && uv run alembic upgrade head`
Expected: no error; ends at `prompts_001` (verify with `uv run alembic current`).

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/src/oddish/db/__init__.py \
        oddish/alembic/versions/prompts_001_add_prompt_registry.py \
        oddish/tests/db/test_prompt_models.py
git commit -m "feat(db): prompt registry tables + migration"
```

---

### Task 3: `core/prompts.py` — versioning logic

**Files:**
- Create: `oddish/src/oddish/core/prompts.py`
- Test: `backend/tests/test_prompts_core.py` (real Postgres, mirrors `backend/tests/test_skills.py`)

**Interfaces:**
- Consumes: `PromptModel`, `PromptVersionModel`, `get_session` from `oddish.db`
- Produces (all async; none commit — caller commits):
  - `set_prompt_core(session, *, key, content, description=None, activate=True, created_by=None) -> PromptVersionModel` — creates the prompt on first use (v1), else appends `max(version)+1`; activates when `activate=True`
  - `list_prompts_core(session) -> list[PromptModel]`
  - `get_prompt_core(session, key, *, version=None) -> tuple[PromptModel, PromptVersionModel]` — resolves the active version when `version is None`; raises `HTTPException(404)` if the key or version is missing
  - `list_prompt_versions_core(session, key) -> list[PromptVersionModel]`
  - `activate_prompt_version_core(session, key, version) -> PromptModel` — 404 if the version does not exist
  - `get_active_prompt_content(session, key) -> str` — convenience for block consumers (Plans B–C); 404 if missing or no active version

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_prompts_core.py
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.prompts import (
    activate_prompt_version_core,
    get_active_prompt_content,
    get_prompt_core,
    list_prompt_versions_core,
    set_prompt_core,
)
from oddish.db import PromptModel, get_session


@pytest_asyncio.fixture
async def prompt_key():
    key = f"test_prompt_{uuid.uuid4().hex[:8]}"
    yield key
    async with get_session() as session:
        await session.execute(PromptModel.__table__.delete().where(PromptModel.key == key))
        await session.commit()


@pytest.mark.asyncio
async def test_set_creates_v1_and_activates(prompt_key):
    async with get_session() as session:
        v = await set_prompt_core(session, key=prompt_key, content="hello", description="d")
        await session.commit()
        assert v.version == 1
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, prompt_key)
        assert prompt.active_version == 1
        assert ver.content == "hello"


@pytest.mark.asyncio
async def test_set_appends_and_bumps_version(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        v2 = await set_prompt_core(session, key=prompt_key, content="v2")
        await session.commit()
        assert v2.version == 2
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v2"
        versions = await list_prompt_versions_core(session, prompt_key)
        assert [x.version for x in versions] == [1, 2]


@pytest.mark.asyncio
async def test_activate_rolls_back_to_earlier_version(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await set_prompt_core(session, key=prompt_key, content="v2")
        await session.commit()
    async with get_session() as session:
        await activate_prompt_version_core(session, prompt_key, 1)
        await session.commit()
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v1"


@pytest.mark.asyncio
async def test_set_no_activate_keeps_pointer(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v2", activate=False)
        await session.commit()
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v1"


@pytest.mark.asyncio
async def test_get_missing_key_raises_404():
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_prompt_core(session, "does_not_exist_xyz")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_activate_missing_version_raises_404(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await activate_prompt_version_core(session, prompt_key, 99)
        assert exc.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Ensure a throwaway Postgres is migrated (see Global Constraints), then:
Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_prompts_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oddish.core.prompts'`.

- [ ] **Step 3: Write minimal implementation**

```python
# oddish/src/oddish/core/prompts.py
"""Versioned prompt registry — pure core logic. Callers own the transaction;
these functions never commit (they ``flush`` so ids/defaults populate)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import PromptModel, PromptVersionModel


async def _get_prompt(session: AsyncSession, key: str) -> PromptModel | None:
    result = await session.execute(select(PromptModel).where(PromptModel.key == key))
    return result.scalar_one_or_none()


async def set_prompt_core(
    session: AsyncSession,
    *,
    key: str,
    content: str,
    description: str | None = None,
    activate: bool = True,
    created_by: str | None = None,
) -> PromptVersionModel:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        prompt = PromptModel(key=key, description=description or "")
        session.add(prompt)
        await session.flush()
        next_version = 1
    else:
        if description is not None:
            prompt.description = description
        versions = await prompt.awaitable_attrs.versions
        next_version = (max((v.version for v in versions), default=0)) + 1

    version = PromptVersionModel(
        prompt_id=prompt.id,
        version=next_version,
        content=content,
        created_by=created_by,
    )
    session.add(version)
    if activate:
        prompt.active_version = next_version
    await session.flush()
    return version


async def list_prompts_core(session: AsyncSession) -> list[PromptModel]:
    result = await session.execute(select(PromptModel).order_by(PromptModel.key))
    return list(result.scalars().all())


async def list_prompt_versions_core(
    session: AsyncSession, key: str
) -> list[PromptVersionModel]:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' not found")
    versions = await prompt.awaitable_attrs.versions
    return sorted(versions, key=lambda v: v.version)


async def get_prompt_core(
    session: AsyncSession, key: str, *, version: int | None = None
) -> tuple[PromptModel, PromptVersionModel]:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' not found")
    target = version if version is not None else prompt.active_version
    if target is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' has no active version")
    versions = await prompt.awaitable_attrs.versions
    for v in versions:
        if v.version == target:
            return prompt, v
    raise HTTPException(
        status_code=404, detail=f"Prompt '{key}' has no version {target}"
    )


async def activate_prompt_version_core(
    session: AsyncSession, key: str, version: int
) -> PromptModel:
    prompt, _ = await get_prompt_core(session, key, version=version)
    prompt.active_version = version
    await session.flush()
    return prompt


async def get_active_prompt_content(session: AsyncSession, key: str) -> str:
    _, ver = await get_prompt_core(session, key)
    return ver.content
```

Note: `awaitable_attrs.versions` lazily loads the relationship in async context (the model uses `lazy="selectin"`, but `awaitable_attrs` is the safe accessor when the parent was just flushed).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_prompts_core.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/prompts.py backend/tests/test_prompts_core.py
git commit -m "feat(core): versioned prompt registry logic"
```

---

### Task 4: `/prompts` router + pydantic schemas

**Files:**
- Modify: `oddish/src/oddish/schemas.py` (append request/response models)
- Create: `backend/api/routers/prompts.py`
- Modify: `backend/api/app.py` (import + `include_router` in the `create_app()` block, ~lines 278–320)
- Test: `backend/tests/test_prompts_router.py` (ASGITransport + dependency_overrides, mirrors `backend/tests/test_notifications_router.py`)

**Interfaces:**
- Consumes: `set_prompt_core`, `list_prompts_core`, `get_prompt_core`, `list_prompt_versions_core`, `activate_prompt_version_core`
- Produces routes: `GET /prompts`, `GET /prompts/{key}` (opt `?version=`), `GET /prompts/{key}/versions`, `PUT /prompts/{key}`, `POST /prompts/{key}/activate`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_prompts_router.py
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import prompts as prompts_router
from auth import APIKeyScope, AuthContext, AuthMethod, require_auth


class _FakeVersion:
    def __init__(self, version, content):
        self.version = version
        self.content = content
        self.created_at = __import__("datetime").datetime.now()
        self.created_by = None


class _FakePrompt:
    id = "p1"
    key = "pre_trial_qa"
    description = "d"
    active_version = 1
    created_at = __import__("datetime").datetime.now()
    updated_at = __import__("datetime").datetime.now()


@contextlib.asynccontextmanager
async def _ctx(_):
    yield None


def _auth(scopes):
    def _factory():
        return AuthContext(
            method=AuthMethod.API_KEY, org_id="org_1", user_id="u1", scopes=scopes
        )
    return _factory


async def _call(method, path, monkeypatch, *, scopes=(APIKeyScope.READ,), **kwargs):
    async def fake_get(session):
        return _FakePrompt(), _FakeVersion(1, "hello")

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "get_prompt_core", fake_get)
    app = create_app()
    app.dependency_overrides[require_auth] = _auth(list(scopes))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_get_prompt_returns_active_content(monkeypatch):
    resp = await _call("GET", "/prompts/pre_trial_qa", monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "pre_trial_qa"
    assert body["content"] == "hello"


@pytest.mark.asyncio
async def test_put_requires_write_scope(monkeypatch):
    # READ-only scope must be rejected on write
    resp = await _call(
        "PUT", "/prompts/pre_trial_qa", monkeypatch,
        scopes=(APIKeyScope.READ,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_prompts_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'api.routers.prompts'`.

- [ ] **Step 3a: Add schemas**

Append to `oddish/src/oddish/schemas.py` (reuse existing `BaseModel`, `datetime` imports at the top of the file):

```python
class PromptVersionResponse(BaseModel):
    version: int
    content: str
    created_at: datetime
    created_by: str | None = None
    model_config = {"from_attributes": True}


class PromptResponse(BaseModel):
    id: str
    key: str
    description: str
    active_version: int | None = None
    created_at: datetime
    updated_at: datetime
    content: str | None = None  # resolved active/selected version content
    model_config = {"from_attributes": True}


class PromptSetRequest(BaseModel):
    content: str
    description: str | None = None
    activate: bool = True


class PromptActivateRequest(BaseModel):
    version: int
```

- [ ] **Step 3b: Add the router**

```python
# backend/api/routers/prompts.py
"""CRUD + versioning endpoints for the analyzer prompt registry.

Thin wrapper over ``oddish.core.prompts``: authenticate, open a session,
delegate, commit, serialize."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.prompts import (
    activate_prompt_version_core,
    get_prompt_core,
    list_prompt_versions_core,
    list_prompts_core,
    set_prompt_core,
)
from oddish.db import get_session
from oddish.schemas import (
    PromptActivateRequest,
    PromptResponse,
    PromptSetRequest,
    PromptVersionResponse,
)

router = APIRouter()


def _to_response(prompt, version) -> PromptResponse:
    resp = PromptResponse.model_validate(prompt)
    resp.content = version.content if version is not None else None
    return resp


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompts = await list_prompts_core(session)
        return [PromptResponse.model_validate(p) for p in prompts]


@router.get("/prompts/{key}", response_model=PromptResponse)
async def get_prompt(
    key: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, key, version=version)
        return _to_response(prompt, ver)


@router.get("/prompts/{key}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    key: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        versions = await list_prompt_versions_core(session, key)
        return [PromptVersionResponse.model_validate(v) for v in versions]


@router.put("/prompts/{key}", response_model=PromptResponse)
async def set_prompt(
    key: str,
    data: PromptSetRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await set_prompt_core(
            session,
            key=key,
            content=data.content,
            description=data.description,
            activate=data.activate,
            created_by=auth.user_id,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(session, key)
        return _to_response(prompt, ver)


@router.post("/prompts/{key}/activate", response_model=PromptResponse)
async def activate_prompt(
    key: str,
    data: PromptActivateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await activate_prompt_version_core(session, key, data.version)
        await session.commit()
        prompt, ver = await get_prompt_core(session, key)
        return _to_response(prompt, ver)
```

- [ ] **Step 3c: Register the router**

In `backend/api/app.py`, add `prompts` to the `from api.routers import (...)` tuple (~line 278) and add `api.include_router(prompts.router)` alongside the others (~line 301–320).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_prompts_router.py -v`
Expected: PASS (2 passed). If `AuthContext(...)`/`require_scope` signature differs, align the test's `_auth` factory with `backend/tests/test_notifications_router.py`'s auth-override helper before implementing.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/schemas.py backend/api/routers/prompts.py \
        backend/api/app.py backend/tests/test_prompts_router.py
git commit -m "feat(api): /prompts registry router"
```

---

### Task 5: `oddish prompt` CLI sub-app

**Files:**
- Create: `oddish/src/oddish/cli/prompt.py`
- Modify: `oddish/src/oddish/cli/__init__.py` (import + `add_typer`)
- Test: `oddish/tests/cli/test_prompt_cli.py` (Typer `CliRunner` + monkeypatched httpx)

**Interfaces:**
- Consumes: `get_api_url`, `get_auth_headers`, `require_api_key` from `oddish.cli.config`
- Produces sub-app `prompt_app` with commands: `list`, `get`, `set`, `versions`, `activate`, `diff`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/cli/test_prompt_cli.py
import json

import httpx
from typer.testing import CliRunner

from oddish.cli.prompt import prompt_app

runner = CliRunner()


def _fake_client(monkeypatch, *, method, url_substr, status=200, payload=None):
    calls = {}

    class _Resp:
        status_code = status
        text = json.dumps(payload or {})

        def json(self):
            return payload or {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            calls["url"] = url
            return _Resp()

        def put(self, url, **k):
            calls["url"] = url
            calls["json"] = k.get("json")
            return _Resp()

        def post(self, url, **k):
            calls["url"] = url
            return _Resp()

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)
    return calls


def test_get_prints_content(monkeypatch):
    _fake_client(monkeypatch, method="get", url_substr="/prompts/pre_trial_qa",
                 payload={"key": "pre_trial_qa", "content": "HELLO"})
    result = runner.invoke(prompt_app, ["get", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "HELLO" in result.stdout


def test_set_reads_file_and_puts(monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, method="put", url_substr="/prompts/pre_trial_qa",
                         payload={"key": "pre_trial_qa", "active_version": 2})
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["set", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"
    assert calls["url"].endswith("/prompts/pre_trial_qa")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/cli/test_prompt_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oddish.cli.prompt'`.

- [ ] **Step 3a: Write the CLI**

```python
# oddish/src/oddish/cli/prompt.py
from __future__ import annotations

import difflib
import json as _json
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
prompt_app = typer.Typer(
    help="Manage versioned analyzer prompts.", no_args_is_help=True
)


def _resolve(api_url: str | None) -> str:
    url = api_url or get_api_url()
    require_api_key(url)
    return url


def _fail(resp: httpx.Response) -> None:
    console.print(f"[red]Failed ({resp.status_code}):[/red] {resp.text}")
    raise typer.Exit(1)


@prompt_app.command("list")
def list_prompts(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """List all registered prompts."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts")
    if resp.status_code != 200:
        _fail(resp)
    for p in resp.json():
        console.print(f"{p['key']:32}  v{p.get('active_version')}  {p.get('description','')}")


@prompt_app.command("get")
def get_prompt(
    key: str,
    version: Annotated[Optional[int], typer.Option("--version", "-v")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print a prompt's content (active version by default)."""
    url = _resolve(api_url)
    params = {"version": version} if version is not None else {}
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{key}", params=params)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if json_output:
        console.print_json(_json.dumps(data))
    else:
        console.print(data.get("content", ""))


@prompt_app.command("set")
def set_prompt(
    key: str,
    file: Annotated[Path, typer.Option("--file", "-f", help="File with prompt content.")],
    description: Annotated[Optional[str], typer.Option("--description", "-d")] = None,
    no_activate: Annotated[bool, typer.Option("--no-activate", help="Append without activating.")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Append a new prompt version from a file (activates it by default)."""
    url = _resolve(api_url)
    content = file.read_text()
    payload: dict = {"content": content, "activate": not no_activate}
    if description is not None:
        payload["description"] = description
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.put(f"{url}/prompts/{key}", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    console.print(
        f"[green]Set {key}[/green] active_version={data.get('active_version')}"
    )


@prompt_app.command("versions")
def versions(
    key: str,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """List a prompt's versions."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{key}/versions")
    if resp.status_code != 200:
        _fail(resp)
    for v in resp.json():
        console.print(f"v{v['version']:<4} {v.get('created_at','')}  {v.get('created_by') or ''}")


@prompt_app.command("activate")
def activate(
    key: str,
    version: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Point the active version at an existing version number."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.post(f"{url}/prompts/{key}/activate", json={"version": version})
    if resp.status_code != 200:
        _fail(resp)
    console.print(f"[green]Activated {key} v{version}[/green]")


@prompt_app.command("diff")
def diff(
    key: str,
    version_a: int,
    version_b: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Unified diff between two versions of a prompt."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        ra = client.get(f"{url}/prompts/{key}", params={"version": version_a})
        rb = client.get(f"{url}/prompts/{key}", params={"version": version_b})
    for r in (ra, rb):
        if r.status_code != 200:
            _fail(r)
    a = ra.json().get("content", "").splitlines(keepends=True)
    b = rb.json().get("content", "").splitlines(keepends=True)
    for line in difflib.unified_diff(a, b, fromfile=f"{key}@v{version_a}", tofile=f"{key}@v{version_b}"):
        console.print(line.rstrip("\n"))
```

- [ ] **Step 3b: Register the sub-app**

In `oddish/src/oddish/cli/__init__.py`, add `from oddish.cli.prompt import prompt_app` to the imports and `app.add_typer(prompt_app, name="prompt")` in the registration block (next to `app.add_typer(report_app, name="report")`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/cli/test_prompt_cli.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/cli/prompt.py oddish/src/oddish/cli/__init__.py \
        oddish/tests/cli/test_prompt_cli.py
git commit -m "feat(cli): oddish prompt registry commands"
```

---

### Task 6: Seed the two analyzer prompt keys

**Files:**
- Create: `oddish/src/oddish/analyze/prompts/pre_trial_qa.v1.txt`
- Create: `oddish/src/oddish/analyze/prompts/post_trial_qa.v1.txt`
- Create: `oddish/src/oddish/core/prompt_seeds.py`
- Modify: `oddish/src/oddish/cli/prompt.py` (add a `seed` command)
- Test: `backend/tests/test_prompt_seeds.py` (real Postgres)

**Interfaces:**
- Consumes: `set_prompt_core`, `get_active_prompt_content`
- Produces:
  - `PROMPT_SEEDS: dict[str, tuple[str, str]]` — `key -> (description, content)`
  - `seed_prompts(session) -> list[str]` — idempotently creates any missing keys (only when the key does not yet exist); returns the keys it created
  - `oddish prompt seed` command

- [ ] **Step 1: Write the seed prompt files**

`oddish/src/oddish/analyze/prompts/pre_trial_qa.v1.txt` (first-draft content; iterated later via the registry — this is real, not a placeholder):

```
You are a senior benchmark-task auditor. You are auditing the SOURCE of a
Harbor task (not any trial run) for authoring defects, BEFORE agents attempt it.

Fetch the task source by running exactly this command with the Bash tool:

    oddish pull {task_id} --type task --include-task-files -o ./task_src

The files land at ./task_src/tasks/{task_id}/files/. Use Read and Glob to
inspect them. Focus on three things:

1. VERIFIER COMPLETENESS (dimension=verifier): does the grader/verifier fully
   check the stated requirements? Look for unchecked outputs, missing edge
   cases, permissive assertions, ignored stderr/exit codes.
2. ORACLE CORRECTNESS (dimension=oracle): does the reference solution / oracle
   actually satisfy the task and pass its own verifier? Look for mismatches
   between what the task asks and what the oracle does.
3. INFO LEAKAGE (dimension=info_leakage): can the agent read the oracle,
   expected outputs, or verifier internals to shortcut the task? Look for
   answers committed into the workspace, hints in comments, or the verifier
   being importable/readable by the agent.

For every issue, emit one action item with: problem_type (incompleteness or
mismatch), dimension, the exact file (task-relative path) and line_start/
line_end, a short title, a detail of what is wrong, a concrete recommendation,
and a tier (must_fix, should_fix, optional). Cite REAL line numbers you saw.
Return only the structured list of action items.
```

`oddish/src/oddish/analyze/prompts/post_trial_qa.v1.txt` (first draft; consumed in Plan C):

```
You are auditing a single trial trajectory of a Harbor task. You are given the
task's pre-trial action items (known authoring weaknesses, each with a file and
line range) and a list of which files each trajectory step inspected or wrote.

For each pre-trial action item, decide:
- exploited: did the agent's trajectory take advantage of this weakness to pass
  without genuinely solving the task? Use the file/line refs to grep the
  trajectory and the per-step file-access metadata to check whether the agent
  touched the implicated files.
- causal: did the agent's behavior happen AS A RESULT of this weakness?
- exploit_evidence: quote the step or output that shows it.
Set links_to to the pre-trial action item id you evaluated.

Then add any NEW action items you find from the trajectory itself (same schema,
source=post_trial), citing file and line where possible.
Return only the structured list of action items.
```

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_prompt_seeds.py
import pytest

from oddish.core.prompt_seeds import PROMPT_SEEDS, seed_prompts
from oddish.core.prompts import get_active_prompt_content
from oddish.db import PromptModel, get_session


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_populates_content():
    # clean slate for the seed keys
    async with get_session() as session:
        for key in PROMPT_SEEDS:
            await session.execute(PromptModel.__table__.delete().where(PromptModel.key == key))
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert set(created) == set(PROMPT_SEEDS)

    async with get_session() as session:
        # second run creates nothing
        created2 = await seed_prompts(session)
        await session.commit()
        assert created2 == []

    async with get_session() as session:
        content = await get_active_prompt_content(session, "pre_trial_qa")
        assert "VERIFIER COMPLETENESS" in content
```

- [ ] **Step 3: Write the seed module + CLI command**

```python
# oddish/src/oddish/core/prompt_seeds.py
"""Seed content for the built-in analyzer prompts. Idempotent: only creates a
key when it is absent, so operator edits made via the registry are never
clobbered."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.prompts import set_prompt_core
from oddish.db import PromptModel

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "analyze" / "prompts"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text()


PROMPT_SEEDS: dict[str, tuple[str, str]] = {
    "pre_trial_qa": (
        "Pre-trial QA auditor: verifier completeness, oracle correctness, info leakage.",
        _load("pre_trial_qa.v1.txt"),
    ),
    "post_trial_qa": (
        "Post-trial QA: exploited/causal assessment + new trajectory action items.",
        _load("post_trial_qa.v1.txt"),
    ),
}


async def seed_prompts(session: AsyncSession) -> list[str]:
    created: list[str] = []
    for key, (description, content) in PROMPT_SEEDS.items():
        existing = await session.execute(
            select(PromptModel.id).where(PromptModel.key == key)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        await set_prompt_core(
            session, key=key, content=content, description=description
        )
        created.append(key)
    return created
```

Add a `seed` command to `oddish/src/oddish/cli/prompt.py`:

```python
@prompt_app.command("seed")
def seed(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Create any missing built-in prompts from their seed content."""
    from oddish.core.prompt_seeds import PROMPT_SEEDS

    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        for key, (description, content) in PROMPT_SEEDS.items():
            got = client.get(f"{url}/prompts/{key}")
            if got.status_code == 200:
                console.print(f"[dim]{key}: exists, skipping[/dim]")
                continue
            resp = client.put(
                f"{url}/prompts/{key}",
                json={"content": content, "description": description, "activate": True},
            )
            if resp.status_code != 200:
                _fail(resp)
            console.print(f"[green]Seeded {key}[/green]")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_prompt_seeds.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/prompts/pre_trial_qa.v1.txt \
        oddish/src/oddish/analyze/prompts/post_trial_qa.v1.txt \
        oddish/src/oddish/core/prompt_seeds.py \
        oddish/src/oddish/cli/prompt.py \
        backend/tests/test_prompt_seeds.py
git commit -m "feat: seed pre_trial_qa and post_trial_qa prompts"
```

---

## Self-Review

**Spec coverage (Component 1 + Component 5 of the design):**
- Shared `ActionItem` schema with taxonomy + tier + post-trial linkage fields → Task 1. ✓
- `prompts`/`prompt_versions` tables, append-immutable versions, active pointer → Tasks 2–3. ✓
- Blocks record `prompt_key`+`version` → the `get_active_prompt_content` helper (Task 3) is the read seam; the actual recording happens in Plan B when the block runs (noted, out of scope here). ✓ (deferred, by design)
- Backend `/prompts` router + registration → Task 4. ✓
- `oddish prompt list/get/set/versions/activate/diff` → Task 5; `seed` → Task 6. ✓
- Seed `pre_trial_qa` + `post_trial_qa` → Task 6. ✓
- Components 2, 3, 4, 6, 7 (pre-trial block, post-trial linkage, trajectory metadata, persistence, frontend) → Plans B/C/D, out of scope. ✓

**Placeholder scan:** The only intentional fill-in is `down_revision = "PARENT_REVISION"` in Task 2, which has an explicit command (`uv run alembic heads`) and decision procedure to resolve it — required because the head is environment-state, not authorable in advance. Seed prompt text is real first-draft content, not a placeholder.

**Type consistency:** `set_prompt_core` / `get_prompt_core` / `get_active_prompt_content` / `activate_prompt_version_core` signatures are identical across Tasks 3, 4, 6. `ActionItem` field names (Task 1) match the design schema. Response models expose `content` (Task 4) consumed by the CLI `get`/`diff` (Task 5).

**Known follow-ups for Plan B:** add `prompt_key`/`prompt_version` columns to `analyzer_blocks` and record them when a block loads its prompt via `get_active_prompt_content`.
