"""add capability taxonomy tables and seed the initial 15 capabilities

Each DDL step runs in its own autocommit transaction and is idempotent,
mirroring analyzers_001. The seed uses ON CONFLICT DO NOTHING so a re-run is a
no-op and hand-edits made after the first upgrade are never clobbered.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "captax_001"
# Re-parented from analyzers_006 onto the main head (analyzers_008) at merge
# time: main extended the analyzers chain (007 blocks, 008 by_model) off the
# same 006 base, so keeping 006 as the parent would fork the graph into two
# heads. captax's tables are independent of those columns, so ordering after
# them is safe.
down_revision = "concurrency_override_001"
branch_labels = None
depends_on = None

# 000_initial_schema creates these tables via Base.metadata.create_all, whose
# created_at/updated_at use the client-side `default=utcnow` -- that emits no
# server DEFAULT, so the CREATE TABLE below is a no-op and the seed must supply
# both columns explicitly. Fixed (not now()) so re-running stays reproducible.
_TS = datetime(2026, 7, 15, tzinfo=timezone.utc)


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
                "(slug, name, description, sort_order, created_at, updated_at) "
                "VALUES (:slug, :name, :description, :sort_order, :ts, :ts) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {
                "slug": slug,
                "name": name,
                "description": desc,
                "sort_order": order,
                "ts": _TS,
            },
        )
    for slug, name, desc, example, cat in _CAPABILITIES:
        bind.execute(
            sa.text(
                "INSERT INTO capabilities "
                "(slug, name, description, example, created_at, updated_at) "
                "VALUES (:slug, :name, :description, :example, :ts, :ts) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {
                "slug": slug,
                "name": name,
                "description": desc,
                "example": example,
                "ts": _TS,
            },
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
