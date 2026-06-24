from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sys

from sqlalchemy import JSON, MetaData, delete, text, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

SEED_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

SAMPLE_RECENT_EXPERIMENTS = 8
SAMPLE_RANDOM_EXPERIMENTS = 8
SAMPLE_EXPERIMENTS_PER_OWNER = 3
SAMPLE_EXTRA_TASKS = 20
SAMPLE_TRIALS_PER_EXPERIMENT = 50
SAMPLE_SKILLS = 10
SAMPLE_DOCUMENTS = 10

_MAX_BIND_PARAMS = 28000
_LOAD_STREAMS = 6

_TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED")
_TERMINAL_TRIAL_STATUSES = ("SUCCESS", "FAILED")
_TERMINAL_JOB_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")

_RECONCILED_TABLES = (
    "experiments",
    "tasks",
    "task_versions",
    "task_experiments",
    "trials",
    "worker_jobs",
    "skills",
    "skill_files",
    "documents",
    # tags before tag_assignments (FK order)
    "tags",
    "tag_assignments",
)

_BACKEDGES = {("tasks", "current_version_id")}

_LINKAGE_COLUMNS = {
    "tasks": {"current_version_id"},
    "trials": {"superseded_by_trial_id"},
}

_STATE_TABLE = "_preview_seed_state"


def _warn(message: str) -> None:
    print(f"preview_seed: {message}", file=sys.stderr)


_warned_dropped_columns: set[tuple[str, str]] = set()


def _error_cause(exc) -> str:
    orig = getattr(exc, "orig", None) or exc
    cause = getattr(orig, "__cause__", None) or orig
    parts = []
    if sqlstate := getattr(cause, "sqlstate", None):
        parts.append(f"SQLSTATE {sqlstate}")
    if constraint := getattr(cause, "constraint_name", None):
        parts.append(f"constraint {constraint}")
    detail = type(cause).__name__
    return f"{detail} ({', '.join(parts)})" if parts else detail


async def sample_prod_subset(source: AsyncEngine, *, sample_key: str) -> dict:
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
        except Exception as exc:
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
        tasks = (
            await rows_of(
                conn,
                "SELECT * FROM tasks WHERE id = ANY(:ids) AND deleted_at IS NULL",
                ids=task_ids,
            )
            if task_ids
            else []
        )
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

        versions = (
            await rows_of(
                conn,
                "SELECT * FROM task_versions WHERE task_id = ANY(:ids)",
                ids=kept_task_ids,
            )
            if kept_task_ids
            else []
        )

        trials = (
            await rows_of(
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
            )
            if kept_task_ids
            else []
        )
        for t in trials:
            t.pop("_rn", None)

        trial_ids = {t["id"] for t in trials}
        failures: dict[str, str] = {}

        async def section(name: str, sql: str, **params):
            try:
                if not await table_exists(conn, name):
                    return
                rows[name] = await rows_of(conn, sql, **params)
            except Exception as exc:
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
        for name, err in failures.items():
            _warn(f"sample section {name!r} skipped ({err})")

        org_ids = sorted(
            {
                row["org_id"]
                for group in [exps, tasks, trials, *rows.values()]
                for row in group
                if row.get("org_id")
            }
        )

        # Tags for the sampled orgs, plus their DIRECT assignments onto the
        # sampled tasks / versions / experiments. The only hard FK on
        # tag_assignments is tag_id -> tags(id), so we constrain assignments to
        # the tags we just drew AND to targets that exist in the trimmed set.
        # Only DIRECT assignments are taken: experiment-propagated LIVING /
        # SNAPSHOT rows carry source_*/target ids that can dangle once the
        # task/experiment graph is sampled down. Sampled before the user
        # backfill below so tag owners / assigners are pulled into `users`.
        tagged_version_ids = sorted({v["id"] for v in versions})
        await section(
            "tags",
            "SELECT * FROM tags"
            " WHERE deleted_at IS NULL AND state = 'ACTIVE'"
            "   AND org_id = ANY(:org_ids)"
            " ORDER BY id",
            org_ids=org_ids,
        )
        tag_ids = sorted({t["id"] for t in rows.get("tags", [])})
        if tag_ids:
            await section(
                "tag_assignments",
                "SELECT * FROM tag_assignments"
                " WHERE deleted_at IS NULL AND state = 'ACTIVE'"
                "   AND source = 'DIRECT'"
                "   AND tag_id = ANY(:tag_ids)"
                "   AND ("
                "     (scope = 'TASK' AND target_id = ANY(:task_ids))"
                "     OR (scope = 'VERSION' AND target_id = ANY(:version_ids))"
                "     OR (scope = 'EXPERIMENT' AND target_id = ANY(:exp_ids))"
                "   )"
                " ORDER BY id",
                tag_ids=tag_ids,
                task_ids=kept_task_ids,
                version_ids=tagged_version_ids,
                exp_ids=exp_ids,
            )

        orgs = (
            await rows_of(
                conn,
                "SELECT * FROM organizations WHERE id = ANY(:ids)",
                ids=org_ids,
            )
            if org_ids
            else []
        )
        users = (
            await rows_of(
                conn,
                "SELECT * FROM users WHERE org_id = ANY(:ids)",
                ids=org_ids,
            )
            if org_ids
            else []
        )
        known_users = {u["id"] for u in users}
        extra_user_ids = sorted(
            {
                v
                for group in [exps, tasks, versions, trials, *rows.values()]
                for row in group
                for k, v in row.items()
                if k.endswith("_user_id") and v and v not in known_users
            }
        )
        if extra_user_ids:
            users += await rows_of(
                conn,
                "SELECT * FROM users WHERE id = ANY(:ids)",
                ids=extra_user_ids,
            )

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
                (
                    "trials",
                    t["id"],
                    "superseded_by_trial_id",
                    t["superseded_by_trial_id"],
                )
            )
        t["superseded_by_trial_id"] = None
    job_ids = {j["id"] for j in rows.get("worker_jobs", [])}
    for j in rows.get("worker_jobs", []):
        j["current_worker_id"] = None
        j["current_queue_slot"] = None
        if j.get("parent_job_id") not in job_ids:
            j["parent_job_id"] = None

    # ACTIVE tags are never merged, but guard the merged_into_id self-FK in case
    # a source row is dirty: drop a merge pointer we didn't also sample.
    sampled_tag_ids = {t["id"] for t in rows.get("tags", [])}
    for tg in rows.get("tags", []):
        if tg.get("merged_into_id") and tg["merged_into_id"] not in sampled_tag_ids:
            tg["merged_into_id"] = None

    rows.update(
        {
            "organizations": orgs,
            "users": users,
            "experiments": exps,
            "tasks": tasks,
            "task_versions": versions,
            "task_experiments": links,
            "trials": trials,
        }
    )
    return {"rows": rows, "linkage": linkage}


def _topo_order(md: MetaData) -> list:
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
            ready = sorted(remaining)
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


def _row_key(table, row: dict) -> str:
    return ":".join(str(row[c.name]) for c in table.primary_key.columns)


def _prepare_row(table, row: dict) -> dict:
    values = {}
    for k, v in row.items():
        col = table.columns.get(k)
        if col is None:
            if (table.name, k) not in _warned_dropped_columns:
                _warned_dropped_columns.add((table.name, k))
                _warn(f"dropped {table.name}.{k} (no such column on the target schema)")
            continue
        if isinstance(col.type, (JSONB, JSON)) and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        values[k] = v
    return values


async def seed(engine: AsyncEngine, *, sampled: dict | None = None) -> None:
    sample_rows = (sampled or {}).get("rows", {})
    md = MetaData()
    async with engine.begin() as conn:
        await conn.run_sync(md.reflect)
        ordered = _topo_order(md)

        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE}"
                " (table_name text NOT NULL, row_id text NOT NULL,"
                "  PRIMARY KEY (table_name, row_id))"
            )
        )
        await _cleanup_legacy_fixture_rows(md, conn, ordered)
        if sampled is None:
            return
        await _reconcile_previous_draw(md, conn, sample_rows)

    for table in ordered:
        rows = sample_rows.get(table.name, [])
        if not rows:
            continue
        await _load_table(engine, table, rows)

    async with engine.begin() as conn:
        for table_name, row_id, column, value in (sampled or {}).get("linkage", []):
            table = md.tables[table_name]
            await conn.execute(
                table.update()
                .where(table.c.id == row_id)
                .where(table.c[column].is_distinct_from(value))
                .values(**{column: value})
            )

        await conn.execute(text(f"DELETE FROM {_STATE_TABLE}"))
        for name in _RECONCILED_TABLES:
            table = md.tables.get(name)
            if table is None or not sample_rows.get(name):
                continue
            rids = [_row_key(table, row) for row in sample_rows[name]]
            for start in range(0, len(rids), 10000):
                await conn.execute(
                    text(
                        f"INSERT INTO {_STATE_TABLE} (table_name, row_id)"
                        " SELECT :t, unnest(CAST(:rids AS text[]))"
                        " ON CONFLICT DO NOTHING"
                    ),
                    {"t": name, "rids": rids[start : start + 10000]},
                )


async def _load_table(engine: AsyncEngine, table, rows: list[dict]) -> None:
    prepared = [_prepare_row(table, row) for row in rows]
    try:
        await _load_table_copy_merge(engine, table, prepared)
        return
    except Exception as exc:
        _warn(
            f"copy fast-path failed for {table.name} "
            f"({_error_cause(exc)}); falling back to batched upserts"
        )
    await _load_table_batches(engine, table, prepared)


async def _load_table_copy_merge(
    engine: AsyncEngine, table, prepared: list[dict]
) -> None:
    cols = [c.name for c in table.columns]
    pk_cols = [c.name for c in table.primary_key.columns]
    json_cols = {c.name for c in table.columns if isinstance(c.type, (JSONB, JSON))}

    def _rec_value(name: str, value):
        if name in json_cols and isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

    records = [tuple(_rec_value(c, r.get(c)) for c in cols) for r in prepared]
    col_list = ", ".join(f'"{c}"' for c in cols)
    pk_list = ", ".join(f'"{c}"' for c in pk_cols)
    deferred = _LINKAGE_COLUMNS.get(table.name, set())
    non_pk = [c for c in cols if c not in pk_cols and c not in deferred]
    set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in non_pk)
    tgt_tuple = ", ".join(f'"{table.name}"."{c}"' for c in non_pk)
    exc_tuple = ", ".join(f'EXCLUDED."{c}"' for c in non_pk)
    conflict = (
        f"ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause}"
        f" WHERE ({tgt_tuple}) IS DISTINCT FROM ({exc_tuple})"
        if set_clause
        else f"ON CONFLICT ({pk_list}) DO NOTHING"
    )
    stage = f"_seed_stage_{table.name}"
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    f'CREATE TEMP TABLE "{stage}"'
                    f' (LIKE "{table.name}" INCLUDING DEFAULTS) ON COMMIT DROP'
                )
            )
            raw = (await conn.get_raw_connection()).driver_connection
            await raw.copy_records_to_table(stage, records=records, columns=cols)
            await conn.execute(
                text(
                    f'INSERT INTO "{table.name}" ({col_list})'
                    f' SELECT {col_list} FROM "{stage}" {conflict}'
                )
            )


async def _load_table_batches(engine: AsyncEngine, table, prepared: list[dict]) -> None:
    pk_cols = [c.name for c in table.primary_key.columns]
    batch_size = max(1, _MAX_BIND_PARAMS // max(1, len(table.columns)))
    batches = [
        prepared[start : start + batch_size]
        for start in range(0, len(prepared), batch_size)
    ]
    queue: asyncio.Queue = asyncio.Queue()
    for batch in batches:
        queue.put_nowait(batch)

    skips: dict[str, list[str]] = {}

    async def worker() -> None:
        async with engine.connect() as conn:
            while True:
                try:
                    chunk = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                async with conn.begin():
                    await _upsert_batch(conn, table, pk_cols, chunk, skips)

    workers = min(_LOAD_STREAMS, len(batches))
    await asyncio.gather(*[worker() for _ in range(workers)])

    for cause, row_keys in skips.items():
        sample = ", ".join(row_keys[:3])
        more = f" (+{len(row_keys) - 3} more)" if len(row_keys) > 3 else ""
        _warn(
            f"skipped {len(row_keys)} {table.name} row(s) on {cause}; "
            f"existing rows kept -- e.g. {sample}{more}"
        )


def _changed(table, stmt, keys: list[str]):
    return tuple_(*[table.c[k] for k in keys]).is_distinct_from(
        tuple_(*[stmt.excluded[k] for k in keys])
    )


async def _upsert_batch(
    conn, table, pk_cols: list[str], chunk: list[dict], skips: dict[str, list[str]]
) -> None:
    deferred = _LINKAGE_COLUMNS.get(table.name, set())
    non_pk = [k for k in chunk[0] if k not in pk_cols and k not in deferred]
    stmt = pg_insert(table).values(chunk)
    set_ = {k: stmt.excluded[k] for k in non_pk}
    try:
        async with conn.begin_nested():
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=pk_cols,
                    set_=set_,
                    where=_changed(table, stmt, non_pk),
                )
            )
        return
    except (IntegrityError, DBAPIError):
        pass
    for values in chunk:
        non_pk = [k for k in values if k not in pk_cols and k not in deferred]
        stmt = pg_insert(table).values(**values)
        set_ = {k: stmt.excluded[k] for k in non_pk}
        try:
            async with conn.begin_nested():
                await conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=pk_cols,
                        set_=set_,
                        where=_changed(table, stmt, non_pk),
                    )
                )
        except (IntegrityError, DBAPIError) as exc:
            skips.setdefault(_error_cause(exc), []).append(_row_key(table, values))


async def _reconcile_previous_draw(md: MetaData, conn, sample_rows: dict) -> None:
    res = await conn.execute(text(f"SELECT table_name, row_id FROM {_STATE_TABLE}"))
    previous: dict[str, set[str]] = {}
    for table_name, row_id in res.fetchall():
        previous.setdefault(table_name, set()).add(row_id)
    if not previous:
        return

    for name in reversed(_RECONCILED_TABLES):
        table = md.tables.get(name)
        if table is None or name not in previous:
            continue
        current = {_row_key(table, row) for row in sample_rows.get(name, [])}
        pk_cols = list(table.primary_key.columns)
        stale = sorted(previous[name] - current)
        if not stale:
            continue
        if len(pk_cols) == 1:
            for start in range(0, len(stale), 5000):
                await conn.execute(
                    delete(table).where(pk_cols[0].in_(stale[start : start + 5000]))
                )
        else:
            keys = [k.split(":", len(pk_cols) - 1) for k in stale]
            pk_tuple = tuple_(*pk_cols)
            for start in range(0, len(keys), 5000):
                await conn.execute(
                    delete(table).where(pk_tuple.in_(keys[start : start + 5000]))
                )


async def _cleanup_legacy_fixture_rows(md: MetaData, conn, ordered) -> None:
    for table in reversed(ordered):
        if table.name == _STATE_TABLE:
            continue
        str_pks = [
            c
            for c in table.primary_key.columns
            if hasattr(c.type, "length") or str(c.type).lower().startswith("text")
        ]
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
