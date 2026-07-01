"""Transfer legacy Sauron trials from leg_trial_ledger into the Oddish DB.

Reuses the production importer (``initialize_trial_import``, origin=IMPORTED,
terminal rows, never re-runs a trial). Order per the design:

  tasks (dedup by name, run_analysis/run_probe=False)  -- must exist first
  -> experiments (one per run root, human-named from the manifest)
  -> trials (imported in run order); Sauron source recorded in orig_s3_src only.
     trial_s3_key is left canonical/untouched (it is a key inside Oddish's own
     bucket -- a legacy prefix there would 404). Artifacts stay in the Sauron
     bucket; rendering them is a follow-up (copy or legacy-bucket read path).

  Name collisions: a same-name pre-existing task with run_analysis=TRUE is never
  merged into (ledger rows marked status='conflict', skipped, for manual review --
  importing into it would enqueue QA). A same-name task with run_analysis=FALSE
  is safe and gets the legacy trials attached (merge).

Everything lands in org 8ebde5d0 ("Abundant") with heavy provenance (source tag,
legacy_s3_prefix anchor, run/pr/base) so cross-org duplicates can be reconciled
later. ``imported_at`` is stamped on tasks/trials/experiments for audit/rollback.

Idempotent: external_trial_id = legacy_s3_prefix -> a unique idempotency_key, and
the ledger ``status`` gates re-runs. Scope to the pilot with --scope-pr / --scope-run.

Usage:
    modal run backend/scripts/legacy_transfer.py --scope-pr 509                 # DRY RUN
    modal run backend/scripts/legacy_transfer.py --scope-pr 509 --execute       # write
    modal run backend/scripts/legacy_transfer.py --execute                      # full run

FINALIZE-LATER (marked in code): exact result.json keys for tokens/timing, and
model recovery (config.json vs cleaned model_key) -- confirm against a real
result.json/config.json from the pilot.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import modal

_parents = Path(__file__).resolve().parents
REPO = _parents[2] if len(_parents) > 2 else Path("/")

app = modal.App("oddish-legacy-transfer")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    .add_local_dir(str(REPO / "oddish"), remote_path="/oddish", copy=True, ignore=[".venv/", ".git/"])
    .run_commands("pip install uv", "cd /oddish && uv pip install --system '.[worker]'")
)
secret = modal.Secret.from_name("oddish-prod", environment_name="main")
aws_secret = modal.Secret.from_name("sauron-legacy")

DEFAULT_BUCKET = "abundant-github-workflows-bucket"
ORG_ID = "8ebde5d0"           # live "Abundant" org (confirmed)
IMPORT_TAG = "sauron-migration"
PROVIDERS = ("gemini", "openai", "anthropic", "xai", "google", "fireworks", "bedrock")


def _clean_model(model_key: str | None) -> str | None:
    """Best-effort un-collapse of the lossy path model_key (e.g.
    ``gemini-gemini-3.1-pro-preview`` -> ``gemini-3.1-pro-preview``). The
    importer additionally runs settings.normalize_trial_model on whatever we
    pass. FINALIZE-LATER: prefer config.json's model when present."""
    if not model_key or model_key == "-":
        return None
    for p in PROVIDERS:
        if model_key.startswith(p + "-"):
            return model_key[len(p) + 1:]
    return model_key


def _root_of(prefix: str) -> str:
    """Run-root path = everything above the ``agent-`` segment. Unique per run
    (or merged bundle) regardless of whether a clean ``run_id`` parsed."""
    return prefix.split("/agent-")[0]


def _exp_id(root: str) -> str:
    """Deterministic experiment id from the run-root PATH -- not the raw run_id.
    Kills the ``leg-None`` collision (trials with no ``run-`` segment used to
    share one experiment); each distinct run-root now gets its own id."""
    return "leg-" + hashlib.sha256(root.encode()).hexdigest()[:12]


def _ext_id(prefix: str) -> str:
    """Short deterministic idempotency anchor for external_trial_id. The importer
    wraps it as ``import:{ext}:{experiment_id}``; 16 hex chars keeps the whole
    idempotency_key well under trials.idempotency_key's VARCHAR(64) (the full S3
    prefix blew past 64 and failed every insert)."""
    return hashlib.sha256(prefix.encode()).hexdigest()[:16]


@app.function(image=image, secrets=[secret, aws_secret], timeout=60 * 60 * 6)
async def transfer(execute: bool, scope_pr: int | None, scope_run: str | None,
                   scope_base: str | None, limit: int | None) -> None:
    import json
    from datetime import datetime, timezone
    from itertools import groupby
    from uuid import uuid4

    import aioboto3
    import yaml
    from sqlalchemy import select, text

    from oddish.config import settings
    from oddish.core.ingest.trial_imports import initialize_trial_import
    from oddish.db import get_session
    from oddish.db.models import (
        ExperimentModel, TaskModel, TaskVersionModel, TrialModel,
    )
    from oddish.schemas import ImportedTrialSpec

    now = lambda: datetime.now(timezone.utc)  # noqa: E731
    gen_id = lambda: str(uuid4())[:8]         # noqa: E731

    # ---- 1. work list from the ledger (scope, pending only, run order) ------
    where = "status = 'discovered'"
    params: dict = {}
    if scope_base:
        where += " AND s3_base = :base"; params["base"] = scope_base
    if scope_run:
        where += " AND run_id = :run"; params["run"] = scope_run
    if scope_pr is not None:
        where += " AND pr_number = :pr"; params["pr"] = scope_pr
    sql = (f"SELECT s3_prefix, s3_base, pr_number, run_id, agent_key, model_key, "
           f"task_id, attempt, has_reward, has_trajectory, last_modified "
           f"FROM leg_trial_ledger WHERE {where} "
           f"ORDER BY task_id, last_modified, attempt NULLS FIRST")
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with get_session() as s:
        rows = [dict(m) for m in (await s.execute(text(sql), params)).mappings().all()]

    n_tasks = len({r["task_id"] for r in rows})
    n_exps = len({_root_of(r["s3_prefix"]) for r in rows})
    print(f"scope: trials={len(rows)} tasks={n_tasks} experiments={n_exps} execute={execute}")
    if not rows:
        print("nothing to do")
        return
    if not execute:
        print("DRY RUN — would create the above tasks/experiments/trials. No writes.")
        return

    # ---- 2. prefetch run manifests (one S3 read per distinct run-root) -------
    roots = sorted({_root_of(r["s3_prefix"]) for r in rows})
    manifests: dict[str, dict] = {}
    aio = aioboto3.Session()
    region = settings.s3_region or "us-east-1"

    async def _get(s3, key: str) -> bytes | None:
        try:
            obj = await s3.get_object(Bucket=DEFAULT_BUCKET, Key=key)
            return await obj["Body"].read()
        except Exception:
            return None

    async with aio.client("s3", region_name=region) as s3:
        for root in roots:
            body = await _get(s3, f"{root}/experiment-manifest.yaml")
            try:
                manifests[root] = yaml.safe_load(body) if body else {}
            except Exception:
                manifests[root] = {}

        # ---- helpers --------------------------------------------------------
        async def build_spec(r: dict) -> ImportedTrialSpec:
            prefix = r["s3_prefix"]
            # reward: plain-number files first, then reward.json fallback. Most
            # has_reward=false trials genuinely have none (harness errors) and
            # correctly become reward=None/FAILED; this fallback just catches the
            # ~0.2% that store their score as reward.json/reward-float.txt.
            reward = None
            for k in (f"{prefix}verifier/reward.txt",
                      f"{prefix}verifier/verifier/reward.txt",
                      f"{prefix}verifier/reward-float.txt"):
                b = await _get(s3, k)
                if b is not None:
                    try:
                        reward = float(b.decode().strip()); break
                    except ValueError:
                        pass
            if reward is None:
                b = await _get(s3, f"{prefix}verifier/reward.json")
                if b:
                    try:
                        j = json.loads(b)
                        if isinstance(j, (int, float)):
                            reward = float(j)
                        elif isinstance(j, dict):
                            for key in ("reward", "score", "value"):
                                v = j.get(key)
                                if isinstance(v, (int, float)):
                                    reward = float(v); break
                    except Exception:
                        pass
            # tokens/timing: result.json (FINALIZE-LATER: confirm exact keys)
            rj = await _get(s3, f"{prefix}result.json")
            result = {}
            if rj:
                try:
                    result = json.loads(rj)
                except Exception:
                    result = {}
            # model: prefer config.json, else cleaned path model_key
            model = _clean_model(r["model_key"])
            cj = await _get(s3, f"{prefix}config.json")
            if cj:
                try:
                    cfg = json.loads(cj)
                    model = cfg.get("model") or cfg.get("model_name") or model
                except Exception:
                    pass
            status = "success" if reward is not None else "failed"
            lm = r["last_modified"]
            return ImportedTrialSpec(
                agent=r["agent_key"],
                model=model,
                status=status,
                reward=reward,
                has_trajectory=bool(r["has_trajectory"]),
                input_tokens=result.get("input_tokens"),
                output_tokens=result.get("output_tokens"),
                cache_tokens=result.get("cache_tokens"),
                total_steps=result.get("total_steps"),
                cost_usd=result.get("cost_usd"),
                phase_timing=result.get("phase_timing"),
                started_at=lm,
                finished_at=lm,
                external_trial_id=_ext_id(prefix),  # short hash -> key fits VARCHAR(64)
                # source tag distinguishes THIS migration from prior test imports;
                # the immutable path anchor lives in the trials.orig_s3_src column.
                # legacy_* structured fields kept for cheap querying/scoping.
                harbor_config={
                    "source": IMPORT_TAG,
                    "legacy_s3_base": r["s3_base"],
                    "legacy_pr_number": r["pr_number"],
                    "legacy_run_id": r["run_id"],
                    "legacy_agent_key": r["agent_key"],
                    "legacy_model_key": r["model_key"],
                },
            )

        # ---- 3. per-task: create task + experiments, then import its trials --
        created = {"tasks": 0, "experiments": 0, "trials": 0, "skipped": 0,
                   "errors": 0, "conflicts": 0}
        rows.sort(key=lambda r: r["task_id"])
        for task_id, group in groupby(rows, key=lambda r: r["task_id"]):
            grp = list(group)

            # 3a. task + experiments (committed so the importer can see them)
            async with get_session() as sess:
                task = (await sess.execute(
                    select(TaskModel).where(TaskModel.org_id == ORG_ID, TaskModel.name == task_id)
                )).scalar_one_or_none()
                if task is not None and task.run_analysis:
                    # Name collides with a pre-existing task whose
                    # run_analysis=TRUE: importing into it would enqueue QA jobs
                    # (maybe_start_qa_stage), and the flag isn't ours to mutate.
                    # Mark every ledger row for this task as a conflict and skip;
                    # expected to be extremely rare -- review manually via
                    # SELECT * FROM leg_trial_ledger WHERE status='conflict'.
                    # A same-name task with run_analysis=FALSE is safe to merge
                    # into (attaching trials to an analysis-off task enqueues
                    # nothing), so it falls through to reuse below.
                    for r in grp:
                        await sess.execute(text(
                            "UPDATE leg_trial_ledger SET status='conflict', "
                            "error=:e WHERE s3_prefix=:p"),
                            {"e": f"name collision with run_analysis=true task: tasks.id={task.id}",
                             "p": r["s3_prefix"]})
                    await sess.commit()
                    created["conflicts"] += len(grp)
                    print(f"  CONFLICT task={task_id!r} collides with run_analysis=true "
                          f"tasks.id={task.id}; skipped {len(grp)} trials")
                    continue
                if task is None:
                    tid = gen_id()
                    legacy_task_path = f"s3://{DEFAULT_BUCKET}/{grp[0]['s3_prefix']}task"
                    task = TaskModel(
                        id=tid, name=task_id, org_id=ORG_ID, user=IMPORT_TAG,
                        task_path=legacy_task_path, task_s3_key=f"{grp[0]['s3_prefix']}task/",
                        tags={"source": IMPORT_TAG, "legacy_task_id": task_id,
                              "legacy_repos": sorted({r["s3_base"] for r in grp})},
                        run_analysis=False, run_probe=False, imported_at=now(),
                    )
                    sess.add(task)
                    await sess.flush()
                    ver = TaskVersionModel(
                        id=f"{tid}-v1", task_id=tid, version=1,
                        task_path=legacy_task_path, task_s3_key=f"{grp[0]['s3_prefix']}task/",
                    )
                    sess.add(ver)
                    await sess.flush()
                    task.current_version_id = ver.id
                    created["tasks"] += 1
                task_pk = task.id

                exp_ids: dict[str, str] = {}   # run-root path -> experiment id
                for root in sorted({_root_of(r["s3_prefix"]) for r in grp}):
                    eid = _exp_id(root)
                    exp = await sess.get(ExperimentModel, eid)
                    if exp is None:
                        m = manifests.get(root, {}) or {}
                        meta = m.get("metadata", {}) or {}
                        rt = m.get("runtime", {}) or {}
                        exp = ExperimentModel(
                            id=eid, org_id=ORG_ID,
                            name=meta.get("name") or f"Legacy {root.rsplit('/', 1)[-1]}",
                            description=meta.get("description"),
                            owner=rt.get("repository"),
                            link=rt.get("workflow_url"),
                            imported_at=now(),
                            orig_s3_src=f"{root}/",  # immutable run-root anchor
                        )
                        sess.add(exp)
                        await sess.flush()
                        created["experiments"] += 1
                    exp_ids[root] = eid
                await sess.commit()

            # 3b. import this task's trials SEQUENTIALLY (preserves run-order index)
            for r in grp:
                root = _root_of(r["s3_prefix"])
                eid = exp_ids[root]
                # deterministic idempotency key the importer will build, so we can
                # find the row again on a collision (crashed resume).
                idem = f"import:{_ext_id(r['s3_prefix'])}:{eid}"
                trial_id = None
                fresh = False
                try:
                    spec = await build_spec(r)
                    resp = await initialize_trial_import(
                        task_id=task_pk,
                        experiment_id_or_name=eid,
                        trial_spec=spec,
                        upload_artifacts=False,   # point at Sauron, do not copy blobs
                        org_id=ORG_ID,
                    )
                    trial_id = resp.trial_id
                    fresh = True
                except Exception as e:
                    msg = str(e).lower()
                    if "idempotency" in msg or "unique" in msg or "duplicate" in msg:
                        # Already imported on a prior (possibly crashed) run. Find
                        # the existing row so we STILL apply the Sauron-pointer
                        # patch below -- a crash between the importer's commit and
                        # that patch would otherwise strand it on empty Oddish
                        # storage. If we can't find it, we still advance the ledger.
                        async with get_session() as sess:
                            trial_id = await sess.scalar(
                                text("SELECT id FROM trials WHERE idempotency_key=:k"),
                                {"k": idem})
                        created["skipped"] += 1
                    else:
                        created["errors"] += 1
                        async with get_session() as sess:
                            await sess.execute(text(
                                "UPDATE leg_trial_ledger SET status='failed', error=:e "
                                "WHERE s3_prefix=:p"), {"e": str(e)[:500], "p": r["s3_prefix"]})
                            await sess.commit()
                        print(f"  ERROR {r['s3_prefix']}: {e!r}")
                        continue

                # Record the Sauron source in orig_s3_src ONLY. We deliberately do
                # NOT touch trial_s3_key: it is a key within Oddish's own bucket
                # (settings.s3_bucket) -- writing the legacy prefix there points the
                # file viewer at a non-existent path in the wrong bucket. Leaving
                # the canonical (empty) prefix means the UI shows "no files" for
                # legacy trials until a follow-up copies artifacts or teaches the
                # read path about the legacy bucket. Runs for BOTH a fresh import
                # and a resume-collision, so state always converges.
                async with get_session() as sess:
                    if trial_id is not None:
                        await sess.execute(text(
                            "UPDATE trials SET orig_s3_src=:k, "
                            "imported_at=COALESCE(imported_at, :t) WHERE id=:id"),
                            {"k": r["s3_prefix"], "t": now(), "id": trial_id})
                    await sess.execute(text(
                        "UPDATE leg_trial_ledger SET status='transferred', "
                        "oddish_trial_id=:id, transferred_at=:t WHERE s3_prefix=:p"),
                        {"id": trial_id, "t": now(), "p": r["s3_prefix"]})
                    await sess.commit()
                if fresh:
                    created["trials"] += 1

    print("=" * 60)
    print(f"DONE  tasks+={created['tasks']} experiments+={created['experiments']} "
          f"trials+={created['trials']} skipped={created['skipped']} "
          f"errors={created['errors']} conflicts={created['conflicts']}")
    if created["conflicts"]:
        print("review conflicts: SELECT * FROM leg_trial_ledger WHERE status='conflict';")


@app.local_entrypoint()
def main(execute: bool = False, scope_pr: int | None = None, scope_run: str | None = None,
         scope_base: str | None = None, limit: int | None = None) -> None:
    transfer.remote(execute=execute, scope_pr=scope_pr, scope_run=scope_run,
                    scope_base=scope_base, limit=limit)
