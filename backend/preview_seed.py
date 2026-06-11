"""Deterministic, idempotent seed for PR preview databases.

Runs in GitHub Actions after Alembic has applied both stacks to a
data-less Supabase preview branch. Populates a small curated set of rows
so the preview UI renders and the feature under review is testable,
plus (best-effort) a small pseudo-random sample of production data so
reviewers can check against real prod items without cloning everything.

Invariants:
- Deterministic: curated rows pin ids/timestamps; the prod sample is
  ordered by ``md5(id || sample_key)`` with the PR number as the key, so
  re-runs within a PR draw the same rows while different PRs draw
  different ones.
- Idempotent + convergent: rows are upserted (ON CONFLICT DO UPDATE);
  seed-owned rows (id prefixed ``seed-``) absent from FIXTURES are
  deleted, and previously sampled rows absent from the current draw are
  deleted before upsert, so edits, removals, and prod drift all converge
  on reused branches.
- Cross-stack via reflection: the live post-Alembic schema is reflected,
  so inserts span both Alembic stacks in FK topological order without
  importing any ORM models.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, Integer, MetaData, Numeric, String,
    Text, and_, delete, false, select, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as _PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

SEED_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
SEED_ID_PREFIX = "seed-"
SEED_ORG_ID = "seed-org"

# Prod-sample sizing. A real slice of the schema and its distribution,
# never the whole dataset.
SAMPLE_RECENT_EXPERIMENTS = 6
SAMPLE_RANDOM_EXPERIMENTS = 6
SAMPLE_EXTRA_TASKS = 10
SAMPLE_TRIALS_PER_EXPERIMENT = 50
SAMPLE_SKILLS = 10
SAMPLE_DOCUMENTS = 10
SAMPLE_PROBE_PRESETS = 10
# In-flight rows ARE sampled (recent experiments are mostly in-flight, and
# excluding them starves the draw) but their status is normalized to a
# terminal one on import -- mirroring what cancel_cloned_preview_work.sh did
# for --with-data clones -- and no worker_jobs in non-terminal states are
# imported, so preview workers never pick anything up.
_TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED")
_TERMINAL_TRIAL_STATUSES = ("SUCCESS", "FAILED")
_TERMINAL_JOB_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")


def _filler(column):
    """Deterministic type-based placeholder for a NOT-NULL column a fixture
    omits and that has no DB default. Keeps the seed from breaking on a
    newly-added column; the value is meaningless, so curate it in FIXTURES
    only if the column matters. Unknown types raise so the CI gate forces an
    explicit decision.

    Every successful fill emits a stderr line so a new column landing in
    main can't silently soak up a wrong-but-passing default -- against the
    project's "no surprises" priority. Curate in FIXTURES if the value
    matters."""
    t = column.type
    if isinstance(t, Boolean):
        val = False
    elif isinstance(t, (Integer, Numeric, Float)):
        val = 0
    elif isinstance(t, DateTime):
        val = SEED_EPOCH
    elif isinstance(t, Date):
        val = SEED_EPOCH.date()
    elif isinstance(t, (JSONB, JSON)):
        val = {}
    elif isinstance(t, _PgUUID):
        val = "00000000-0000-0000-0000-000000000000"
    elif getattr(t, "enums", None):  # reflected PG ENUM
        val = t.enums[0]
    elif isinstance(t, (String, Text)):
        val = ""
    else:
        raise RuntimeError(
            f"No auto-fill rule for {column.table.name}.{column.name} ({t!r}); "
            f"add it to FIXTURES explicitly."
        )
    print(
        f"preview_seed: auto-filled {column.table.name}.{column.name}={val!r} "
        f"(no fixture value / no server_default) -- curate if it matters",
        file=sys.stderr,
    )
    return val


def _seed_owned(table):
    """Return a WHERE clause that selects rows owned by the seed: every
    string-typed primary-key column begins with ``SEED_ID_PREFIX``. For an
    association table, ALL endpoints must be seeded -- prevents the
    reconcile pass from dropping a seed-task link to a real experiment."""
    str_pks = [
        c for c in table.primary_key.columns
        if isinstance(c.type, (String, Text))
    ]
    return and_(*[c.like(f"{SEED_ID_PREFIX}%") for c in str_pks]) if str_pks else false()


def _fixtures() -> dict[str, list[dict]]:
    """Curated rows keyed by table name. Every id starts with
    ``SEED_ID_PREFIX`` and every row sets created_at/updated_at = SEED_EPOCH."""
    org_id = SEED_ORG_ID
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
        "task_experiments": [
            {"task_id": task_a, "experiment_id": exp_id,
             "created_at": SEED_EPOCH, "deleted_at": None},
            {"task_id": task_b, "experiment_id": exp_id,
             "created_at": SEED_EPOCH, "deleted_at": SEED_EPOCH},
        ],
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


async def sample_prod_subset(
    source: AsyncEngine, *, sample_key: str
) -> dict:
    """Draw a deterministic pseudo-random subset of production data.

    Read-only by construction: only SELECTs are issued, and the caller builds
    the source engine with ``default_transaction_read_only=on``. Rows are
    ordered by ``md5(id || sample_key)`` so the draw is stable for a given key
    (the PR number) but differs across PRs.

    The core closure is anchored on live experiments (recent + random) plus
    extra random tasks, and walks task_experiments -> tasks -> task_versions
    -> trials -> referenced users. In-flight tasks/trials are included but
    normalized to FAILED on import (as the old --with-data quiesce did), and
    only terminal worker_jobs are imported, so preview workers never pick
    anything up. Schema-coverage tables (worker_jobs, skills, skill_files,
    documents, probe_presets) are sampled best-effort: each section is
    guarded by a table-existence check and its own try/except, so one
    section's failure never sinks the draw.

    Every row is remapped into the seeded org (so the reviewer's Clerk login
    sees it) and identity fields are anonymized; production ``organizations``
    rows are never imported.

    Returns ``{"rows": {table: [row, ...]}, "current_versions": {task: ver}}``
    for :func:`seed` to merge through its normal upsert machinery.
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
        exps = list({e["id"]: e for e in exps}.values())
        exp_ids = [e["id"] for e in exps]
        if not exp_ids:
            return {"rows": {}, "current_versions": {}}

        links = await rows_of(
            conn,
            "SELECT * FROM task_experiments"
            " WHERE experiment_id = ANY(:ids) AND deleted_at IS NULL",
            ids=exp_ids,
        )
        task_ids = sorted({l["task_id"] for l in links})
        tasks = await rows_of(
            conn,
            "SELECT * FROM tasks"
            " WHERE id = ANY(:ids) AND deleted_at IS NULL"
            " ORDER BY md5(id || :key)",
            ids=task_ids,
            key=sample_key,
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
        # Dedupe by name: after the org remap every task lands in the seeded
        # org, where idx_tasks_unique_org_name enforces (org, name) unique.
        # md5 order above makes "which duplicate wins" deterministic.
        seen_names: set[str] = set()
        tasks = [
            t for t in tasks
            if not (t["name"] in seen_names or seen_names.add(t["name"]))
        ]
        kept_task_ids = [t["id"] for t in tasks]

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

        # --- schema-coverage tables, each independently best-effort.
        trial_ids = {t["id"] for t in trials}
        sections: dict[str, str] = {}

        async def section(name: str, sql: str, **params):
            try:
                if not await table_exists(conn, name):
                    return
                rows[name] = await rows_of(conn, sql, **params)
            except Exception as exc:  # noqa: BLE001 -- one section never sinks the draw
                sections[name] = f"{type(exc).__name__}: {exc}"
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
            key=sample_key,
            n=SAMPLE_SKILLS,
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
            key=sample_key,
            n=SAMPLE_DOCUMENTS,
        )
        await section(
            "probe_presets",
            "SELECT * FROM probe_presets"
            " WHERE deleted_at IS NULL AND org_id IS NOT NULL"
            " ORDER BY md5(id || :key) LIMIT :n",
            key=sample_key,
            n=SAMPLE_PROBE_PRESETS,
        )
        for name, err in sections.items():
            print(
                f"preview_seed: sample section {name!r} skipped ({err})",
                file=sys.stderr,
            )

        user_ids = sorted({
            v
            for group in ([exps, tasks, versions, trials], rows.values())
            for table_rows in group
            for row in (table_rows if isinstance(table_rows, list) else [table_rows])
            for k, v in (row.items() if isinstance(row, dict) else [])
            if k.endswith("_user_id") and v
        })
        users = await rows_of(
            conn,
            "SELECT * FROM users WHERE id = ANY(:ids)",
            ids=user_ids,
        ) if user_ids else []

    # --- transforms: remap into the seeded org, anonymize identity fields,
    # normalize in-flight statuses, and never let sampled data become
    # publicly shareable from a preview.
    links = [l for l in links if l["task_id"] in set(kept_task_ids)]
    for e in exps:
        e["org_id"] = SEED_ORG_ID
        e["is_public"] = False
        e["public_token"] = None
    # Self/circular references are deferred to the linkage pass in seed():
    # rows within one table upsert in arbitrary order, so a row pointing at a
    # sibling (tasks.current_version_id, trials.superseded_by_trial_id) would
    # otherwise hit the FK before its target exists.
    linkage: list[tuple[str, str, str, str]] = []
    version_ids = {v["id"] for v in versions}
    for t in tasks:
        t["org_id"] = SEED_ORG_ID
        t["user"] = "owner@preview.local"
        if t["status"] not in _TERMINAL_TASK_STATUSES:
            t["status"] = "FAILED"
        if t.get("current_version_id") in version_ids:
            linkage.append(
                ("tasks", t["id"], "current_version_id", t["current_version_id"])
            )
        t["current_version_id"] = None
    for t in trials:
        t["org_id"] = SEED_ORG_ID
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
        j["org_id"] = SEED_ORG_ID
        j["current_worker_id"] = None
        j["current_queue_slot"] = None
        if j.get("parent_job_id") not in job_ids:
            j["parent_job_id"] = None
    seen_skill_names: set[str] = set()
    rows["skills"] = [
        s for s in rows.get("skills", [])
        if not (s["name"] in seen_skill_names or seen_skill_names.add(s["name"]))
    ]
    kept_skill_ids = {s["id"] for s in rows["skills"]}
    rows["skill_files"] = [
        f for f in rows.get("skill_files", []) if f["skill_id"] in kept_skill_ids
    ]
    for s in rows["skills"]:
        if s.get("org_id") is not None:
            s["org_id"] = SEED_ORG_ID
    for d in rows.get("documents", []):
        if d.get("org_id") is not None:
            d["org_id"] = SEED_ORG_ID
    seen_preset_names: set[str] = set()
    rows["probe_presets"] = [
        p for p in rows.get("probe_presets", [])
        if not (p["name"] in seen_preset_names or seen_preset_names.add(p["name"]))
    ]
    for p in rows["probe_presets"]:
        p["org_id"] = SEED_ORG_ID
    for u in users:
        u["org_id"] = SEED_ORG_ID
        u["email"] = f"user-{u['id']}@preview.local"
        u["name"] = f"Prod User {u['id'][:8]}"
        u["clerk_user_id"] = None
        u["avatar_url"] = None
        u["github_username"] = None

    rows.update({
        "users": users,
        "experiments": exps,
        "tasks": tasks,
        "task_versions": versions,
        "task_experiments": links,
        "trials": trials,
    })
    return {"rows": rows, "linkage": linkage}


async def seed(engine: AsyncEngine, *, sampled: dict | None = None) -> None:
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

        # The seeded org carries a real external clerk_org_id (so a reviewer's
        # Clerk JWT matches it). The generic upsert keys on the primary key
        # only, so it can't resolve the separate UNIQUE(clerk_org_id): a reused
        # or once-``--with-data`` branch may already hold that mapping on a
        # different row (cloned prod data). Free it first so the seeded org can
        # claim it -- a no-op on a clean data-less branch.
        orgs = md.tables.get("organizations")
        if orgs is not None:
            await conn.execute(
                orgs.update()
                .where(orgs.c.clerk_org_id == org["clerk_org_id"])
                .where(orgs.c.id != org["id"])
                .values(clerk_org_id=None)
            )

        # Reconcile previously sampled rows BEFORE upserting: rows from an
        # earlier draw that are absent from the current one are deleted
        # (children-first), so reused branches track prod drift and a stale
        # task can't collide with a fresh same-named one on
        # idx_tasks_unique_org_name. Only runs when a sample is provided --
        # a transient prod outage (sampled=None) must not wipe the realism
        # data a previous push loaded. JIT reviewer users are untouched
        # (users are never sample-reconciled).
        sample_rows = (sampled or {}).get("rows", {})
        if sampled is not None:
            await _reconcile_sampled(md, conn, sample_rows)

        # Upsert parents-first, auto-filling any NOT-NULL column the fixture
        # omits that has no DB default. Generalized to ANY primary key
        # (single-col ``id``, composite like ``task_experiments``, ...): we
        # key ON CONFLICT on the full PK tuple and never blast PK columns in
        # the UPDATE clause. Sampled rows ride the same path; their keys are
        # filtered to the target's columns (prod may trail the branch schema)
        # and raw-SELECT JSON strings are coerced back to objects so the
        # typed JSONB bind doesn't double-encode them.
        for table in ordered:
            rows = list(fixtures.get(table.name, []))
            rows += sample_rows.get(table.name, [])
            if not rows:
                continue
            pk_cols = [c.name for c in table.primary_key.columns]
            needs_value = [
                c for c in table.columns
                if not c.nullable and c.server_default is None and not c.primary_key
            ]
            for row in rows:
                filled = {}
                for k, v in row.items():
                    col = table.columns.get(k)
                    if col is None:
                        print(
                            f"preview_seed: dropped {table.name}.{k} (no such "
                            f"column on the target schema)",
                            file=sys.stderr,
                        )
                        continue
                    if isinstance(col.type, (JSONB, JSON)) and isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass
                    filled[k] = v
                for c in needs_value:
                    if c.name not in filled:
                        filled[c.name] = _filler(c)
                stmt = pg_insert(table).values(**filled)
                set_ = {k: stmt.excluded[k] for k in filled if k not in pk_cols}
                await conn.execute(
                    stmt.on_conflict_do_update(index_elements=pk_cols, set_=set_)
                )

        # Linkage pass for circular/self references: the curated
        # current-version map plus any deferred (table, id, column, value)
        # entries captured by the sampler.
        linkage = [
            ("tasks", task_id, "current_version_id", version_id)
            for task_id, version_id in _CURRENT_VERSION.items()
        ]
        if sampled:
            linkage += [tuple(entry) for entry in sampled.get("linkage", [])]
        for table_name, row_id, column, value in linkage:
            table = md.tables[table_name]
            await conn.execute(
                table.update().where(table.c.id == row_id)
                .values(**{column: value})
            )

        # Reconcile removals: delete seed-owned rows no longer in FIXTURES,
        # children-first. Match by full PK tuple so an association row with
        # mixed seed/non-seed endpoints (e.g. seed-task-a linked to a real
        # experiment) is left alone -- ``_seed_owned`` requires every string
        # PK column to start with ``SEED_ID_PREFIX``.
        for table in reversed(ordered):
            if table.name not in fixtures:
                continue
            pk_cols = list(table.primary_key.columns)
            keep = {tuple(r[c.name] for c in pk_cols) for r in fixtures[table.name]}
            existing = await conn.execute(
                select(*pk_cols).where(_seed_owned(table))
            )
            for row in existing.fetchall():
                key = tuple(row)
                if key not in keep:
                    await conn.execute(
                        delete(table).where(
                            and_(*[c == v for c, v in zip(pk_cols, key)])
                        )
                    )

        await _recompute_projections(conn, fixtures)


async def _reconcile_sampled(md: MetaData, conn, sample_rows: dict) -> None:
    """Delete previously sampled rows absent from the current draw.

    Sampled rows are identified as non-``seed-`` rows living in the seeded
    org (the only way such rows arrive on a data-less branch). Deletes run
    children-first so no FK ordering is assumed. ``users`` are deliberately
    excluded: JIT-provisioned reviewer accounts live there and must survive;
    a stale sampled user row is harmless (anonymized, tiny).
    """
    def _kept_ids(name: str) -> list:
        return [r["id"] for r in sample_rows.get(name, [])]

    def _not_seed(col):
        return ~col.like(f"{SEED_ID_PREFIX}%")

    # Org-scoped schema-coverage tables first (nothing references them from
    # the core chain). skill_files ride the skills FK cascade. Globally
    # scoped sampled rows (org_id NULL, e.g. seed skills) are not reconciled;
    # they are reference data with negligible churn.
    for name in ("worker_jobs", "documents", "skills", "probe_presets"):
        table = md.tables.get(name)
        if table is None:
            continue
        await conn.execute(
            delete(table)
            .where(table.c.org_id == SEED_ORG_ID)
            .where(_not_seed(table.c.id))
            .where(~table.c.id.in_(_kept_ids(name)))
        )

    trials = md.tables.get("trials")
    if trials is not None:
        await conn.execute(
            delete(trials)
            .where(trials.c.org_id == SEED_ORG_ID)
            .where(_not_seed(trials.c.id))
            .where(~trials.c.id.in_(_kept_ids("trials")))
        )

    links = md.tables.get("task_experiments")
    if links is not None:
        kept_pairs = {
            (l["task_id"], l["experiment_id"])
            for l in sample_rows.get("task_experiments", [])
        }
        res = await conn.execute(
            select(links.c.task_id, links.c.experiment_id)
            .where(_not_seed(links.c.task_id))
            .where(_not_seed(links.c.experiment_id))
        )
        for task_id, exp_id in res.fetchall():
            if (task_id, exp_id) not in kept_pairs:
                await conn.execute(
                    delete(links)
                    .where(links.c.task_id == task_id)
                    .where(links.c.experiment_id == exp_id)
                )

    versions = md.tables.get("task_versions")
    if versions is not None:
        # tasks.current_version_id is ON DELETE SET NULL; the linkage pass
        # re-points kept tasks afterwards.
        await conn.execute(
            delete(versions)
            .where(_not_seed(versions.c.task_id))
            .where(~versions.c.id.in_(_kept_ids("task_versions")))
        )

    for name in ("tasks", "experiments"):
        table = md.tables.get(name)
        if table is None:
            continue
        await conn.execute(
            delete(table)
            .where(table.c.org_id == SEED_ORG_ID)
            .where(_not_seed(table.c.id))
            .where(~table.c.id.in_(_kept_ids(name)))
        )


async def _recompute_projections(conn, fixtures: dict[str, list[dict]]) -> None:
    """Refresh derived columns after seeding. No-op on schemas without
    projections. A feature that adds derived projection columns (e.g. tags)
    extends this to recompute them per seeded task via a guarded import, so
    this stays usable on schemas without those tables."""
    return


def _scaffold(table_name: str) -> str:
    """Print a ready-to-edit FIXTURES entry for one table, reflected from a
    migrated DB (MIGRATED_DB_URL). Uses async reflection because backend ships
    asyncpg, not psycopg2 -- a sync ``create_engine`` on the stripped URL
    would raise ModuleNotFoundError at the import."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _reflect():
        eng = create_async_engine(
            os.environ["MIGRATED_DB_URL"],
            connect_args={"statement_cache_size": 0},
        )
        md = MetaData()
        async with eng.begin() as conn:
            await conn.run_sync(md.reflect, only=[table_name])
        await eng.dispose()
        return md.tables[table_name]

    table = asyncio.run(_reflect())
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
