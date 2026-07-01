"""Discover legacy Sauron trials in S3 and record them in a ledger table.

This is the read-only step #1 of the Sauron -> Oddish migration. It walks the
legacy S3 bucket with CONCURRENT list streams, collapses the millions of raw
objects down to one row per trial (each ``.../attempt_N/`` prefix), and upserts
those rows into ``leg_trial_ledger`` so the later transfer step has a queryable,
resumable work list.

What it does NOT do: read object contents (no per-trial GETs), touch
``trials``/``tasks``/``experiments``, or run/import anything. Its entire blast
radius is one new table. Re-runs are safe (``ON CONFLICT (s3_prefix) DO NOTHING``).

Credentials (both injected by the ``oddish-prod`` Modal secret):
- ``DATABASE_URL``                  -> the Oddish Postgres (read via oddish.config.settings)
- ``AWS_ACCESS_KEY_ID``/``..._SECRET_ACCESS_KEY`` -> legacy bucket (read via the default boto3 env chain)

Usage:
    modal run backend/scripts/legacy_discover.py                      # dry-run: count only, writes nothing
    modal run backend/scripts/legacy_discover.py --execute            # build the ledger
    modal run backend/scripts/legacy_discover.py --execute --concurrency 48
    modal run backend/scripts/legacy_discover.py --root abundant-ai/nov-5-export/   # one repo
"""

from __future__ import annotations

import re
from pathlib import Path

import modal

_ATT = re.compile(r"attempt_\d+")
_PR = re.compile(r"pr-\d+")

# Repo root for the local image build only. Guarded because Modal re-imports
# this module INSIDE the container where parents[2] doesn't exist.
_parents = Path(__file__).resolve().parents
REPO = _parents[2] if len(_parents) > 2 else Path("/")

app = modal.App("oddish-legacy-discover")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    .add_local_dir(
        str(REPO / "oddish"),
        remote_path="/oddish",
        copy=True,
        ignore=[".venv/", ".git/"],
    )
    # [worker] pulls aioboto3 (S3) + asyncpg/sqlalchemy (DB).
    .run_commands("pip install uv", "cd /oddish && uv pip install --system '.[worker]'")
)
secret = modal.Secret.from_name("oddish-prod", environment_name="main")
# Legacy-bucket AWS keys (oddish-prod doesn't carry them). aioboto3 reads
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from the env this injects.
aws_secret = modal.Secret.from_name("sauron-legacy")

DEFAULT_BUCKET = "abundant-github-workflows-bucket"
DEFAULT_ROOT = "abundant-ai/"

# Ledger DDL. Self-contained so discovery doesn't depend on an Alembic chain.
DDL = """
CREATE TABLE IF NOT EXISTS leg_trial_ledger (
    s3_prefix       text PRIMARY KEY,        -- .../{task}/attempt_N/  (the trial identity)
    bucket          text NOT NULL,
    s3_base         text,                    -- e.g. abundant-ai/nov-5-export
    pr_number       integer,
    run_id          text,
    agent_key       text,
    model_key       text,                    -- lossy; real model recovered at transfer time
    task_id         text,
    attempt         integer,
    has_reward      boolean NOT NULL DEFAULT false,
    has_trajectory  boolean NOT NULL DEFAULT false,
    object_count    integer NOT NULL DEFAULT 0,
    total_bytes     bigint  NOT NULL DEFAULT 0,
    last_modified   timestamptz,
    status          text NOT NULL DEFAULT 'discovered',  -- discovered|transferring|transferred|failed|skipped|conflict
    oddish_trial_id text,
    error           text,
    discovered_at   timestamptz NOT NULL DEFAULT now(),
    transferred_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_leg_ledger_status ON leg_trial_ledger (status);
CREATE INDEX IF NOT EXISTS idx_leg_ledger_base   ON leg_trial_ledger (s3_base);
"""

SHARDS_DDL = """
CREATE TABLE IF NOT EXISTS leg_shards (
    shard    text PRIMARY KEY,   -- a fully-scanned prefix; skipped on future runs
    trials   integer NOT NULL DEFAULT 0,
    done_at  timestamptz NOT NULL DEFAULT now()
);
"""

SHARDS_INSERT = (
    "INSERT INTO leg_shards (shard, trials) VALUES ($1, $2) "
    "ON CONFLICT (shard) DO NOTHING"
)

SHAPES_DDL = """
CREATE TABLE IF NOT EXISTS leg_file_shapes (
    shape    text PRIMARY KEY,   -- normalized object pattern, e.g. attempt_N/verifier/reward.txt
    count    bigint NOT NULL,
    example  text                 -- one real key matching this shape
);
"""

SHAPES_INSERT = """
INSERT INTO leg_file_shapes (shape, count, example)
VALUES ($1, $2, $3)
ON CONFLICT (shape) DO UPDATE SET count = EXCLUDED.count, example = EXCLUDED.example
"""

INSERT_SQL = """
INSERT INTO leg_trial_ledger
  (s3_prefix, bucket, s3_base, pr_number, run_id, agent_key, model_key,
   task_id, attempt, has_reward, has_trajectory, object_count, total_bytes, last_modified)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
ON CONFLICT (s3_prefix) DO NOTHING
"""


def _parse_trial(key: str):
    """Collapse an object key to its trial identity, or None if not a trial object.

    Handles all three legacy layouts seen in S3, anchored on the agent- segment:
      1. attempt:  .../agent-{k}:{m}/{task}/attempt_N/...          (attempt=N)
      2. native:   .../agent-{k}:{m}/{task}/{task}__{randomId}/... (Harbor trial dir)
      3. flat:     .../agent-{k}:{m}/{task}/...                    (files under task dir)

    The trial ROOT is the directory holding the trial's files; everything below it
    (verifier/, agent/, task/, result.json, ...) belongs to that one trial.
    """
    parts = key.split("/")
    gi = next((i for i, p in enumerate(parts) if p.startswith("agent-")), None)
    if gi is None or gi + 2 >= len(parts):
        return None  # need agent/{task}/{something-below}

    rest = parts[gi][len("agent-") :]
    agent_key, model_key = rest.split(":", 1) if ":" in rest else (rest, "")
    task_id = parts[gi + 1]

    sub = parts[gi + 2]
    if _ATT.fullmatch(sub):
        trial_end, attempt = gi + 3, int(sub.split("_")[1])   # attempt layout
    elif "__" in sub:
        trial_end, attempt = gi + 3, None                     # native trial dir
    else:
        trial_end, attempt = gi + 2, None                     # flat: task dir is root

    trial_prefix = "/".join(parts[:trial_end]) + "/"

    pr_number = run_id = None
    pr_idx = None
    for i, p in enumerate(parts[:gi]):
        if _PR.fullmatch(p):
            pr_number, pr_idx = int(p[3:]), i
        elif p.startswith("run-") and run_id is None:
            run_id = p[4:]

    s3_base = "/".join(parts[:pr_idx]) if pr_idx is not None else None
    return {
        "s3_prefix": trial_prefix,
        "s3_base": s3_base,
        "pr_number": pr_number,
        "run_id": run_id,
        "agent_key": agent_key,
        "model_key": model_key or None,
        "task_id": task_id,
        "attempt": attempt,
    }


def _file_shape(key: str):
    """Normalize an object key to a stable pattern so we can inventory the
    distinct *kinds* of files in the bucket (manifests, results, artifacts).

    Runs on EVERY object, trial or not, so run/job-level files (e.g.
    experiment-manifest.yaml, progress.json, result.json) show up too.
    Volatile segments are collapsed; the tail is kept up to 2 levels deep.
    """
    if key.endswith("/"):
        return None
    parts = key.split("/")
    ai = next((i for i, p in enumerate(parts) if _ATT.fullmatch(p)), None)
    if ai is not None:
        rel, prefix = parts[ai + 1 :], "attempt_N/"
    else:
        ri = next((i for i, p in enumerate(parts) if p.startswith("run-")), None)
        rel = parts[ri + 1 :] if ri is not None else parts[1:]
        prefix = "run-ID/" if ri is not None else "<root>/"
    if not rel:
        return None
    head = rel[0]
    if head.startswith("agent-"):
        head = "agent-X"
    elif _PR.fullmatch(head):
        head = "pr-N"
    elif head.startswith("run-"):
        head = "run-ID"
    if len(rel) == 1:
        tail = head
    elif len(rel) == 2:
        tail = f"{head}/{rel[1]}"
    else:
        tail = f"{head}/**"
    return prefix + tail


@app.function(image=image, secrets=[secret, aws_secret], timeout=60 * 60 * 12)
async def discover(execute: bool, concurrency: int, bucket: str, root_prefix: str) -> None:
    import asyncio
    import time

    import aioboto3
    import asyncpg
    from oddish.config import settings

    region = settings.s3_region or "us-east-1"
    session = aioboto3.Session()

    # ------------------------------------------------------------------ shards
    # Discover one level of child prefixes under the root so the full paginated
    # scans run in parallel (natural per-pr / per-run sharding, balances itself).
    async with session.client("s3", region_name=region) as s3:
        paginator = s3.get_paginator("list_objects_v2")
        shards: list[str] = []
        async for page in paginator.paginate(
            Bucket=bucket, Prefix=root_prefix, Delimiter="/"
        ):
            shards += [cp["Prefix"] for cp in page.get("CommonPrefixes", [])]
        # Two levels deep when the first level is just repo dirs (pr-* underneath).
        deeper: list[str] = []
        for shard in shards:
            async for page in paginator.paginate(
                Bucket=bucket, Prefix=shard, Delimiter="/"
            ):
                deeper += [cp["Prefix"] for cp in page.get("CommonPrefixes", [])]
        shards = deeper or shards

    # Resumability: skip shards already fully scanned in a prior --execute run.
    # (Dry-runs don't record, so they always scan everything.)
    total_shards = len(shards)
    if execute:
        c0 = await asyncpg.connect(settings.asyncpg_url, statement_cache_size=0)
        await c0.execute(DDL)
        await c0.execute(SHARDS_DDL)
        done = {r[0] for r in await c0.fetch("SELECT shard FROM leg_shards")}
        await c0.close()
        shards = [s for s in shards if s not in done]
        print(f"resume: {len(done)} shards already done, {len(shards)}/{total_shards} to scan")

    print(f"shards={len(shards)} concurrency={concurrency} execute={execute} bucket={bucket} root={root_prefix}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)
    sem = asyncio.Semaphore(concurrency)
    stats = {"objects": 0, "trials": 0, "with_reward": 0, "shards_done": 0, "orphans": 0}
    # shape -> [count, example_key]. Safe to share: asyncio is single-threaded
    # and we only touch it synchronously (no await between read and write).
    shapes: dict[str, list] = {}
    # Trial-signature files (reward/result) sitting OUTSIDE any attempt_ folder =
    # proof of flat-layout trials our parser would miss. Capture a few examples.
    orphan_examples: list[str] = []

    async def scan(shard: str) -> None:
        async with sem, session.client("s3", region_name=region) as s3:
            pg = s3.get_paginator("list_objects_v2")
            trials: dict[str, dict] = {}
            async for page in pg.paginate(Bucket=bucket, Prefix=shard):
                for obj in page.get("Contents", []):
                    stats["objects"] += 1
                    key = obj["Key"]
                    if "/attempt_" not in key and (
                        key.endswith("/reward.txt") or key.endswith("/result.json")
                    ):
                        stats["orphans"] += 1
                        if len(orphan_examples) < 20:
                            orphan_examples.append(key)
                    sh = _file_shape(obj["Key"])
                    if sh:
                        e = shapes.get(sh)
                        if e is None:
                            shapes[sh] = [1, obj["Key"]]
                        else:
                            e[0] += 1
                    rec = _parse_trial(obj["Key"])
                    if rec is None:
                        continue
                    t = trials.get(rec["s3_prefix"])
                    if t is None:
                        rec.update(
                            has_reward=False, has_trajectory=False, is_trial=False,
                            object_count=0, total_bytes=0, last_modified=None,
                        )
                        t = trials[rec["s3_prefix"]] = rec
                    t["object_count"] += 1
                    t["total_bytes"] += obj.get("Size", 0)
                    lm = obj.get("LastModified")
                    if lm and (t["last_modified"] is None or lm > t["last_modified"]):
                        t["last_modified"] = lm
                    k = obj["Key"]
                    if k.endswith("verifier/reward.txt") or k.endswith("/reward.txt"):
                        t["has_reward"] = True
                    if "trajectory-analysis" in k:
                        t["has_trajectory"] = True
                    # Signature files that mark a dir as a real trial root (vs bloat).
                    if (
                        k.endswith("/result.json")
                        or k.endswith("/config.json")
                        or k.endswith("/trial.log")
                        or t["has_reward"]
                    ):
                        t["is_trial"] = True
            # Only emit groups proven to be real trials by a signature file.
            emitted = 0
            for t in trials.values():
                if t["is_trial"]:
                    await queue.put(t)
                    emitted += 1
            stats["shards_done"] += 1
            # Marker so the writer records this shard as done AFTER its trials
            # are flushed (crash-safe resume).
            await queue.put({"__shard_done__": shard, "trials": emitted})

    async def writer() -> None:
        conn = None
        if execute:
            # statement_cache_size=0 is REQUIRED behind Supabase's transaction
            # pooler (Supavisor/PgBouncer): prepared statements don't survive
            # the per-transaction backend swap. Mirrors oddish.db.connection.
            conn = await asyncpg.connect(settings.asyncpg_url, statement_cache_size=0)
            await conn.execute(DDL)
        batch: list[tuple] = []

        async def flush():
            if execute and batch:
                await conn.executemany(INSERT_SQL, batch)
            batch.clear()

        while True:
            t = await queue.get()
            if t is None:
                break
            # Shard-done marker: flush pending trials, THEN record the shard so a
            # crash never marks a shard done with unflushed trials.
            if "__shard_done__" in t:
                await flush()
                if execute:
                    await conn.execute(
                        SHARDS_INSERT, t["__shard_done__"], t["trials"]
                    )
                continue
            stats["trials"] += 1
            if t["has_reward"]:
                stats["with_reward"] += 1
            if execute:
                batch.append((
                    t["s3_prefix"], bucket, t["s3_base"], t["pr_number"], t["run_id"],
                    t["agent_key"], t["model_key"], t["task_id"], t["attempt"],
                    t["has_reward"], t["has_trajectory"], t["object_count"],
                    t["total_bytes"], t["last_modified"],
                ))
                if len(batch) >= 5000:
                    await flush()
            if stats["trials"] % 50000 == 0:
                print(f"  progress trials={stats['trials']} objects={stats['objects']} shards_done={stats['shards_done']}/{len(shards)}")
        await flush()
        if conn:
            await conn.close()

    t0 = time.time()
    wtask = asyncio.create_task(writer())
    await asyncio.gather(*(scan(s) for s in shards))
    await queue.put(None)
    await wtask

    dt = time.time() - t0
    print("=" * 60)
    print(f"DONE in {dt:.0f}s  objects={stats['objects']}  trials={stats['trials']}  with_reward={stats['with_reward']}")
    print(f"mode={'WROTE to leg_trial_ledger' if execute else 'DRY-RUN (no writes)'}")

    # Orphan trial-signature files = flat-layout trials the parser would miss.
    print(f"ORPHAN trial-signature files (reward/result outside attempt_): {stats['orphans']}")
    for ex in orphan_examples:
        print(f"    orphan: {ex}")
    if stats["orphans"] == 0:
        print("    -> none: no flat-layout trials missed.")

    # File-shape inventory: the definitive vocabulary of every object kind.
    print("-" * 60)
    print(f"DISTINCT FILE SHAPES ({len(shapes)}):")
    for sh, (cnt, ex) in sorted(shapes.items(), key=lambda kv: -kv[1][0]):
        print(f"  {cnt:>12}  {sh}")
    if execute and shapes:
        conn2 = await asyncpg.connect(settings.asyncpg_url, statement_cache_size=0)
        await conn2.execute(SHAPES_DDL)
        await conn2.executemany(
            SHAPES_INSERT, [(sh, cnt, ex) for sh, (cnt, ex) in shapes.items()]
        )
        await conn2.close()
        print("wrote leg_file_shapes")


@app.local_entrypoint()
def main(
    execute: bool = False,
    concurrency: int = 32,
    bucket: str = DEFAULT_BUCKET,
    root: str = DEFAULT_ROOT,
) -> None:
    discover.remote(
        execute=execute, concurrency=concurrency, bucket=bucket, root_prefix=root
    )
