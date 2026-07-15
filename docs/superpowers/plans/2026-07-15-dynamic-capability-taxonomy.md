# Dynamic Capability Taxonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the analyzer's good-bucket capability taxonomy out of hardcoded prompt fragments and into Postgres, seeded with 15 capabilities across 5 failure categories, so agents can propose new capabilities that a human promotes.

**Architecture:** A pure `Taxonomy` dataclass tree renders the map-phase rubric; the DB is read by the caller and passed down, preserving `prompt_builder.py`'s no-I/O guarantee. `capability_slug` is added to `Finding` as a **new, LLM-assigned** field, leaving `bucketing.py`'s deterministic `subcategory` derivation untouched. Agent-authored proposals ride out on `AnalyzerEvalOutput.proposals` and are persisted by the existing shielded `_store()`.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Postgres (JSONB), pytest.

## Global Constraints

- **Branch:** `feat/capability-taxonomy`, based on `origin/main`. Never commit to `main`. (Note: there is no local `main` branch in this repo — only `origin/main`. Use `origin/main` as the diff base.)
- **Spec:** `docs/superpowers/specs/2026-07-15-dynamic-capability-taxonomy-design.md`.
- **Alembic head is `analyzers_006`** (verified with `uv run alembic heads` — the repo has exactly one head; do not trust a grep, several migrations use typed `down_revision: Union[...] = "..."` declarations that naive regexes miss).
- **Migrations follow `analyzers_001`'s style:** every DDL step in its own `_autocommit()` block, idempotent (`IF NOT EXISTS`), `SET lock_timeout = '8s'` first.
- **`prompt_builder.py` must remain pure** — no I/O beyond reading its own packaged `prompts/` templates. Its module docstring promises this.
- **`bucketing.py` must not change.** It is the regression gate for the orthogonality decision.
- **Slugs are immutable once promoted.** `name` is the editable display field.
- **Taxonomy is global** — no `org_id` on any new table.
- Tests: `pytest` from `oddish/` or `backend/`.
- Commit messages end with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## File Structure

| File | Responsibility |
|---|---|
| `oddish/src/oddish/evals/analyzer/taxonomy.py` | **Create.** `Category`/`Capability`/`Taxonomy` dataclasses, rubric rendering, fingerprint, snapshot round-trip. Pure. |
| `oddish/src/oddish/evals/analyzer/prompts/fragments/map_rubric.txt` | **Modify.** Static bad half + `{capabilities_block}` placeholder. |
| `oddish/src/oddish/evals/analyzer/prompts/fragments/map_output.txt` | **Modify.** Add `capability_slug` + `capability_proposal`. |
| `oddish/src/oddish/evals/analyzer/prompts/sections/universal_capabilities_content.txt` | **Modify.** Organize by category then capability. |
| `oddish/src/oddish/evals/analyzer/prompt_builder.py` | **Modify.** `map_rubric(taxonomy)`; thread taxonomy through `build_map_prompt`. |
| `oddish/src/oddish/evals/analyzer/schemas.py` | **Modify.** `Finding.capability_slug`; `AnalyzerEvalOutput.proposals` + taxonomy fields. |
| `oddish/src/oddish/db/models.py` | **Modify.** 4 new tables + 2 `analyzers` columns. |
| `oddish/src/oddish/db/taxonomy_query.py` | **Create.** `load_taxonomy(session)` — the only DB→`Taxonomy` reader. |
| `oddish/alembic/versions/captax_001_add_capability_taxonomy.py` | **Create.** DDL + seed. |
| `oddish/src/oddish/workers/queue/analyzer_handler.py` | **Modify.** `_store()` persists proposals + snapshot. |
| `backend/api/services/cc_chat/analyzer_prompt.py` | **Modify.** Take `taxonomy`, render rubric, add proposal instructions. |
| `backend/api/services/cc_chat/analyzer_parse.py` | **Modify.** Return `(findings, sections, proposals)`. |
| `backend/api/services/cc_chat/analyzer_cohort.py` | **Modify.** Thread `taxonomy` in, `proposals` out. |
| `backend/worker/analyzer_sandbox.py` | **Modify.** Load taxonomy, pass down, collect proposals. |
| `backend/scripts/promote_capability.py` | **Create.** The human review surface. |

---

### Task 1: Taxonomy dataclasses, rubric rendering, fingerprint

**Files:**
- Create: `oddish/src/oddish/evals/analyzer/taxonomy.py`
- Test: `oddish/tests/evals/analyzer/test_taxonomy.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Category(slug, name, description="", sort_order=0)` frozen dataclass
  - `Capability(slug, name, description, example="", primary_category="", extra_categories=())` frozen dataclass
  - `Taxonomy(categories=(), capabilities=())` frozen dataclass, with
    `Taxonomy.by_category() -> list[tuple[Category, list[Capability]]]`
  - `render_capabilities(taxonomy: Taxonomy) -> str`
  - `taxonomy_fingerprint(taxonomy: Taxonomy) -> str` (12-hex-char sha256)
  - `taxonomy_snapshot(taxonomy: Taxonomy) -> dict`
  - `taxonomy_from_snapshot(d: dict) -> Taxonomy`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/evals/analyzer/test_taxonomy.py
from oddish.evals.analyzer.taxonomy import (
    Capability,
    Category,
    Taxonomy,
    render_capabilities,
    taxonomy_fingerprint,
    taxonomy_from_snapshot,
    taxonomy_snapshot,
)


def _tax() -> Taxonomy:
    return Taxonomy(
        categories=(
            Category("verification", "Verification failures", "stops early", 0),
            Category("tool", "Tool failures", "wrong tool", 1),
        ),
        capabilities=(
            Capability(
                "agent-early-stop", "Agent Early Stop",
                "Stops early and assumes it's done.",
                "apex-swe: patches then solves without verifying.",
                primary_category="verification",
            ),
            Capability(
                "tool-selection-error", "Tool Selection Error",
                "Picks the familiar-but-wrong tool.",
                "APEX agents pick curl over MCP.",
                primary_category="tool",
                extra_categories=("verification",),
            ),
        ),
    )


def test_by_category_groups_on_primary_only():
    """extra_categories must NOT duplicate a capability into a second group --
    that is exactly the double-count is_primary exists to prevent."""
    groups = _tax().by_category()
    assert [c.slug for c, _ in groups] == ["verification", "tool"]
    assert [x.slug for x in groups[0][1]] == ["agent-early-stop"]
    assert [x.slug for x in groups[1][1]] == ["tool-selection-error"]


def test_by_category_orders_by_sort_order():
    tax = Taxonomy(
        categories=(Category("b", "B", "", 5), Category("a", "A", "", 1)),
        capabilities=(),
    )
    assert [c.slug for c, _ in tax.by_category()] == ["a", "b"]


def test_render_capabilities_includes_slug_name_description_example():
    out = render_capabilities(_tax())
    assert "verification — Verification failures" in out
    assert "agent-early-stop" in out
    assert "Stops early and assumes it's done." in out
    assert "apex-swe: patches then solves without verifying." in out


def test_render_capabilities_shows_extra_categories_as_cross_reference():
    out = render_capabilities(_tax())
    assert "also: verification" in out


def test_fingerprint_is_stable_and_content_sensitive():
    a = taxonomy_fingerprint(_tax())
    assert a == taxonomy_fingerprint(_tax())
    assert len(a) == 12
    changed = Taxonomy(
        categories=_tax().categories,
        capabilities=_tax().capabilities[:1],
    )
    assert taxonomy_fingerprint(changed) != a


def test_snapshot_round_trips():
    tax = _tax()
    assert taxonomy_from_snapshot(taxonomy_snapshot(tax)) == tax
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/evals/analyzer/test_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oddish.evals.analyzer.taxonomy'`

- [ ] **Step 3: Write minimal implementation**

```python
# oddish/src/oddish/evals/analyzer/taxonomy.py
"""The capability taxonomy as pure data.

Loaded from Postgres by ``oddish.db.taxonomy_query`` and passed down; nothing
here touches a session. Keeping it pure is what lets ``prompt_builder`` render
the rubric without acquiring the I/O its docstring promises it does not do.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    description: str = ""
    sort_order: int = 0


@dataclass(frozen=True)
class Capability:
    slug: str
    name: str
    description: str
    example: str = ""
    primary_category: str = ""
    # Non-primary tags. Deliberately excluded from by_category() grouping:
    # counting a capability under every tag would make category totals exceed
    # num_good_failures. These render as cross-references instead.
    extra_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[Category, ...] = ()
    capabilities: tuple[Capability, ...] = ()

    def by_category(self) -> list[tuple[Category, list[Capability]]]:
        ordered = sorted(self.categories, key=lambda c: (c.sort_order, c.slug))
        return [
            (cat, [c for c in self.capabilities if c.primary_category == cat.slug])
            for cat in ordered
        ]


def render_capabilities(taxonomy: Taxonomy) -> str:
    lines: list[str] = []
    for cat, caps in taxonomy.by_category():
        if not caps:
            continue
        lines.append(f"### {cat.slug} — {cat.name}")
        for c in caps:
            extra = (
                f"   (also: {', '.join(c.extra_categories)})"
                if c.extra_categories else ""
            )
            lines.append(f"  {c.slug} — {c.name}{extra}")
            lines.append(f"      {c.description}")
            if c.example:
                lines.append(f"      e.g. {c.example}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def taxonomy_snapshot(taxonomy: Taxonomy) -> dict:
    return {
        "categories": [
            {"slug": c.slug, "name": c.name, "description": c.description,
             "sort_order": c.sort_order}
            for c in taxonomy.categories
        ],
        "capabilities": [
            {"slug": c.slug, "name": c.name, "description": c.description,
             "example": c.example, "primary_category": c.primary_category,
             "extra_categories": list(c.extra_categories)}
            for c in taxonomy.capabilities
        ],
    }


def taxonomy_from_snapshot(d: dict) -> Taxonomy:
    return Taxonomy(
        categories=tuple(Category(**c) for c in d.get("categories", [])),
        capabilities=tuple(
            Capability(
                slug=c["slug"], name=c["name"], description=c["description"],
                example=c.get("example", ""),
                primary_category=c.get("primary_category", ""),
                extra_categories=tuple(c.get("extra_categories", [])),
            )
            for c in d.get("capabilities", [])
        ),
    )


def taxonomy_fingerprint(taxonomy: Taxonomy) -> str:
    """Short content hash, stored as ``analyzers.taxonomy_version``.

    sort_keys makes it order-independent, so a row reshuffle does not read as a
    taxonomy change.
    """
    blob = json.dumps(taxonomy_snapshot(taxonomy), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/evals/analyzer/test_taxonomy.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/taxonomy.py oddish/tests/evals/analyzer/test_taxonomy.py
git commit -m "$(cat <<'EOF'
feat(analyzer): pure Taxonomy dataclasses with rubric rendering

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: DB models, migration, and seed

**Files:**
- Modify: `oddish/src/oddish/db/models.py`
- Create: `oddish/alembic/versions/captax_001_add_capability_taxonomy.py`
- Test: `oddish/tests/db/test_capability_taxonomy_model.py`

**Interfaces:**
- Consumes: nothing from Task 1 (DDL only).
- Produces: `CapabilityCategoryModel`, `CapabilityModel`, `CapabilityCategoryTagModel`, `CapabilityProposalModel`; `AnalyzerModel.taxonomy_version`, `AnalyzerModel.taxonomy_snapshot`.

**Note:** these models subclass `Base` **directly**, not `TimestampedMixin` — the mixin forces an `id` PK, and these use `slug` / composite PKs. `models.py`'s `Base` docstring calls out this exact case (`QueueSlotModel`).

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/db/test_capability_taxonomy_model.py
from oddish.db.models import (
    AnalyzerModel,
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
)


def test_tables_and_columns():
    assert CapabilityCategoryModel.__tablename__ == "capability_categories"
    assert {"slug", "name", "description", "sort_order",
            "created_at", "updated_at", "deleted_at"} <= set(
        CapabilityCategoryModel.__table__.columns.keys())

    assert CapabilityModel.__tablename__ == "capabilities"
    assert {"slug", "name", "description", "example",
            "created_at", "updated_at", "deleted_at"} <= set(
        CapabilityModel.__table__.columns.keys())

    assert CapabilityCategoryTagModel.__tablename__ == "capability_category_tags"
    assert {"capability_slug", "category_slug", "is_primary"} <= set(
        CapabilityCategoryTagModel.__table__.columns.keys())

    assert CapabilityProposalModel.__tablename__ == "capability_proposals"
    assert {"id", "slug_suggestion", "name", "description", "example",
            "category_slugs", "analyzer_id", "trial_ids", "trajectory_link",
            "status", "promoted_capability_slug", "created_at",
            "reviewed_at", "reviewed_by"} <= set(
        CapabilityProposalModel.__table__.columns.keys())


def test_taxonomy_is_global_no_org_scoping():
    """The taxonomy is global by design -- an org_id here would silently make
    cross-org capability counts incomparable."""
    for m in (CapabilityModel, CapabilityCategoryModel):
        assert "org_id" not in m.__table__.columns.keys()


def test_tag_primary_key_is_composite():
    pk = {c.name for c in CapabilityCategoryTagModel.__table__.primary_key}
    assert pk == {"capability_slug", "category_slug"}


def test_analyzer_gains_taxonomy_snapshot_columns():
    cols = set(AnalyzerModel.__table__.columns.keys())
    assert {"taxonomy_version", "taxonomy_snapshot"} <= cols


def test_capability_tables_are_registered_for_soft_delete():
    """A deleted_at column does nothing on its own -- the session filter only
    applies to classes passed to register_soft_delete_models. Unregistered,
    retiring a capability would set the tombstone and load_taxonomy would keep
    rendering it into the rubric anyway."""
    from oddish.db.soft_delete import _SOFT_DELETE_MODELS  # registry

    assert CapabilityModel in _SOFT_DELETE_MODELS
    assert CapabilityCategoryModel in _SOFT_DELETE_MODELS
```

**Note:** confirm the registry's real attribute name by reading
`oddish/src/oddish/db/soft_delete.py` before writing this test — assert against
whatever `register_soft_delete_models` actually populates. If it exposes no
inspectable registry, assert the behavior instead: soft-delete a `CapabilityModel`
row, then confirm a plain `select(CapabilityModel)` does not return it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_capability_taxonomy_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'CapabilityCategoryModel'`

- [ ] **Step 3a: Add the models**

Append to `oddish/src/oddish/db/models.py` (after `AnalyzerModel`):

```python
class CapabilityCategoryModel(Base):
    """A top-level failure category. Curated by hand -- agents may propose
    capabilities but never categories, so that the 5-way lens stays fixed."""

    __tablename__ = "capability_categories"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CapabilityModel(Base):
    """One capability in the live taxonomy.

    ``slug`` is the PK rather than a surrogate id: findings reference
    capabilities by slug and the map agent emits slugs, so an integer id would
    force a lookup the agent cannot perform. Slugs are immutable once promoted --
    renaming one orphans every historical finding that cites it. Edit ``name``.
    """

    __tablename__ = "capabilities"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CapabilityCategoryTagModel(Base):
    """capability <-> category many-to-many.

    ``is_primary`` is what keeps category rollups an exact partition: grouping
    on every tag would count a multi-tagged capability once per tag, so category
    totals would exceed num_good_failures. Exactly one primary per capability,
    enforced by a partial unique index.
    """

    __tablename__ = "capability_category_tags"
    __table_args__ = (
        Index(
            "uq_capability_primary_category",
            "capability_slug",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    capability_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("capabilities.slug", ondelete="CASCADE"),
        primary_key=True,
    )
    category_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("capability_categories.slug", ondelete="CASCADE"),
        primary_key=True,
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CapabilityProposalModel(Base):
    """An agent-authored capability awaiting human review.

    ``category_slugs`` is JSONB rather than a join table because a proposal is a
    draft that may name categories which do not exist yet -- an FK would reject
    exactly the rows most worth seeing. It hardens into
    ``capability_category_tags`` rows at promotion.
    """

    __tablename__ = "capability_proposals"
    __table_args__ = (
        Index(
            "uq_capability_proposal_per_analyzer",
            "analyzer_id",
            "slug_suggestion",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    slug_suggestion: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_slugs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    analyzer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trial_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    trajectory_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    promoted_capability_slug: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Add to `AnalyzerModel` (immediately after the `breakdown` column):

```python
    # The taxonomy this run classified against. Without it, editing a capability
    # silently rewrites the meaning of every historical breakdown.
    taxonomy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    taxonomy_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**Register the two soft-deletable tables.** `models.py` ends (~line 1994) with an
explicit `register_soft_delete_models(...)` call. A `deleted_at` column does
nothing unless the class is in that list — the session filter is what applies
`WHERE deleted_at IS NULL`. Add both:

```python
register_soft_delete_models(
    ExperimentModel,
    AnalyzerModel,
    ...
    DocumentModel,
    CapabilityModel,
    CapabilityCategoryModel,
)
```

`CapabilityCategoryTagModel` and `CapabilityProposalModel` are **not** registered
— neither has a `deleted_at`. Tags are hard-deleted (retagging is not history
worth keeping) and proposals carry `status` instead, which is their audit trail.

- [ ] **Step 3b: Write the migration**

```python
# oddish/alembic/versions/captax_001_add_capability_taxonomy.py
"""add capability taxonomy tables and seed the initial 15 capabilities

Each DDL step runs in its own autocommit transaction and is idempotent,
mirroring analyzers_001. The seed uses ON CONFLICT DO NOTHING so a re-run is a
no-op and hand-edits made after the first upgrade are never clobbered.
"""

import sqlalchemy as sa
from alembic import op

revision = "captax_001"
down_revision = "analyzers_006"
branch_labels = None
depends_on = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


_CATEGORIES = [
    ("verification", "Verification failures",
     "The agent stops early and assumes it is done.", 0),
    ("perception", "Perception failures",
     "Relevant signal is present, but context is mismanaged.", 1),
    ("tool", "Tool failures", "Wrong tools, or the tool to be used is OOD.", 2),
    ("world-knowledge", "World knowledge gaps",
     "Cannot connect layers, couple faults, or work outside Python.", 3),
    ("long-horizon", "Long-horizon failures",
     "Planning, context propagation, and implementation break down over time.", 4),
]

# (slug, name, description, example, primary_category)
_CAPABILITIES = [
    ("agent-early-stop", "Agent Early Stop",
     "The agent stops early and assumes it's done; declares done without "
     "checking outputs against reality.",
     "apex-swe: models patch the fix then issue a solve without verifying. "
     "DevOps-Gym: models reach premature conclusions.",
     "verification"),
    ("hypothesis-fixation", "Hypothesis Fixation",
     "The model fixates on its initial approach or first plausible cause, "
     "generating no competing hypotheses and discarding contradicting evidence.",
     "SREGym \"greedy approach\"; valkey requirepass case; gc/GOGC case; "
     "APEX \"single-source reliance.\"",
     "verification"),
    ("specification-non-compliance", "Specification Non-Compliance",
     "Fixates on the natural-language description instead of extracting hard "
     "constraints.",
     "APEX \"Specification Non-Compliance\" 14%; DevOps-Gym Maven migration: "
     "\"253 tests completed, 88 failed.\"",
     "verification"),
    ("volume-mismanagement", "Volume Mismanagement",
     "Truncation ignored, unfiltered queries, context exhaustion, lossy "
     "overflow summarization.",
     "APEX \"Bad Context Handling\" 38% (DeepSeek 16,515-char one-shot); "
     "DevOps-Gym context-exhaustion case.",
     "perception"),
    ("temporal-mismanagement", "Temporal Mismanagement",
     "Cannot sustain attention on an evolving stream; wrong sampling policy.",
     "DevOps-Gym \"inadequate monitoring methodology\" 37%, \"insufficient "
     "temporal granularity\" 11%; distraction onto past observations.",
     "perception"),
    ("narrow-evidence-gathering", "Narrow Evidence Gathering",
     "Single-source diagnosis, missing triangulation.",
     "APEX single-source (6%); SREGym noise-induced tunnel vision.",
     "perception"),
    ("tool-selection-error", "Tool Selection Error",
     "Picks the familiar-but-wrong tool over the correct provided one.",
     "APEX agents pick HTTP curl over MCP.",
     "tool"),
    ("brittle-tool-mechanics", "Brittle Tool Mechanics",
     "Malformed calls, argument-format errors, retry cascades that burn the "
     "budget.",
     "APEX \"Execution Failure\" 12%/16%; Grok 4 str_replace (>1/3 fail); "
     "DeepSeek view_file (~1/2 fail); SREGym \"Infrastructure Failure\" 18%.",
     "tool"),
    ("ood-tool-unfamiliarity", "OOD Tool Unfamiliarity",
     "Lacks the causal model to even look in the right place.",
     "",
     "tool"),
    ("cross-layer-reasoning-gap", "Cross-Layer Reasoning Gap",
     "Can't connect hardware/OS/control-plane to application symptoms.",
     "SREGym hardware faults (0% characterization); control-plane-vs-data-plane "
     "confusion.",
     "world-knowledge"),
    ("coupled-fault-reasoning", "Coupled / Compound-Fault Reasoning",
     "Can't reason about interacting causes.",
     "SREGym metastable (trigger x constraint); concurrent/correlated failures.",
     "world-knowledge"),
    ("non-python-degradation", "Non-Python Degradation",
     "Outside Python, the agent performs poorly on agentic tasks.",
     "DevOps-Gym Java/Go collapse (70.4% -> 23.87%); APEX Java/C++ 0%.",
     "world-knowledge"),
    ("multi-step-planning-collapse", "Multi-Step Planning Collapse",
     "Fixes one error, loses track of the rest, terminates early.",
     "DevOps-Gym \"multi-step reasoning\" 23%; APEX \"stalling without progress.\"",
     "long-horizon"),
    ("cross-stage-context-propagation", "Cross-Stage Context Propagation",
     "Can't carry context downstream to multi-stage faults.",
     "DevOps-Gym E2E 0%.",
     "long-horizon"),
    ("implementation-breakdown", "Implementation Breakdown",
     "Compile/import/syntax errors block an otherwise-correct solution.",
     "APEX Observability \"Execution Failure\" (import/compile/syntax); "
     "DevOps-Gym compiled-language difficulty.",
     "long-horizon"),
]


def upgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")

    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS capability_categories (
            slug VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS capabilities (
            slug VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            example TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS capability_category_tags (
            capability_slug VARCHAR(64) NOT NULL
                REFERENCES capabilities(slug) ON DELETE CASCADE,
            category_slug VARCHAR(64) NOT NULL
                REFERENCES capability_categories(slug) ON DELETE CASCADE,
            is_primary BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (capability_slug, category_slug)
        )
        """
    )
    _autocommit(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_capability_primary_category ON capability_category_tags "
        "(capability_slug) WHERE is_primary"
    )
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS capability_proposals (
            id VARCHAR(64) PRIMARY KEY,
            slug_suggestion VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            example TEXT,
            category_slugs JSONB,
            analyzer_id VARCHAR(64),
            trial_ids JSONB,
            trajectory_link TEXT,
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            promoted_capability_slug VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_at TIMESTAMPTZ,
            reviewed_by VARCHAR(64)
        )
        """
    )
    _autocommit(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_capability_proposal_per_analyzer ON capability_proposals "
        "(analyzer_id, slug_suggestion)"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_capability_proposals_analyzer_id ON capability_proposals (analyzer_id)"
    )

    _autocommit(
        "ALTER TABLE analyzers ADD COLUMN IF NOT EXISTS "
        "taxonomy_version VARCHAR(64)"
    )
    _autocommit(
        "ALTER TABLE analyzers ADD COLUMN IF NOT EXISTS taxonomy_snapshot JSONB"
    )

    # Seed is DML, so it runs in the migration's own transaction with bind
    # parameters -- matching skills_seed_directives_001_seed.py. Only the DDL
    # above needs the autocommit blocks. ON CONFLICT DO NOTHING makes a re-run a
    # no-op and never clobbers a hand-edit made after the first upgrade.
    bind = op.get_bind()
    for slug, name, desc, order in _CATEGORIES:
        bind.execute(
            sa.text(
                "INSERT INTO capability_categories "
                "(slug, name, description, sort_order) "
                "VALUES (:slug, :name, :description, :sort_order) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"slug": slug, "name": name, "description": desc, "sort_order": order},
        )
    for slug, name, desc, example, cat in _CAPABILITIES:
        bind.execute(
            sa.text(
                "INSERT INTO capabilities (slug, name, description, example) "
                "VALUES (:slug, :name, :description, :example) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"slug": slug, "name": name, "description": desc, "example": example},
        )
        bind.execute(
            sa.text(
                "INSERT INTO capability_category_tags "
                "(capability_slug, category_slug, is_primary) "
                "VALUES (:slug, :cat, true) "
                "ON CONFLICT (capability_slug, category_slug) DO NOTHING"
            ),
            {"slug": slug, "cat": cat},
        )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("ALTER TABLE analyzers DROP COLUMN IF EXISTS taxonomy_snapshot")
    _autocommit("ALTER TABLE analyzers DROP COLUMN IF EXISTS taxonomy_version")
    _autocommit("DROP TABLE IF EXISTS capability_proposals")
    _autocommit("DROP TABLE IF EXISTS capability_category_tags")
    _autocommit("DROP TABLE IF EXISTS capabilities")
    _autocommit("DROP TABLE IF EXISTS capability_categories")
```

- [ ] **Step 4: Run tests + verify the chain stays single-headed**

Run: `cd oddish && uv run pytest tests/db/test_capability_taxonomy_model.py -v && uv run alembic heads`
Expected: PASS (4 tests), and `alembic heads` prints exactly `captax_001 (head)`.
If it prints two heads, `down_revision` is wrong — it must be `analyzers_006`.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/alembic/versions/captax_001_add_capability_taxonomy.py oddish/tests/db/test_capability_taxonomy_model.py
git commit -m "$(cat <<'EOF'
feat(db): capability taxonomy tables seeded with 15 capabilities

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Taxonomy loader (DB → Taxonomy)

**Files:**
- Create: `oddish/src/oddish/db/taxonomy_query.py`
- Test: `oddish/tests/db/test_taxonomy_query.py`

**Interfaces:**
- Consumes: `Taxonomy`/`Capability`/`Category` (Task 1); models (Task 2).
- Produces: `async def load_taxonomy(session) -> Taxonomy`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/db/test_taxonomy_query.py
import pytest

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
)
from oddish.db.taxonomy_query import load_taxonomy


@pytest.mark.asyncio
async def test_load_taxonomy_builds_primary_and_extra_tags(session):
    session.add_all([
        CapabilityCategoryModel(slug="verification", name="Verification failures",
                                description="d", sort_order=0),
        CapabilityCategoryModel(slug="tool", name="Tool failures",
                                description="d", sort_order=1),
        CapabilityModel(slug="agent-early-stop", name="Agent Early Stop",
                        description="Stops early.", example="apex-swe."),
        CapabilityModel(slug="tool-selection-error", name="Tool Selection Error",
                        description="Wrong tool.", example="curl over MCP."),
    ])
    await session.flush()
    session.add_all([
        CapabilityCategoryTagModel(capability_slug="agent-early-stop",
                                   category_slug="verification", is_primary=True),
        CapabilityCategoryTagModel(capability_slug="tool-selection-error",
                                   category_slug="tool", is_primary=True),
        CapabilityCategoryTagModel(capability_slug="tool-selection-error",
                                   category_slug="verification", is_primary=False),
    ])
    await session.flush()

    tax = await load_taxonomy(session)

    assert [c.slug for c in tax.categories] == ["verification", "tool"]
    by_slug = {c.slug: c for c in tax.capabilities}
    assert by_slug["agent-early-stop"].primary_category == "verification"
    assert by_slug["agent-early-stop"].extra_categories == ()
    assert by_slug["tool-selection-error"].primary_category == "tool"
    assert by_slug["tool-selection-error"].extra_categories == ("verification",)


@pytest.mark.asyncio
async def test_load_taxonomy_skips_untagged_capability(session):
    """A capability with no primary tag cannot be grouped, so it must not reach
    the rubric -- it would render under no category and be unpickable."""
    session.add(CapabilityModel(slug="orphan", name="Orphan", description="d"))
    await session.flush()
    tax = await load_taxonomy(session)
    assert [c.slug for c in tax.capabilities] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_taxonomy_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oddish.db.taxonomy_query'`

- [ ] **Step 3: Write minimal implementation**

```python
# oddish/src/oddish/db/taxonomy_query.py
"""The single DB -> Taxonomy reader.

Everything downstream (prompt rendering, snapshotting) takes a Taxonomy value,
so this is the only place that needs a session. Keeping it alone here is what
lets prompt_builder stay pure.
"""

from __future__ import annotations

from sqlalchemy import select

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
)
from oddish.evals.analyzer.taxonomy import Capability, Category, Taxonomy


async def load_taxonomy(session) -> Taxonomy:
    cat_rows = (
        await session.execute(
            select(CapabilityCategoryModel).order_by(
                CapabilityCategoryModel.sort_order, CapabilityCategoryModel.slug
            )
        )
    ).scalars().all()
    cap_rows = (
        await session.execute(select(CapabilityModel).order_by(CapabilityModel.slug))
    ).scalars().all()
    tag_rows = (
        await session.execute(select(CapabilityCategoryTagModel))
    ).scalars().all()

    primary: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    for t in tag_rows:
        if t.is_primary:
            primary[t.capability_slug] = t.category_slug
        else:
            extra.setdefault(t.capability_slug, []).append(t.category_slug)

    capabilities = tuple(
        Capability(
            slug=c.slug,
            name=c.name,
            description=c.description,
            example=c.example or "",
            primary_category=primary[c.slug],
            extra_categories=tuple(sorted(extra.get(c.slug, []))),
        )
        # An untagged capability has no group to render under, so it would be
        # invisible-but-pickable in the rubric. Drop it rather than half-show it.
        for c in cap_rows
        if c.slug in primary
    )
    return Taxonomy(
        categories=tuple(
            Category(slug=c.slug, name=c.name, description=c.description or "",
                     sort_order=c.sort_order)
            for c in cat_rows
        ),
        capabilities=capabilities,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/db/test_taxonomy_query.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/db/taxonomy_query.py oddish/tests/db/test_taxonomy_query.py
git commit -m "$(cat <<'EOF'
feat(db): load_taxonomy reads the live taxonomy into a pure value

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Render the taxonomy into the map rubric

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/prompts/fragments/map_rubric.txt`
- Modify: `oddish/src/oddish/evals/analyzer/prompts/fragments/map_output.txt`
- Modify: `oddish/src/oddish/evals/analyzer/prompt_builder.py:48,86`
- Modify: `oddish/src/oddish/evals/analyzer/core.py:109`
- Modify: `backend/scripts/haiku_sandbox_bad_failures.py:186`
- Test: `oddish/tests/evals/analyzer/test_prompt_builder.py`

**Interfaces:**
- Consumes: `Taxonomy`, `render_capabilities` (Task 1).
- Produces:
  - `map_rubric(taxonomy: Taxonomy) -> str`
  - `build_map_prompt(bundle, subanalysis, roster, taxonomy) -> str`

`map_rubric()` gains a **required** argument. All 3 call sites must be updated: `prompt_builder.py:86`, `backend/api/services/cc_chat/analyzer_prompt.py:95` (Task 5), and `backend/scripts/haiku_sandbox_bad_failures.py:186`.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/evals/analyzer/test_prompt_builder.py`:

```python
from oddish.evals.analyzer.prompt_builder import map_rubric
from oddish.evals.analyzer.taxonomy import Capability, Category, Taxonomy


def _tiny_tax() -> Taxonomy:
    return Taxonomy(
        categories=(Category("verification", "Verification failures", "d", 0),),
        capabilities=(
            Capability("agent-early-stop", "Agent Early Stop",
                       "Stops early.", "apex-swe.", primary_category="verification"),
        ),
    )


def test_map_rubric_keeps_the_bad_half_static():
    """1a/1b stay hardcoded -- they classify task defects, which the classifier
    Subtype enum already pins. Only the good half is DB-driven."""
    out = map_rubric(_tiny_tax())
    assert "1a = task ambiguity / specification" in out
    assert "1b = task security / construction" in out


def test_map_rubric_renders_db_capabilities_not_3a3b3c():
    out = map_rubric(_tiny_tax())
    assert "agent-early-stop" in out
    assert "verification — Verification failures" in out
    assert "3a = problem identification" not in out


def test_map_rubric_tells_the_agent_how_to_propose():
    out = map_rubric(_tiny_tax())
    assert "capability_proposal" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/evals/analyzer/test_prompt_builder.py -k map_rubric -v`
Expected: FAIL — `TypeError: map_rubric() takes 0 positional arguments but 1 was given`

- [ ] **Step 3a: Rewrite `map_rubric.txt`**

```text
Bad bucket:
  1a = task ambiguity / specification
  1b = task security / construction

Good bucket (universal capabilities) — pick the ONE capability slug that best
fits the failure, and put it in `capability_slug`:

{capabilities_block}

If NONE of the capabilities above fit, author a new one: set `capability_slug`
to a new kebab-case slug of your own and fill in `capability_proposal` with its
name, a one-sentence description, an example citing this trial, and the
category slug(s) it belongs under. Only propose when nothing fits — a
near-match is better than a duplicate.
```

- [ ] **Step 3b: Rewrite `map_output.txt`**

```text
{{"trial_id": "...", "bucket": "bad|good", "subcategory": "1a|1b|3a|3b|3c|emergent:<label>",
 "capability_slug": "good bucket only: the capability slug this failure shows",
 "capability_proposal": {{"name": "...", "description": "...", "example": "...",
   "categories": ["<category-slug>"]}},
 "evidence_quote": "verbatim quote from the trajectory", "step_ids": [<ints>],
 "root_cause": "1-2 sentences", "headroom_signal": "for good trials: what capability, if
 improved, would fix this; else empty", "trajectory_link": "{trajectory_link}"}}

Omit `capability_proposal` entirely unless you are authoring a NEW capability.
```

- [ ] **Step 3c: Update `prompt_builder.py`**

Replace `map_rubric` (line 48) and its call site (line 86):

```python
def map_rubric(taxonomy: Taxonomy) -> str:
    """Render the rubric. Pure: the taxonomy is fetched by the caller.

    The bad half stays static in the template -- 1a/1b classify task defects,
    which the classifier's Subtype enum already pins. Only the good half is
    DB-driven.
    """
    template = (_FRAGMENTS_DIR / "map_rubric.txt").read_text().rstrip("\n")
    return template.replace("{capabilities_block}", render_capabilities(taxonomy))
```

Add the import at the top:

```python
from oddish.evals.analyzer.taxonomy import Taxonomy, render_capabilities
```

Change `build_map_prompt`'s signature and its `rubric_block`:

```python
def build_map_prompt(
    bundle: TrajectoryBundle, subanalysis: SubAnalysis, roster: list[dict],
    taxonomy: Taxonomy,
) -> str:
    return MAP_PROMPT_TEMPLATE.format(
        ...
        rubric_block=map_rubric(taxonomy),
        ...
    )
```

**Note:** `.replace()` not `.format()` — `map_output.txt` carries literal `{{ }}` braces and the rubric template must not be format-parsed alongside them.

- [ ] **Step 3d: Update the two other call sites**

`oddish/src/oddish/evals/analyzer/core.py:109` — thread a `taxonomy` parameter into the enclosing function and pass it:

```python
            prompt = build_map_prompt(bundle, sa, roster, taxonomy)
```

`backend/scripts/haiku_sandbox_bad_failures.py:186`:

```python
        .replace("{rubric_block}", map_rubric(taxonomy))
```

- [ ] **Step 4: Run tests**

Run: `cd oddish && uv run pytest tests/evals/analyzer/ -v`
Expected: PASS. Pre-existing `test_prompt_builder.py` tests that call `build_map_prompt` need the new 4th argument — update them to pass `_tiny_tax()`.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/ backend/scripts/haiku_sandbox_bad_failures.py oddish/tests/evals/analyzer/test_prompt_builder.py
git commit -m "$(cat <<'EOF'
feat(analyzer): render the map rubric from a DB-loaded taxonomy

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `Finding.capability_slug` and proposal parsing

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/schemas.py`
- Modify: `backend/api/services/cc_chat/analyzer_parse.py`
- Modify: `backend/api/services/cc_chat/analyzer_prompt.py:95`
- Test: `backend/tests/cc_chat/test_analyzer_parse.py`

**Interfaces:**
- Consumes: `Taxonomy` (Task 1).
- Produces:
  - `Finding.capability_slug: str | None`
  - `CapabilityProposal` dataclass: `(slug, name, description, example, categories, trial_ids, trajectory_link)`
  - `parse_cohort_result(...) -> tuple[list[Finding], dict[str, str], list[CapabilityProposal]]` — **note the new third element; Task 6 consumes it**
  - `build_map_batch_prompt(bucket, batch, roster, oracle_by_trial, batch_no, batch_total, tail_bytes, taxonomy)`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/cc_chat/test_analyzer_parse.py`:

```python
GOOD_LINKS = {"good-1": _host("good-1", classification="GOOD_FAILURE", subtype="3a"),
              "good-2": _host("good-2", classification="GOOD_FAILURE", subtype="3a")}


def _good_finding(trial_id="good-1", **over) -> dict:
    return {
        "trial_id": trial_id, "bucket": "good", "subcategory": "3a",
        "capability_slug": "agent-early-stop",
        "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
        "headroom_signal": "hs", "trajectory_link": "JUNK", **over,
    }


def test_capability_slug_is_kept_from_the_model():
    """Unlike trajectory_link, there is no host fact to override this with --
    only the map agent can tell these capabilities apart."""
    b = (json.dumps(_good_finding()) + "\n").encode()
    findings, _, _ = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), b, "", GOOD_LINKS)
    assert findings[0].capability_slug == "agent-early-stop"


def test_proposal_is_lifted_off_the_finding():
    prop = {"name": "Hypothesis Fixation", "description": "d", "example": "e",
            "categories": ["verification"]}
    b = (json.dumps(_good_finding(capability_slug="hypothesis-fixation",
                                  capability_proposal=prop)) + "\n").encode()
    findings, _, proposals = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), b, "", GOOD_LINKS)
    assert len(proposals) == 1
    assert proposals[0].slug == "hypothesis-fixation"
    assert proposals[0].categories == ["verification"]
    assert proposals[0].trial_ids == ["good-1"]
    # The proposal is not a per-trial fact; storing it on Finding would
    # duplicate it across every citing trial.
    assert not hasattr(findings[0], "capability_proposal")


def test_duplicate_proposals_across_batches_merge_by_slug():
    """Every MAP batch is a fresh claude process, so batch 2 has no memory that
    batch 1 already proposed this. Blind insert would make one row per batch."""
    prop = {"name": "Hypothesis Fixation", "description": "d", "example": "e",
            "categories": ["verification"]}
    lines = "".join(
        json.dumps(_good_finding(t, capability_slug="hypothesis-fixation",
                                 capability_proposal=prop)) + "\n"
        for t in ("good-1", "good-2")
    ).encode()
    _, _, proposals = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), lines, "",
        GOOD_LINKS)
    assert len(proposals) == 1
    assert proposals[0].trial_ids == ["good-1", "good-2"]


def test_bad_bucket_proposals_are_dropped():
    """The bad bucket classifies task defects, not agent capabilities. Gate
    structurally so prompt drift cannot leak one in."""
    prop = {"name": "Nope", "description": "d", "example": "e",
            "categories": ["verification"]}
    b = (json.dumps({**_finding(), "capability_slug": "nope",
                     "capability_proposal": prop}) + "\n").encode()
    findings, _, proposals = parse_cohort_result(
        "bad", json.dumps({"bad_failure_content": "# B"}).encode(), b, "", LINKS)
    assert proposals == []
    assert findings[0].capability_slug is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/cc_chat/test_analyzer_parse.py -k capability -v`
Expected: FAIL — `ValueError: too many values to unpack (expected 2)`

- [ ] **Step 3a: Add `capability_slug` to `Finding` and the proposal dataclass**

In `oddish/src/oddish/evals/analyzer/schemas.py`, add to `Finding` (after `subcategory`):

```python
    # LLM-assigned, good bucket only. Deliberately NOT derived host-side:
    # capabilities are finer-grained than the classifier's Subtype enum, so
    # nothing but the map agent reading the trajectory can distinguish e.g.
    # hypothesis-fixation from narrow-evidence-gathering (both are 3a today).
    # Plain text, not an FK -- a proposed slug has no row yet, and leaving it
    # unconstrained is what makes promotion reclassify history with no backfill.
    capability_slug: str | None = None
```

Add the new dataclass:

```python
@dataclass
class CapabilityProposal:
    """An agent-authored capability, pending human review.

    Not carried on Finding: a proposal is not a per-trial fact, and storing it
    there would duplicate one proposal across every trial that cited it.
    """

    slug: str
    name: str
    description: str
    example: str = ""
    categories: list[str] = field(default_factory=list)
    trial_ids: list[str] = field(default_factory=list)
    trajectory_link: str = ""
```

- [ ] **Step 3b: Parse proposals in `analyzer_parse.py`**

Add `capability_slug` to `_finding_from`'s constructor call, gated on bucket:

```python
        # Good bucket only: the bad bucket classifies task defects, not agent
        # capabilities. Gating here (not in the prompt) means drift can't leak.
        capability_slug=(d.get("capability_slug") or None) if bucket == "good" else None,
```

Add the proposal collector and widen the return:

```python
def _proposals_from(
    text: str, bucket: str, host_by_trial: dict[str, dict]
) -> list[CapabilityProposal]:
    """Merge by slug. Each MAP batch is a fresh process with no memory of the
    previous one, so the same capability arrives once per batch that saw it."""
    if bucket != "good":
        return []
    merged: dict[str, CapabilityProposal] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        raw = d.get("capability_proposal")
        slug = d.get("capability_slug")
        trial_id = d.get("trial_id", "")
        if not isinstance(raw, dict) or not slug or trial_id not in host_by_trial:
            continue
        existing = merged.get(slug)
        if existing is None:
            merged[slug] = CapabilityProposal(
                slug=slug,
                name=raw.get("name", slug),
                description=raw.get("description", ""),
                example=raw.get("example", ""),
                categories=list(raw.get("categories") or []),
                trial_ids=[trial_id],
                trajectory_link=host_by_trial[trial_id]["trajectory_link"],
            )
        elif trial_id not in existing.trial_ids:
            existing.trial_ids.append(trial_id)
    return list(merged.values())
```

In `parse_cohort_result`, decode the findings text once, reuse it, and return three values:

```python
    findings_text = findings_bytes.decode("utf-8", "replace")
    findings = _findings_from_jsonl(findings_text, bucket, host_by_trial)
    proposals = _proposals_from(findings_text, bucket, host_by_trial)
    ...
    return findings, sections, proposals
```

Import `CapabilityProposal` from `oddish.evals.analyzer.schemas`.

- [ ] **Step 3c: Thread the taxonomy into `analyzer_prompt.py`**

`build_map_batch_prompt` and `build_cohort_prompt` gain a `taxonomy: Taxonomy` parameter, and `map_rubric()` becomes `map_rubric(taxonomy)` at line 95 and line 208.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/cc_chat/ -v`
Expected: PASS. Existing `parse_cohort_result` callers in tests unpack 2 values — update them to 3.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/schemas.py backend/api/services/cc_chat/analyzer_parse.py backend/api/services/cc_chat/analyzer_prompt.py backend/tests/cc_chat/test_analyzer_parse.py
git commit -m "$(cat <<'EOF'
feat(analyzer): parse capability_slug and lift agent-authored proposals

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Carry proposals to the shielded persist

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/schemas.py` (`AnalyzerEvalOutput`)
- Modify: `backend/api/services/cc_chat/analyzer_cohort.py:~90,~200`
- Modify: `backend/worker/analyzer_sandbox.py`
- Modify: `oddish/src/oddish/workers/queue/analyzer_handler.py:336-360`
- Test: `backend/tests/test_analyzer_sandbox_eval.py`

**Interfaces:**
- Consumes: `CapabilityProposal`, 3-tuple `parse_cohort_result` (Task 5); `load_taxonomy` (Task 3); `taxonomy_fingerprint`/`taxonomy_snapshot` (Task 1).
- Produces: `AnalyzerEvalOutput.proposals: list[CapabilityProposal]`, `.taxonomy_version: str | None`, `.taxonomy_snapshot: dict | None`.

**Why here, not in `analyzer_sandbox.py`:** the spec says the write happens in the worker module, but `sandbox_eval_rows` takes no session — it returns an `AnalyzerEvalOutput` and `_store()` in `analyzer_handler.py` does the actual persist under `asyncio.shield`. Findings already ride that path as `output.findings`. Proposals follow it identically, which also makes them work for the core API eval strategy, not just the sandbox one.

- [ ] **Step 1: Write the failing test**

The existing `patched` fixture stubs creds/CLI/sandboxes and its `fake_run_cohort`
returns a **2-tuple**. Widen it to a 3-tuple (that change is itself part of this
task — the old shape no longer type-checks against `run_cohort`), and stub
`load_taxonomy` so the unit needs no live DB.

Note the import root: `backend/pyproject.toml` sets `pythonpath = ["."]`, so it
is `from worker import ...`, **not** `from backend.worker import ...`.

```python
# backend/tests/test_analyzer_sandbox_eval.py

# 1. In the `patched` fixture, widen both fake_run_cohort returns to 3-tuples:
#      return [_finding("bad-1", "bad")], {"bad_failure_content": "# Bad"}, []
#    ... and for the good bucket, return [] as the third element too.
#
# 2. Stub the taxonomy read inside the same fixture:
#
#    from oddish.evals.analyzer.taxonomy import Capability, Category, Taxonomy
#
#    _TAX = Taxonomy(
#        categories=(Category("verification", "Verification failures", "d", 0),),
#        capabilities=(Capability("agent-early-stop", "Agent Early Stop",
#                                 "Stops early.", "apex-swe.",
#                                 primary_category="verification"),),
#    )
#
#    async def fake_load_taxonomy(session):
#        return _TAX
#
#    monkeypatch.setattr(m, "load_taxonomy", fake_load_taxonomy)
#
#    @asynccontextmanager
#    async def fake_get_session():
#        yield None
#
#    monkeypatch.setattr(m, "get_session", fake_get_session)

# 3. Append these tests:

from oddish.evals.analyzer.schemas import CapabilityProposal


async def test_sandbox_eval_carries_proposals_out_on_the_output(patched, monkeypatch):
    """Proposals must ride out on the output: sandbox_eval_rows holds no
    session, and _store() is the only thing that writes."""
    from worker import analyzer_sandbox as m

    prop = CapabilityProposal(
        slug="hypothesis-fixation", name="Hypothesis Fixation", description="d",
        categories=["verification"], trial_ids=["good-1"],
    )

    async def fake_run_cohort(client, runtime, *, bucket, cohort, **kw):
        if bucket == "bad":
            return [_finding("bad-1", "bad")], {"bad_failure_content": "# Bad"}, []
        return (
            [_finding("good-1", "good")],
            {"good_failure_content": "# Good",
             "universal_capabilities_content": "# Caps",
             "headroom_analysis": "# Head"},
            [prop],
        )

    monkeypatch.setattr(m, "run_cohort", fake_run_cohort)
    rows = [(_trial("bad-1", "BAD_FAILURE"), "tasks/t1"),
            (_trial("good-1", "GOOD_FAILURE"), "tasks/t1")]
    out = await m.sandbox_eval_rows(rows, AnalyzerEvalConfig(), "an-1")

    assert out.proposals == [prop]


async def test_sandbox_eval_snapshots_the_taxonomy_it_ran_against(patched):
    """Without the snapshot, editing a capability silently rewrites the meaning
    of this run's breakdown after the fact."""
    from worker import analyzer_sandbox as m

    rows = [(_trial("bad-1", "BAD_FAILURE"), "tasks/t1")]
    out = await m.sandbox_eval_rows(rows, AnalyzerEvalConfig(), "an-1")

    assert len(out.taxonomy_version) == 12
    assert [c["slug"] for c in out.taxonomy_snapshot["capabilities"]] == [
        "agent-early-stop"
    ]
```

`_trial` is whatever row-builder the existing fixture already uses to feed
`_gather`; reuse it verbatim rather than inventing a second one.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_sandbox_eval.py -k proposals -v`
Expected: FAIL — `AttributeError: 'AnalyzerEvalOutput' object has no attribute 'proposals'`

- [ ] **Step 3a: Widen `AnalyzerEvalOutput`**

```python
    # Agent-authored capabilities awaiting review. Ride the same path as
    # findings: the eval strategies hold no session, _store() persists.
    proposals: list[CapabilityProposal] = field(default_factory=list)
    # The taxonomy this run classified against. Without it, editing a capability
    # silently rewrites the meaning of every historical breakdown.
    taxonomy_version: str | None = None
    taxonomy_snapshot: dict | None = None
```

- [ ] **Step 3b: `run_cohort` returns proposals**

Its return type becomes `tuple[list[Finding], dict[str, str], list[CapabilityProposal]]`; it takes a `taxonomy: Taxonomy` kwarg and forwards it to `build_map_batch_prompt`. The final line becomes:

```python
    findings, sections, proposals = parse_cohort_result(...)
    logger.info("%s done: %d findings, %d proposals, sections=%s",
                tag, len(findings), len(proposals), sorted(sections))
    return findings, sections, proposals
```

- [ ] **Step 3c: `sandbox_eval_rows` loads the taxonomy and collects proposals**

```python
from oddish.db.connection import get_session
from oddish.db.taxonomy_query import load_taxonomy
from oddish.evals.analyzer.taxonomy import taxonomy_fingerprint, taxonomy_snapshot

    async with get_session() as session:
        taxonomy = await load_taxonomy(session)
```

Pass `taxonomy=taxonomy` into `run_cohort`, then collect:

```python
    proposals: list[CapabilityProposal] = []
    for (bucket, cohort), (cohort_findings, cohort_sections, cohort_props) in zip(
        jobs, results
    ):
        ...
        proposals.extend(cohort_props)
```

and widen the return:

```python
    return AnalyzerEvalOutput(
        sections=sections, findings=findings, counts=counts, breakdown=breakdown,
        subanalyses=subs, proposals=proposals,
        taxonomy_version=taxonomy_fingerprint(taxonomy),
        taxonomy_snapshot=taxonomy_snapshot(taxonomy),
    )
```

The zero-work early return also sets `taxonomy_version`/`taxonomy_snapshot` — a run that found no failures still ran against a taxonomy.

- [ ] **Step 3d: `_store()` persists them**

In `oddish/src/oddish/workers/queue/analyzer_handler.py`, after `analyzer.models_by_task = output.models_by_task`:

```python
                analyzer.taxonomy_version = output.taxonomy_version
                analyzer.taxonomy_snapshot = output.taxonomy_snapshot
                for p in output.proposals:
                    # Idempotent on (analyzer_id, slug_suggestion): _store runs
                    # under asyncio.shield and a retried job re-enters here.
                    await session.execute(
                        pg_insert(CapabilityProposalModel)
                        .values(
                            id=generate_id(), slug_suggestion=p.slug, name=p.name,
                            description=p.description, example=p.example,
                            category_slugs=p.categories, analyzer_id=analyzer_id,
                            trial_ids=p.trial_ids,
                            trajectory_link=p.trajectory_link, status="PENDING",
                        )
                        .on_conflict_do_nothing(
                            index_elements=["analyzer_id", "slug_suggestion"]
                        )
                    )
```

Imports: `from sqlalchemy.dialects.postgresql import insert as pg_insert` and `CapabilityProposalModel`, `generate_id` from `oddish.db.models`.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/ -v && cd ../oddish && uv run pytest tests/ -v`
Expected: PASS. **`tests/evals/analyzer/test_bucketing.py` must pass untouched** — it is the regression gate proving `subcategory` derivation did not change.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/schemas.py oddish/src/oddish/workers/queue/analyzer_handler.py backend/api/services/cc_chat/analyzer_cohort.py backend/worker/analyzer_sandbox.py backend/tests/test_analyzer_sandbox_eval.py
git commit -m "$(cat <<'EOF'
feat(analyzer): persist capability proposals and the taxonomy snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Reduce organizes by category then capability

**Files:**
- Modify: `oddish/src/oddish/evals/analyzer/prompts/sections/universal_capabilities_content.txt`
- Test: `oddish/tests/evals/test_section_fragments.py`

**Interfaces:**
- Consumes: nothing new. `sections_block()` reads this file unchanged.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/evals/test_section_fragments.py (append)
from oddish.evals.analyzer.prompt_builder import section_brief


def test_universal_capabilities_organizes_by_category_then_capability():
    brief = section_brief("universal_capabilities_content")
    assert "category" in brief.lower()
    assert "capability" in brief.lower()
    # 3a/3b/3c is no longer the good-bucket frame.
    assert "3a" not in brief


def test_universal_capabilities_mentions_proposed_bucket():
    """Findings citing a not-yet-promoted slug resolve to no live capability;
    they must land somewhere visible rather than vanish."""
    assert "Proposed capabilities" in section_brief("universal_capabilities_content")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/evals/test_section_fragments.py -k universal -v`
Expected: FAIL — `assert "3a" not in brief`

- [ ] **Step 3: Rewrite the section brief**

```text
- universal_capabilities_content: organize the good failures by failure
  category, then by capability within each category. Use one `## <Category
  name>` heading per category that has findings, in the order the rubric listed
  them, and one `### <Capability name>` subheading under it. Cite every claim
  with the finding's trajectory_link verbatim. If a capability carries
  cross-reference categories, note them inline ("also: long-horizon") rather
  than repeating the capability under a second heading. Group findings whose
  capability_slug was newly proposed (not in the rubric) under a final
  `## Proposed capabilities` heading.
```

- [ ] **Step 4: Run tests**

Run: `cd oddish && uv run pytest tests/evals/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/evals/analyzer/prompts/sections/universal_capabilities_content.txt oddish/tests/evals/test_section_fragments.py
git commit -m "$(cat <<'EOF'
feat(analyzer): reduce organizes good failures by category then capability

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Promotion script (the human review surface)

**Files:**
- Create: `backend/scripts/promote_capability.py`
- Test: `backend/tests/test_promote_capability.py`

**Interfaces:**
- Consumes: models (Task 2).
- Produces: `list_pending()`, `promote(proposal_id, primary_category, extra_categories=(), slug=None)`, `reject(proposal_id, merge_into=None)`.

**Scope note:** the spec names a human review step but no surface. This is the minimal one — a script. A dashboard review UI is deliberately **out of scope** and belongs in its own plan.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_promote_capability.py
import pytest

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
)
from scripts.promote_capability import promote, reject  # backend/ is the import root


@pytest.mark.asyncio
async def test_promote_creates_capability_and_primary_tag(session):
    session.add(CapabilityCategoryModel(slug="verification", name="V", sort_order=0))
    session.add(CapabilityProposalModel(
        id="p1", slug_suggestion="hypothesis-fixation", name="Hypothesis Fixation",
        description="d", example="e", category_slugs=["verification"],
        analyzer_id="an-1", trial_ids=["good-1"], status="PENDING"))
    await session.flush()

    await promote(session, "p1", primary_category="verification")

    cap = await session.get(CapabilityModel, "hypothesis-fixation")
    assert cap is not None and cap.name == "Hypothesis Fixation"
    tag = await session.get(
        CapabilityCategoryTagModel, ("hypothesis-fixation", "verification"))
    assert tag.is_primary is True
    prop = await session.get(CapabilityProposalModel, "p1")
    assert prop.status == "PROMOTED"
    assert prop.promoted_capability_slug == "hypothesis-fixation"


@pytest.mark.asyncio
async def test_reject_with_merge_target_records_the_survivor(session):
    """Findings citing the rejected slug resolve to the merge target -- that is
    the whole reason capability_slug is not an FK."""
    session.add(CapabilityProposalModel(
        id="p2", slug_suggestion="early-stop", name="Early Stop", description="d",
        analyzer_id="an-1", status="PENDING"))
    await session.flush()

    await reject(session, "p2", merge_into="agent-early-stop")

    prop = await session.get(CapabilityProposalModel, "p2")
    assert prop.status == "REJECTED"
    assert prop.promoted_capability_slug == "agent-early-stop"
    assert await session.get(CapabilityModel, "early-stop") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_promote_capability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.scripts.promote_capability'`

- [ ] **Step 3: Write the implementation**

```python
# backend/scripts/promote_capability.py
"""Review agent-authored capability proposals.

The human half of propose-and-promote. Nothing reaches the live rubric without
passing through here: parse_cohort_result overrides the model wherever a host
fact exists, but a proposal's content is model-authored by definition and has no
host fact to check against. This review is what stands in for that override.

Run from backend/ (pyproject sets pythonpath = ["."]):
    uv run python -m scripts.promote_capability list
    uv run python -m scripts.promote_capability promote p1 --category verification
    uv run python -m scripts.promote_capability reject p2 --merge-into agent-early-stop
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from oddish.db.connection import get_session
from oddish.db.models import (
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
    utcnow,
)


async def list_pending(session) -> list[CapabilityProposalModel]:
    return list((await session.execute(
        select(CapabilityProposalModel)
        .where(CapabilityProposalModel.status == "PENDING")
        .order_by(CapabilityProposalModel.created_at)
    )).scalars().all())


async def promote(
    session, proposal_id: str, *, primary_category: str,
    extra_categories: tuple[str, ...] = (), slug: str | None = None,
    reviewed_by: str = "cli",
) -> str:
    prop = await session.get(CapabilityProposalModel, proposal_id)
    if prop is None:
        raise ValueError(f"no proposal {proposal_id!r}")
    target = slug or prop.slug_suggestion
    if await session.get(CapabilityModel, target) is None:
        session.add(CapabilityModel(
            slug=target, name=prop.name, description=prop.description,
            example=prop.example,
        ))
        await session.flush()
    session.add(CapabilityCategoryTagModel(
        capability_slug=target, category_slug=primary_category, is_primary=True))
    for extra in extra_categories:
        session.add(CapabilityCategoryTagModel(
            capability_slug=target, category_slug=extra, is_primary=False))
    prop.status = "PROMOTED"
    prop.promoted_capability_slug = target
    prop.reviewed_at = utcnow()
    prop.reviewed_by = reviewed_by
    await session.flush()
    return target


async def reject(
    session, proposal_id: str, *, merge_into: str | None = None,
    reviewed_by: str = "cli",
) -> None:
    prop = await session.get(CapabilityProposalModel, proposal_id)
    if prop is None:
        raise ValueError(f"no proposal {proposal_id!r}")
    prop.status = "REJECTED"
    # Doubles as the merge target: findings citing the rejected slug resolve to
    # the survivor. Without one they stay orphaned and roll up as unclassified.
    prop.promoted_capability_slug = merge_into
    prop.reviewed_at = utcnow()
    prop.reviewed_by = reviewed_by
    await session.flush()


async def _main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("promote")
    p.add_argument("proposal_id")
    p.add_argument("--category", required=True)
    p.add_argument("--also", nargs="*", default=[])
    p.add_argument("--slug")
    r = sub.add_parser("reject")
    r.add_argument("proposal_id")
    r.add_argument("--merge-into")
    args = ap.parse_args()

    async with get_session() as session:
        if args.cmd == "list":
            for x in await list_pending(session):
                print(f"{x.id}  {x.slug_suggestion:36s} {x.name}")
                print(f"      {x.description}")
                print(f"      categories={x.category_slugs} trials={x.trial_ids}")
        elif args.cmd == "promote":
            print("promoted ->", await promote(
                session, args.proposal_id, primary_category=args.category,
                extra_categories=tuple(args.also), slug=args.slug))
        else:
            await reject(session, args.proposal_id, merge_into=args.merge_into)
            print("rejected", args.proposal_id)


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/test_promote_capability.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/promote_capability.py backend/tests/test_promote_capability.py
git commit -m "$(cat <<'EOF'
feat(analyzer): promote/reject CLI for agent-authored capability proposals

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] `cd oddish && uv run pytest tests/ -q` — all pass
- [ ] `cd backend && uv run pytest tests/ -q` — all pass
- [ ] `cd oddish && uv run alembic heads` — prints exactly `captax_001 (head)`
- [ ] `cd oddish && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` — migration is reversible and re-runnable
- [ ] `git diff origin/main --stat -- oddish/src/oddish/evals/analyzer/bucketing.py` — **empty**. Non-negotiable: `bucketing.py` unchanged is what proves the deterministic breakdown did not regress.
- [ ] Open a PR against `main`.
