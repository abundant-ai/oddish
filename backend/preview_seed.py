"""Deterministic, idempotent seed for PR preview databases.

Runs in GitHub Actions after Alembic has applied both stacks to a
data-less Supabase preview branch. Populates a small curated set of rows
so the preview UI renders and the feature under review is testable,
without cloning production.

Invariants:
- Deterministic: every row sets its id and timestamps explicitly (no
  uuid4()/utcnow()), so runs and branches are byte-identical.
- Idempotent + convergent: rows are upserted (ON CONFLICT DO UPDATE) and
  any seed-owned row (id prefixed ``seed-``) absent from FIXTURES is
  deleted (reconcile), so edits and removals converge on reused branches.
- Cross-stack via reflection: the live post-Alembic schema is reflected,
  so inserts span both Alembic stacks in FK topological order without
  importing any ORM models.
"""
from __future__ import annotations

import datetime as _dt
import os

from sqlalchemy import (
    Boolean, DateTime, Float, Integer, MetaData, Numeric, String, Text, delete,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as _PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

SEED_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
SEED_ID_PREFIX = "seed-"


def _filler(column):
    """Deterministic type-based placeholder for a NOT-NULL column a fixture
    omits and that has no DB default. Keeps the seed from breaking on a
    newly-added column; the value is meaningless, so curate it in FIXTURES
    only if the column matters. Unknown types raise so the CI gate forces an
    explicit decision."""
    t = column.type
    if isinstance(t, Boolean):
        return False
    if isinstance(t, (Integer, Numeric, Float)):
        return 0
    if isinstance(t, DateTime):
        return SEED_EPOCH
    if isinstance(t, JSONB):
        return {}
    if isinstance(t, _PgUUID):
        return "00000000-0000-0000-0000-000000000000"
    if getattr(t, "enums", None):  # reflected PG ENUM
        return t.enums[0]
    if isinstance(t, (String, Text)):
        return ""
    raise RuntimeError(
        f"No auto-fill rule for {column.table.name}.{column.name} ({t!r}); "
        f"add it to FIXTURES explicitly."
    )


def _fixtures() -> dict[str, list[dict]]:
    """Curated rows keyed by table name. Every id starts with
    ``SEED_ID_PREFIX`` and every row sets created_at/updated_at = SEED_EPOCH."""
    org_id = "seed-org"
    owner_id = "seed-usr-owner"
    member_id = "seed-usr-member"
    exp_id = "seed-exp-1"
    task_a = "seed-task-a"
    task_b = "seed-task-b"
    ts = {"created_at": SEED_EPOCH, "updated_at": SEED_EPOCH}
    return {
        "organizations": [
            {"id": org_id, "name": "Preview Org", "slug": "preview-org",
             "clerk_org_id": os.environ.get("ODDISH_PREVIEW_CLERK_ORG_ID"),
             "plan": "free", "settings": {}, "is_active": True, **ts},
        ],
        "users": [
            {"id": owner_id, "org_id": org_id, "clerk_user_id": None,
             "role": "owner", "email": "owner@preview.local", "name": "Preview Owner",
             "avatar_url": None, "github_username": None, "is_active": True,
             "last_login_at": None, **ts},
            {"id": member_id, "org_id": org_id, "clerk_user_id": None,
             "role": "member", "email": "member@preview.local", "name": "Preview Member",
             "avatar_url": None, "github_username": None, "is_active": True,
             "last_login_at": None, **ts},
        ],
        "experiments": [
            {"id": exp_id, "name": "Preview Experiment", "org_id": org_id,
             "last_activity_at": SEED_EPOCH, "is_public": False, "public_token": None, **ts},
        ],
        "tasks": [
            {"id": task_a, "name": "preview-task-a", "org_id": org_id,
             "created_by_user_id": owner_id, "user": "owner@preview.local",
             "priority": "LOW", "status": "PENDING", "task_path": "preview/task-a",
             "task_s3_key": None, "tags": {}, "link": None,
             "current_version_id": None, "run_analysis": False, **ts},
            {"id": task_b, "name": "preview-task-b", "org_id": org_id,
             "created_by_user_id": owner_id, "user": "owner@preview.local",
             "priority": "LOW", "status": "COMPLETED", "task_path": "preview/task-b",
             "task_s3_key": None, "tags": {}, "link": None,
             "current_version_id": None, "run_analysis": True, **ts},
        ],
        "task_versions": [
            {"id": "seed-task-a-v1", "task_id": task_a, "version": 1,
             "task_path": "preview/task-a", "task_s3_key": None, "content_hash": None,
             "message": "v1", "created_by_user_id": owner_id,
             "expanded_at": None, "expanded_manifest_key": None, **ts},
            {"id": "seed-task-a-v2", "task_id": task_a, "version": 2,
             "task_path": "preview/task-a", "task_s3_key": None, "content_hash": None,
             "message": "v2", "created_by_user_id": owner_id,
             "expanded_at": None, "expanded_manifest_key": None, **ts},
        ],
        # task_experiments (composite PK, no id column) is handled in
        # _recompute_projections so the id-keyed engine pass doesn't touch it.
        "trials": [
            {"id": "seed-task-a-1", "name": "seed-task-a-1", "task_id": task_a,
             "task_version_id": "seed-task-a-v2", "experiment_id": exp_id,
             "org_id": org_id, "idempotency_key": None,
             "agent": "claude", "provider": "anthropic",
             "queue_key": "anthropic:claude", "model": "claude-opus-4-7",
             "timeout_minutes": 30, "environment": "preview",
             "harbor_config": {}, "is_probe": False,
             "status": "SUCCESS", "origin": "oddish",
             "attempts": 1, "max_attempts": 6,
             "harbor_stage": None, "current_worker_id": None,
             "current_queue_slot": None, "claimed_at": None,
             "heartbeat_at": None, "stale_reaped_at": None,
             "heartbeat_failure_count": 0, "last_heartbeat_error": None,
             "last_heartbeat_error_at": None,
             "started_at": SEED_EPOCH, "finished_at": SEED_EPOCH,
             "next_retry_at": None,
             "reward": 1.0, "error_message": None,
             "harbor_result_path": None, "trial_s3_key": None,
             "result": {}, "input_tokens": 100, "cache_tokens": 0,
             "output_tokens": 50, "cost_usd": 0.01,
             "phase_timing": None, "has_trajectory": False,
             "analysis": None, "analysis_status": None,
             "analysis_error": None, "analysis_started_at": None,
             "analysis_finished_at": None, "superseded_by_trial_id": None,
             **ts},
        ],
        "worker_jobs": [
            {"id": "seed-wj-trial-1", "kind": "TRIAL", "status": "SUCCESS",
             "queue_key": "anthropic:claude", "priority": 0,
             "subject_table": "trials", "subject_id": "seed-task-a-1",
             "parent_job_id": None, "payload": {},
             "attempts": 1, "max_attempts": 6,
             "next_retry_at": None, "available_after": SEED_EPOCH,
             "current_worker_id": None, "current_queue_slot": None,
             "modal_function_call_id": None,
             "claimed_at": SEED_EPOCH, "heartbeat_at": SEED_EPOCH,
             "stale_reaped_at": None, "heartbeat_failure_count": 0,
             "last_heartbeat_error": None, "last_heartbeat_error_at": None,
             "error_message": None, "result_summary": {},
             "started_at": SEED_EPOCH, "finished_at": SEED_EPOCH,
             "org_id": org_id, "provider": "anthropic",
             "external_id": None, **ts},
            {"id": "seed-wj-verdict-1", "kind": "VERDICT", "status": "QUEUED",
             "queue_key": "verdict", "priority": 0,
             "subject_table": "tasks", "subject_id": task_b,
             "parent_job_id": None, "payload": {},
             "attempts": 0, "max_attempts": 6,
             "next_retry_at": None, "available_after": SEED_EPOCH,
             "current_worker_id": None, "current_queue_slot": None,
             "modal_function_call_id": None,
             "claimed_at": None, "heartbeat_at": None,
             "stale_reaped_at": None, "heartbeat_failure_count": 0,
             "last_heartbeat_error": None, "last_heartbeat_error_at": None,
             "error_message": None, "result_summary": None,
             "started_at": None, "finished_at": None,
             "org_id": org_id, "provider": None,
             "external_id": None, **ts},
        ],
    }


# Circular FK: tasks.current_version_id -> task_versions.id. Set after both exist.
_CURRENT_VERSION = {"seed-task-a": "seed-task-a-v2"}

# FK edges to ignore when topologically sorting reflected tables. Each entry is
# a (table, column) on the dependent side whose FK target would create a cycle;
# the linkage pass in ``seed`` patches these columns up after both endpoints
# exist. Keep this list short -- prefer adding cycles to the linkage pass.
_BACKEDGES = {("tasks", "current_version_id")}


def _topo_order(md: MetaData) -> list:
    """Kahn's topological sort of reflected tables, ignoring ``_BACKEDGES``.

    SQLAlchemy's built-in ``sorted_tables`` reacts to a cycle by dropping every
    FK on every table involved, which silently misorders unrelated tables (e.g.
    pushes ``users`` after ``tasks`` because ``tasks <-> task_versions`` is
    cyclic). We only want to drop the documented back-edge.
    """
    tables = list(md.tables.values())
    deps: dict[str, set[str]] = {t.name: set() for t in tables}
    for t in tables:
        for fk in t.foreign_keys:
            col = fk.parent
            if (t.name, col.name) in _BACKEDGES:
                continue
            target = fk.column.table.name
            if target != t.name:
                deps[t.name].add(target)

    ordered: list = []
    remaining = {t.name: t for t in tables}
    while remaining:
        ready = sorted(n for n, d in deps.items() if n in remaining and not (d & set(remaining)))
        if not ready:
            # Defensive: if a real cycle remains beyond ``_BACKEDGES``, break
            # ties by name so the order stays deterministic across runs.
            ready = sorted(remaining)
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


async def seed(engine: AsyncEngine) -> None:
    fixtures = _fixtures()
    org = fixtures["organizations"][0]
    if not org["clerk_org_id"]:
        raise RuntimeError(
            "ODDISH_PREVIEW_CLERK_ORG_ID must be set so a reviewer's Clerk "
            "org matches the seeded org (else auth returns 403)."
        )
    md = MetaData()
    async with engine.begin() as conn:
        await conn.run_sync(md.reflect)
        # SQLAlchemy's ``sorted_tables`` drops *all* FK edges on tables
        # involved in a cycle (here ``tasks <-> task_versions`` via
        # ``current_version_id``), which also discards valid edges like
        # ``tasks.created_by_user_id -> users.id`` and pushes ``users``
        # after ``tasks``. Run Kahn's sort ourselves, ignoring only the
        # known back-edge; the linkage pass below sets ``current_version_id``.
        ordered = _topo_order(md)

        # Upsert parents-first, auto-filling any NOT-NULL column the fixture
        # omits that has no DB default.
        for table in ordered:
            rows = fixtures.get(table.name, [])
            if not rows:
                continue
            needs_value = [
                c for c in table.columns
                if not c.nullable and c.server_default is None and not c.primary_key
            ]
            for row in rows:
                filled = dict(row)
                for c in needs_value:
                    if c.name not in filled:
                        filled[c.name] = _filler(c)
                stmt = pg_insert(table).values(**filled)
                set_ = {k: stmt.excluded[k] for k in filled if k != "id"}
                await conn.execute(
                    stmt.on_conflict_do_update(index_elements=["id"], set_=set_)
                )

        # Linkage pass for circular FKs.
        tasks = md.tables["tasks"]
        for task_id, version_id in _CURRENT_VERSION.items():
            await conn.execute(
                tasks.update().where(tasks.c.id == task_id)
                .values(current_version_id=version_id)
            )

        # Reconcile removals: delete seed-owned rows no longer in FIXTURES,
        # children-first.
        for table in reversed(ordered):
            if table.name not in fixtures:
                continue
            keep = [r["id"] for r in fixtures[table.name]]
            await conn.execute(
                delete(table)
                .where(table.c.id.like(f"{SEED_ID_PREFIX}%"))
                .where(table.c.id.notin_(keep))
            )

        await _recompute_projections(conn, fixtures)


async def _recompute_projections(conn, fixtures: dict[str, list[dict]]) -> None:
    """Refresh derived rows/projections after the main id-keyed pass.

    Currently handles the ``task_experiments`` M2M (composite PK, no ``id``
    column) which the generic engine skips, plus the reconciliation of its
    seed-owned rows. When the tag feature (PR #239) lands this extends to call
    ``oddish.core.tags_projection.recompute_task_browse_projection`` per seeded
    task (guarded import so this stays usable without the tag tables)."""
    from sqlalchemy import MetaData as _MetaData

    md = _MetaData()
    await conn.run_sync(md.reflect)
    tx = md.tables.get("task_experiments")
    if tx is None:
        return

    # Curated M2M rows. Soft-deleted row carries deleted_at=SEED_EPOCH so the
    # gate test can assert the tombstone path is reachable in previews.
    desired = [
        {"task_id": "seed-task-a", "experiment_id": "seed-exp-1",
         "created_at": SEED_EPOCH, "deleted_at": None},
        {"task_id": "seed-task-b", "experiment_id": "seed-exp-1",
         "created_at": SEED_EPOCH, "deleted_at": SEED_EPOCH},
    ]
    for row in desired:
        stmt = pg_insert(tx).values(**row)
        set_ = {k: stmt.excluded[k] for k in row
                if k not in ("task_id", "experiment_id")}
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["task_id", "experiment_id"], set_=set_
            )
        )

    # Reconcile: drop seed-owned M2M rows no longer in `desired`. The
    # seed-ownership marker on a join table is "both endpoints are seeded".
    keep_keys = {(r["task_id"], r["experiment_id"]) for r in desired}
    res = await conn.execute(
        tx.select().where(tx.c.task_id.like(f"{SEED_ID_PREFIX}%"))
    )
    for row in res.fetchall():
        if (row.task_id, row.experiment_id) not in keep_keys:
            await conn.execute(
                delete(tx).where(tx.c.task_id == row.task_id)
                .where(tx.c.experiment_id == row.experiment_id)
            )


def _scaffold(table_name: str) -> str:
    """Print a ready-to-edit FIXTURES entry for one table, reflected from a
    migrated DB (MIGRATED_DB_URL)."""
    from sqlalchemy import create_engine

    url = os.environ["MIGRATED_DB_URL"].replace("+asyncpg", "")
    md = MetaData()
    md.reflect(bind=create_engine(url), only=[table_name])
    table = md.tables[table_name]
    out = [f'        "{table_name}": [', "            {"]
    for c in table.columns:
        if c.name == "id":
            val = f'"{SEED_ID_PREFIX}{table_name}-1"'
        elif c.name in ("created_at", "updated_at"):
            val = "SEED_EPOCH"
        elif c.nullable or c.server_default is not None:
            val = "None"
        else:
            val = "...  # required"
        out.append(f'                "{c.name}": {val},')
    out += ["            },", "        ],"]
    return "\n".join(out)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="python -m backend.preview_seed")
    ap.add_argument("--scaffold", metavar="TABLE", required=True)
    print(_scaffold(ap.parse_args().scaffold))
