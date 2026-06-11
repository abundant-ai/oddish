"""Seed PR preview databases with a faithful subset of production data.

Runs in GitHub Actions after Alembic has applied both stacks to a data-less
Supabase preview branch. The preview holds nothing but a pseudo-random
subset of real production rows -- organizations, users, experiments, tasks,
versions, trials, worker jobs, skills, documents, presets -- imported as-is,
so the preview is indistinguishable from prod apart from being smaller.

Reviewers authenticate exactly as in prod: the real ``organizations`` rows
(with their real ``clerk_org_id``) are part of the subset, so a Clerk login
resolves to the same org and the same user rows it would in production.

Invariants:
- Deterministic: the draw is ordered by ``md5(id || sample_key)`` with the
  PR number as the key, so re-runs within a PR draw the same rows while
  different PRs draw different ones.
- Idempotent + convergent: rows upsert by primary key, and the previous
  draw is recorded in a private ``_preview_seed_state`` table so rows that
  drop out of the draw (prod drift) are deleted -- without ever touching
  data a reviewer created in the preview by hand.
- Never imports runnable work: in-flight tasks/trials are normalized to
  FAILED on import and only terminal worker_jobs are taken, otherwise the
  preview's own stage-transition safety nets would enqueue real analysis /
  verdict jobs (and spend real tokens) against sampled tasks. This is the
  single deliberate deviation from prod, inherited from the old
  ``--with-data`` quiesce step.
- Never fails the whole run on one row: every upsert runs in a savepoint;
  a constraint surprise (e.g. a JIT-provisioned reviewer user already
  holding a unique email/clerk id on a reused branch) skips that row with
  a loud warning and the existing row wins.
- Cross-stack via reflection: the live post-Alembic schema is reflected, so
  inserts span both Alembic stacks in FK topological order without
  importing any ORM models, and new tables join the draw automatically.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys

from sqlalchemy import JSON, MetaData, delete, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

SEED_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

# Prod-sample sizing: a slice large enough to exercise features against
# real data and cover every data-bearing table, never the whole dataset.
SAMPLE_RECENT_EXPERIMENTS = 8
SAMPLE_RANDOM_EXPERIMENTS = 8
# Per-user coverage: every distinct experiment owner contributes their K
# most recent experiments, so per-user features (the dashboard "Mine"
# filter) work in the preview for every member, exactly as in prod.
SAMPLE_EXPERIMENTS_PER_OWNER = 3
SAMPLE_EXTRA_TASKS = 20
SAMPLE_TRIALS_PER_EXPERIMENT = 50
SAMPLE_SKILLS = 10
SAMPLE_DOCUMENTS = 10
SAMPLE_PROBE_PRESETS = 10

_TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED")
_TERMINAL_TRIAL_STATUSES = ("SUCCESS", "FAILED")
_TERMINAL_JOB_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")

# Insert order is derived from reflection (topological); reconcile order is
# this list reversed-children-first by construction.
_RECONCILED_TABLES = (
    "experiments", "tasks", "task_versions", "task_experiments", "trials",
    "worker_jobs", "skills", "skill_files", "documents", "probe_presets",
)

# FK edges to ignore when topologically sorting reflected tables; the
# linkage pass patches these columns after both endpoints exist.
_BACKEDGES = {("tasks", "current_version_id")}

# Bookkeeping table recording the previous draw, so reconcile deletes only
# rows THIS seed imported -- never reviewer-created data. Lives only on
# preview branches; prod never runs the seed.
_STATE_TABLE = "_preview_seed_state"


def _warn(message: str) -> None:
    print(f"preview_seed: {message}", file=sys.stderr)


async def sample_prod_subset(source: AsyncEngine, *, sample_key: str) -> dict:
    """Draw a deterministic pseudo-random subset of production data.

    Read-only by construction: only SELECTs are issued, and the caller
    builds the source engine with ``default_transaction_read_only=on``.

    The core closure anchors on live experiments (recent + random) plus
    extra random tasks, and walks task_experiments -> tasks -> versions ->
    trials, then pulls every user and organization of the sampled orgs so
    membership lists and authorship look exactly like prod. Coverage tables
    (worker_jobs, skills, skill_files, documents, probe_presets) are each
    guarded by a table-existence check and their own try/except, so one
    section's failure never sinks the draw.

    Returns ``{"rows": {table: [row, ...]}, "linkage": [(table, id, column,
    value), ...]}`` for :func:`seed`.
    """
    async def rows_of(conn, sql: str, **params) -> list[dict]:
        res = await conn.execute(text(sql), params)
        return [dict(r._mapping) for r in res.fetchall()]

    async def table_exists(conn, name: str) -> bool:
        res = await conn.execute(
            text("SELECT to_regclass(:qname) IS NOT NULL"),
            {"qname": f"public.{name}"},
        )
        return bool(res.scalar_one())

    rows: dict[str, list[dict]] = {}
    async with source.connect() as conn:
        # --- core closure: experiments -> links -> tasks -> versions -> trials
        exps = await rows_of(
            conn,
            "SELECT * FROM experiments"
            " WHERE deleted_at IS NULL AND org_id IS NOT NULL"
            " ORDER BY last_activity_at DESC NULLS LAST, created_at DESC"
            " LIMIT :n",
            n=SAMPLE_RECENT_EXPERIMENTS,
        )
        exps += await rows_of(
            conn,
            "SELECT * FROM experiments"
            " WHERE deleted_at IS NULL AND org_id IS NOT NULL"
            " ORDER BY md5(id || :key) LIMIT :n",
            key=sample_key,
            n=SAMPLE_RANDOM_EXPERIMENTS,
        )
        # Per-owner anchor: each distinct owner's most recent experiments,
        # so the dashboard "Mine" view has data for every member. Guarded:
        # owner_user_id is a newer column and may be absent on older schemas.
        try:
            per_owner = await rows_of(
                conn,
                "SELECT * FROM ("
                "  SELECT e.*, row_number() OVER ("
                "    PARTITION BY e.owner_user_id"
                "    ORDER BY e.last_activity_at DESC NULLS LAST,"
                "             e.created_at DESC"
                "  ) AS _rn FROM experiments e"
                "  WHERE e.deleted_at IS NULL AND e.org_id IS NOT NULL"
                "    AND e.owner_user_id IS NOT NULL"
                ") s WHERE s._rn <= :k",
                k=SAMPLE_EXPERIMENTS_PER_OWNER,
            )
            for e in per_owner:
                e.pop("_rn", None)
            exps += per_owner
        except Exception as exc:  # noqa: BLE001 -- never sink the draw
            _warn(f"per-owner experiment anchor skipped ({type(exc).__name__}: {exc})")
        exps = list({e["id"]: e for e in exps}.values())
        exp_ids = [e["id"] for e in exps]
        if not exp_ids:
            return {"rows": {}, "linkage": []}

        links = await rows_of(
            conn,
            "SELECT * FROM task_experiments"
            " WHERE experiment_id = ANY(:ids) AND deleted_at IS NULL",
            ids=exp_ids,
        )
        task_ids = sorted({l["task_id"] for l in links})
        tasks = await rows_of(
            conn,
            "SELECT * FROM tasks WHERE id = ANY(:ids) AND deleted_at IS NULL",
            ids=task_ids,
        ) if task_ids else []
        # Extra random tasks beyond the anchored experiments widen the task
        # distribution (tasks outside any sampled experiment).
        tasks += await rows_of(
            conn,
            "SELECT * FROM tasks"
            " WHERE deleted_at IS NULL AND org_id IS NOT NULL"
            "   AND NOT (id = ANY(:ids))"
            " ORDER BY md5(id || :key) LIMIT :n",
            ids=task_ids or [""],
            key=sample_key,
            n=SAMPLE_EXTRA_TASKS,
        )
        kept_task_ids = [t["id"] for t in tasks]
        links = [l for l in links if l["task_id"] in set(kept_task_ids)]

        versions = await rows_of(
            conn,
            "SELECT * FROM task_versions WHERE task_id = ANY(:ids)",
            ids=kept_task_ids,
        ) if kept_task_ids else []

        trials = await rows_of(
            conn,
            "SELECT * FROM ("
            "  SELECT t.*, row_number() OVER ("
            "    PARTITION BY t.experiment_id ORDER BY md5(t.id || :key)"
            "  ) AS _rn FROM trials t"
            "  WHERE t.experiment_id = ANY(:exp_ids)"
            "    AND t.task_id = ANY(:task_ids)"
            "    AND t.deleted_at IS NULL"
            ") s WHERE s._rn <= :cap",
            key=sample_key,
            exp_ids=exp_ids,
            task_ids=kept_task_ids,
            cap=SAMPLE_TRIALS_PER_EXPERIMENT,
        ) if kept_task_ids else []
        for t in trials:
            t.pop("_rn", None)

        # --- coverage tables, each independently best-effort.
        trial_ids = {t["id"] for t in trials}
        failures: dict[str, str] = {}

        async def section(name: str, sql: str, **params):
            try:
                if not await table_exists(conn, name):
                    return
                rows[name] = await rows_of(conn, sql, **params)
            except Exception as exc:  # noqa: BLE001 -- never sink the draw
                failures[name] = f"{type(exc).__name__}: {exc}"
                rows.pop(name, None)

        await section(
            "worker_jobs",
            "SELECT * FROM worker_jobs"
            " WHERE status::text = ANY(:statuses)"
            "   AND subject_id = ANY(:subjects)",
            statuses=list(_TERMINAL_JOB_STATUSES),
            subjects=sorted(trial_ids | set(kept_task_ids)),
        )
        await section(
            "skills",
            "SELECT * FROM skills WHERE deleted_at IS NULL"
            " ORDER BY md5(id || :key) LIMIT :n",
            key=sample_key, n=SAMPLE_SKILLS,
        )
        if rows.get("skills"):
            await section(
                "skill_files",
                "SELECT * FROM skill_files WHERE skill_id = ANY(:ids)",
                ids=[s["id"] for s in rows["skills"]],
            )
        await section(
            "documents",
            "SELECT * FROM documents WHERE deleted_at IS NULL"
            " ORDER BY md5(id || :key) LIMIT :n",
            key=sample_key, n=SAMPLE_DOCUMENTS,
        )
        await section(
            "probe_presets",
            "SELECT * FROM probe_presets"
            " WHERE deleted_at IS NULL AND org_id IS NOT NULL"
            " ORDER BY md5(id || :key) LIMIT :n",
            key=sample_key, n=SAMPLE_PROBE_PRESETS,
        )
        for name, err in failures.items():
            _warn(f"sample section {name!r} skipped ({err})")

        # --- identity: import the sampled orgs IN FULL (org row + every
        # member), as-is, so auth and the members list match prod exactly.
        org_ids = sorted({
            row["org_id"]
            for table_rows in ([exps, tasks, trials], rows.values())
            for group in table_rows
            for row in (group if isinstance(group, list) else [group])
            if isinstance(row, dict) and row.get("org_id")
        })
        orgs = await rows_of(
            conn,
            "SELECT * FROM organizations WHERE id = ANY(:ids)",
            ids=org_ids,
        ) if org_ids else []
        users = await rows_of(
            conn,
            "SELECT * FROM users WHERE org_id = ANY(:ids)",
            ids=org_ids,
        ) if org_ids else []
        # Plus any referenced author who sits outside the sampled orgs.
        known_users = {u["id"] for u in users}
        extra_user_ids = sorted({
            v
            for group in ([exps, tasks, versions, trials], rows.values())
            for table_rows in group
            for row in (table_rows if isinstance(table_rows, list) else [table_rows])
            for k, v in (row.items() if isinstance(row, dict) else [])
            if k.endswith("_user_id") and v and v not in known_users
        })
        if extra_user_ids:
            users += await rows_of(
                conn,
                "SELECT * FROM users WHERE id = ANY(:ids)",
                ids=extra_user_ids,
            )

    # --- transforms. The ONLY mutations are operational: in-flight statuses
    # normalize to terminal so the preview never schedules real work, and
    # self-referential FKs are deferred to the linkage pass because rows
    # within one table upsert in arbitrary order.
    linkage: list[tuple[str, str, str, str]] = []
    version_ids = {v["id"] for v in versions}
    for t in tasks:
        if t["status"] not in _TERMINAL_TASK_STATUSES:
            t["status"] = "FAILED"
        if t.get("current_version_id") in version_ids:
            linkage.append(
                ("tasks", t["id"], "current_version_id", t["current_version_id"])
            )
        t["current_version_id"] = None
    for t in trials:
        if t["status"] not in _TERMINAL_TRIAL_STATUSES:
            t["status"] = "FAILED"
            t["current_worker_id"] = None
            t["current_queue_slot"] = None
        if t.get("superseded_by_trial_id") in trial_ids:
            linkage.append(
                ("trials", t["id"], "superseded_by_trial_id",
                 t["superseded_by_trial_id"])
            )
        t["superseded_by_trial_id"] = None
    job_ids = {j["id"] for j in rows.get("worker_jobs", [])}
    for j in rows.get("worker_jobs", []):
        j["current_worker_id"] = None
        j["current_queue_slot"] = None
        if j.get("parent_job_id") not in job_ids:
            j["parent_job_id"] = None

    rows.update({
        "organizations": orgs,
        "users": users,
        "experiments": exps,
        "tasks": tasks,
        "task_versions": versions,
        "task_experiments": links,
        "trials": trials,
    })
    return {"rows": rows, "linkage": linkage}


def _topo_order(md: MetaData) -> list:
    """Kahn's topological sort of reflected tables, ignoring ``_BACKEDGES``.

    SQLAlchemy's built-in ``sorted_tables`` reacts to a cycle by dropping
    every FK on every table involved, which silently misorders unrelated
    tables (e.g. pushes ``users`` after ``tasks`` because ``tasks <->
    task_versions`` is cyclic). We only want to drop the documented
    back-edge.
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
        ready = sorted(
            n for n, d in deps.items() if n in remaining and not (d & set(remaining))
        )
        if not ready:
            # Defensive: if a real cycle remains beyond ``_BACKEDGES``, break
            # ties by name so the order stays deterministic across runs.
            ready = sorted(remaining)
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


def _row_key(table, row: dict) -> str:
    """Stable state-table identifier for a row: PK values joined by ':'."""
    return ":".join(str(row[c.name]) for c in table.primary_key.columns)


async def seed(engine: AsyncEngine, *, sampled: dict | None = None) -> None:
    """Load the sampled prod subset into the branch DB.

    Idempotent and convergent: rows upsert by primary key; rows imported by
    a PREVIOUS run that are absent from the current draw are deleted (prod
    drift), tracked via ``_preview_seed_state`` so reviewer-created data is
    never touched. ``sampled=None`` leaves existing data alone entirely.
    """
    sample_rows = (sampled or {}).get("rows", {})
    md = MetaData()
    async with engine.begin() as conn:
        await conn.run_sync(md.reflect)
        ordered = _topo_order(md)

        await conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE}"
            " (table_name text NOT NULL, row_id text NOT NULL,"
            "  PRIMARY KEY (table_name, row_id))"
        ))
        await _cleanup_legacy_fixture_rows(md, conn, ordered)
        if sampled is None:
            return

        # Imported users carry real unique identities (clerk_user_id,
        # (org_id, email)). A JIT-provisioned reviewer row on a reused
        # branch may already hold one of them under a different primary
        # key; per-row savepoints below let that single import yield to
        # the existing row instead of failing the run.
        await _reconcile_previous_draw(md, conn, sample_rows)

        for table in ordered:
            rows = sample_rows.get(table.name, [])
            if not rows:
                continue
            pk_cols = [c.name for c in table.primary_key.columns]
            for row in rows:
                values = {}
                for k, v in row.items():
                    col = table.columns.get(k)
                    if col is None:
                        _warn(
                            f"dropped {table.name}.{k} (no such column on "
                            f"the target schema)"
                        )
                        continue
                    if isinstance(col.type, (JSONB, JSON)) and isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass
                    values[k] = v
                stmt = pg_insert(table).values(**values)
                set_ = {k: stmt.excluded[k] for k in values if k not in pk_cols}
                try:
                    async with conn.begin_nested():
                        await conn.execute(
                            stmt.on_conflict_do_update(
                                index_elements=pk_cols, set_=set_
                            )
                        )
                except (IntegrityError, DBAPIError) as exc:
                    _warn(
                        f"skipped {table.name} row {_row_key(table, row)} "
                        f"({type(exc.orig or exc).__name__}); existing row wins"
                    )

        # Linkage pass: self/circular references deferred by the sampler.
        for table_name, row_id, column, value in (sampled or {}).get("linkage", []):
            table = md.tables[table_name]
            await conn.execute(
                table.update().where(table.c.id == row_id)
                .values(**{column: value})
            )

        # Record the current draw for the next run's reconcile.
        await conn.execute(text(f"DELETE FROM {_STATE_TABLE}"))
        for name in _RECONCILED_TABLES:
            table = md.tables.get(name)
            if table is None or not sample_rows.get(name):
                continue
            await conn.execute(
                text(
                    f"INSERT INTO {_STATE_TABLE} (table_name, row_id)"
                    " VALUES (:t, :r) ON CONFLICT DO NOTHING"
                ),
                [
                    {"t": name, "r": _row_key(table, row)}
                    for row in sample_rows[name]
                ],
            )


async def _reconcile_previous_draw(md: MetaData, conn, sample_rows: dict) -> None:
    """Delete rows the PREVIOUS draw imported that this draw no longer has.

    Children-first via reversed ``_RECONCILED_TABLES``. Only rows recorded
    in the state table are candidates, so reviewer-created data and
    organizations/users (identity rows; negligible churn, may be referenced
    by reviewer-created rows) are never deleted.
    """
    res = await conn.execute(
        text(f"SELECT table_name, row_id FROM {_STATE_TABLE}")
    )
    previous: dict[str, set[str]] = {}
    for table_name, row_id in res.fetchall():
        previous.setdefault(table_name, set()).add(row_id)
    if not previous:
        return

    for name in reversed(_RECONCILED_TABLES):
        table = md.tables.get(name)
        if table is None or name not in previous:
            continue
        current = {
            _row_key(table, row) for row in sample_rows.get(name, [])
        }
        pk_cols = list(table.primary_key.columns)
        for stale_key in sorted(previous[name] - current):
            parts = stale_key.split(":", len(pk_cols) - 1)
            cond = [c == v for c, v in zip(pk_cols, parts)]
            stmt = delete(table)
            for c in cond:
                stmt = stmt.where(c)
            await conn.execute(stmt)


async def _cleanup_legacy_fixture_rows(md: MetaData, conn, ordered) -> None:
    """Remove artifacts of earlier seed versions from reused branches.

    Earlier revisions seeded curated fixtures (ids prefixed ``seed-``) and
    anonymized users (``...@preview.local``). Previews now hold prod data
    only, so those rows are deleted children-first; each delete runs in a
    savepoint so an unexpected reference never fails the run.
    """
    for table in reversed(ordered):
        if table.name == _STATE_TABLE:
            continue
        str_pks = [c for c in table.primary_key.columns if hasattr(c.type, "length") or str(c.type).lower().startswith("text")]
        if not str_pks:
            continue
        conds = [c.like("seed-%") for c in str_pks]
        try:
            async with conn.begin_nested():
                for cond in conds:
                    await conn.execute(delete(table).where(cond))
        except (IntegrityError, DBAPIError):
            _warn(f"legacy cleanup skipped for {table.name} (still referenced)")
    users = md.tables.get("users")
    if users is not None:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    delete(users).where(users.c.email.like("%@preview.local"))
                )
        except (IntegrityError, DBAPIError):
            _warn("legacy cleanup skipped for anonymized users (referenced)")
