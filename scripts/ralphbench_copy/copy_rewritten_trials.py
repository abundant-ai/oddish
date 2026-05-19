"""Copy rewritten-task trials from Oddish into s3://ralphbench-logs/.

Operates on experiment `cd8c33d8` for the four tasks:
    embedding-eval, ruby-rust-port, trimul-cuda, parameter-golf.

Selection rules:
- task_version_id == task.current_version_id (the "rewritten" version)
- superseded_by_trial_id IS NULL
- reward IS NOT NULL (numeric, including 0.0)
- analysis classification != BAD_SUCCESS (excludes reward-hacked trials)
- Must have at least one canonical inner ``task-*/`` dir containing
  ``agent/``, ``artifacts/``, ``verifier/`` and a numeric reward in its
  inner ``result.json``.
- Dedupe across (id, trial_name, task_checksum) of inner ``result.json``.
- Cap at 5 trials per (agent, model). When more than 5 valid candidates
  exist for a pair, rank by total artifact bytes (proxy for trajectory
  size) descending and keep the top 5. Pruned trials are moved to a
  timestamped `trash/<task>/<run>` archive prefix instead of being
  uploaded.

Layout (per task):
    s3://ralphbench-logs/<task_name>/
        _manifest.json
        <task_name>-<task_hash>-<index>/
            config.json
            result.json
            job.log
            lock.json
            modal-output.log
            task-.../
                config.json
                result.json
                trial.log
                agent/
                artifacts/
                verifier/

Metadata patched into root config.json + result.json:
    task_name, task_hash, task_version_id
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aioboto3
import boto3
from botocore.config import Config

sys.path.insert(0, "/workspace/oddish/src")

from sqlalchemy import text
from oddish.db.connection import get_session


# -- Configuration ------------------------------------------------------------

EXPERIMENT_ID = "cd8c33d8"

TASKS: dict[str, dict[str, str]] = {
    # task_id -> {task_name, task_hash, current_version_id}
    "embedding-eval-be1dc228": {
        "task_name": "embedding-eval",
        "task_hash": "be1dc228",
        "task_version_id": "embedding-eval-be1dc228-v13",
    },
    "ruby-rust-port-383776ff": {
        "task_name": "ruby-rust-port",
        "task_hash": "383776ff",
        "task_version_id": "ruby-rust-port-383776ff-v13",
    },
    "trimul-cuda-fc34aaba": {
        "task_name": "trimul-cuda",
        "task_hash": "fc34aaba",
        "task_version_id": "trimul-cuda-fc34aaba-v81",
    },
    "parameter-golf-61e11605": {
        "task_name": "parameter-golf",
        "task_hash": "61e11605",
        "task_version_id": "parameter-golf-61e11605-v31",
    },
}

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"

SRC_BUCKET = os.environ["ODDISH_S3_BUCKET"]
SRC_ENDPOINT = os.environ["ODDISH_S3_ENDPOINT_URL"]
SRC_ACCESS_KEY = os.environ["ODDISH_S3_ACCESS_KEY"]
SRC_SECRET_KEY = os.environ["ODDISH_S3_SECRET_KEY"]

DEST_ACCESS_KEY = os.environ["AWS_ACCESS_KEY_ID"]
DEST_SECRET_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]

REQUIRED_INNER_DIRS = ("verifier",)
PREFERRED_INNER_DIRS = ("agent", "artifacts", "verifier")
REQUIRED_INNER_FILES = ("config.json", "result.json", "trial.log")
ROOT_REQUIRED_FILES = ("config.json", "result.json", "job.log", "lock.json", "modal-output.log")

CAP_PER_AGENT_MODEL = 5


# -- DB query ----------------------------------------------------------------


@dataclass
class TrialRow:
    id: str
    name: str
    task_id: str
    task_version_id: str
    agent: str
    model: str | None
    provider: str
    status: str
    reward: float
    error_message: str | None
    attempts: int
    max_attempts: int
    trial_s3_key: str | None
    started_at: datetime | None
    finished_at: datetime | None
    input_tokens: int | None
    cache_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    has_trajectory: bool
    phase_timing: dict | None
    analysis: dict | None


async def fetch_candidates() -> list[TrialRow]:
    rows: list[TrialRow] = []
    async with get_session() as session:
        for task_id, meta in TASKS.items():
            r = await session.execute(
                text(
                    """
                    SELECT id, name, task_id, task_version_id,
                           agent, model, provider, status::text, reward,
                           error_message, attempts, max_attempts, trial_s3_key,
                           started_at, finished_at,
                           input_tokens, cache_tokens, output_tokens, cost_usd,
                           has_trajectory, phase_timing, analysis
                    FROM trials
                    WHERE experiment_id = :exp
                      AND task_id = :tid
                      AND task_version_id = :vid
                      AND superseded_by_trial_id IS NULL
                      AND deleted_at IS NULL
                      AND reward IS NOT NULL
                    ORDER BY id
                    """
                ),
                {"exp": EXPERIMENT_ID, "tid": task_id, "vid": meta["task_version_id"]},
            )
            for row in r:
                m = row._mapping
                rows.append(TrialRow(**dict(m)))
    return rows


# -- S3 clients --------------------------------------------------------------


def make_src_client():
    sess = aioboto3.Session()
    return sess.client(
        "s3",
        endpoint_url=SRC_ENDPOINT,
        aws_access_key_id=SRC_ACCESS_KEY,
        aws_secret_access_key=SRC_SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )


def make_dest_sync_client():
    return boto3.client(
        "s3",
        aws_access_key_id=DEST_ACCESS_KEY,
        aws_secret_access_key=DEST_SECRET_KEY,
        region_name=DEST_REGION,
    )


def make_dest_async_client():
    sess = aioboto3.Session()
    return sess.client(
        "s3",
        aws_access_key_id=DEST_ACCESS_KEY,
        aws_secret_access_key=DEST_SECRET_KEY,
        region_name=DEST_REGION,
    )


# -- S3 listing helpers ------------------------------------------------------


async def list_trial_keys(src_s3, prefix: str) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    paginator = src_s3.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=SRC_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append({"key": obj["Key"], "size": obj["Size"]})
    return keys


# -- Validation ---------------------------------------------------------------


@dataclass
class CanonicalDir:
    inner_name: str  # e.g., "task-parameter-golf-61e11605-0yu__hTtBorD"
    inner_result: dict[str, Any]
    inner_reward: float
    has_required_dirs: bool
    artifact_bytes: int


@dataclass
class ValidatedTrial:
    row: TrialRow
    src_prefix: str
    all_keys: list[dict[str, Any]]
    canonical: CanonicalDir
    total_bytes: int
    fingerprint: tuple[str, str, str]
    excluded: str | None = None


async def get_json(src_s3, key: str) -> dict | None:
    try:
        r = await src_s3.get_object(Bucket=SRC_BUCKET, Key=key)
        async with r["Body"] as body:
            data = await body.read()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


async def validate_trial(src_s3, row: TrialRow) -> ValidatedTrial | None:
    prefix = row.trial_s3_key
    if not prefix:
        return ValidatedTrial(row=row, src_prefix="", all_keys=[], canonical=None, total_bytes=0, fingerprint=("", "", ""), excluded="no trial_s3_key")  # type: ignore
    if not prefix.endswith("/"):
        prefix += "/"

    keys = await list_trial_keys(src_s3, prefix)
    if not keys:
        return ValidatedTrial(row=row, src_prefix=prefix, all_keys=[], canonical=None, total_bytes=0, fingerprint=("", "", ""), excluded="empty s3 prefix")  # type: ignore

    rel_keys = []
    for k in keys:
        rel = k["key"][len(prefix):]
        rel_keys.append({"key": k["key"], "rel": rel, "size": k["size"]})

    # Group by top-level dir
    inner_dirs: dict[str, list[dict[str, Any]]] = {}
    root_files: dict[str, dict[str, Any]] = {}
    for k in rel_keys:
        rel = k["rel"]
        if "/" not in rel:
            root_files[rel] = k
            continue
        first, _, _ = rel.partition("/")
        inner_dirs.setdefault(first, []).append(k)

    # Find canonical inner dir. Required: top-level task-* dir whose
    # ``result.json`` carries a numeric verifier reward and that has at
    # minimum a ``verifier/`` subdir. ``agent/`` and ``artifacts/`` are
    # preferred but not required (nop baselines, oracle-only trials, and
    # some failure modes omit them).
    canonical_candidates: list[CanonicalDir] = []
    for inner_name, inner_keys in inner_dirs.items():
        if not inner_name.startswith("task-"):
            continue
        subdirs: set[str] = set()
        files_inside: set[str] = set()
        artifact_bytes = 0
        for ik in inner_keys:
            rel_inside = ik["rel"][len(inner_name) + 1:]
            if "/" in rel_inside:
                subdirs.add(rel_inside.split("/", 1)[0])
            else:
                files_inside.add(rel_inside)
            artifact_bytes += ik["size"]
        # Must have minimum files + minimum subdirs.
        if not all(f in files_inside for f in REQUIRED_INNER_FILES):
            continue
        if not all(d in subdirs for d in REQUIRED_INNER_DIRS):
            continue
        # Read inner result.json
        result_key = f"{prefix}{inner_name}/result.json"
        inner_result = await get_json(src_s3, result_key)
        if inner_result is None:
            continue
        verifier = inner_result.get("verifier_result") or {}
        rewards = verifier.get("rewards") or {}
        reward_val = rewards.get("reward")
        if not isinstance(reward_val, (int, float)):
            continue
        has_all_preferred = all(d in subdirs for d in PREFERRED_INNER_DIRS)
        canonical_candidates.append(
            CanonicalDir(
                inner_name=inner_name,
                inner_result=inner_result,
                inner_reward=float(reward_val),
                has_required_dirs=has_all_preferred,
                artifact_bytes=artifact_bytes,
            )
        )

    if not canonical_candidates:
        return ValidatedTrial(row=row, src_prefix=prefix, all_keys=rel_keys, canonical=None, total_bytes=0, fingerprint=("", "", ""), excluded="no canonical inner dir with verifier/ + result.json")  # type: ignore

    # Prefer the canonical dir whose inner_reward matches the trial's
    # row.reward; tie-break by largest artifact_bytes (= most-complete).
    canonical_candidates.sort(
        key=lambda c: (
            abs(c.inner_reward - row.reward) < 1e-9,
            c.has_required_dirs,
            c.artifact_bytes,
        ),
        reverse=True,
    )
    canonical = canonical_candidates[0]

    # Check required root files
    missing_root = [f for f in ROOT_REQUIRED_FILES if f not in root_files]
    if missing_root:
        return ValidatedTrial(row=row, src_prefix=prefix, all_keys=rel_keys, canonical=canonical, total_bytes=0, fingerprint=("", "", ""), excluded=f"missing root files {missing_root}")  # type: ignore

    # Fingerprint for dedup
    inner_id = str(canonical.inner_result.get("id", ""))
    inner_trial_name = str(canonical.inner_result.get("trial_name", ""))
    task_checksum = str(canonical.inner_result.get("task_checksum", ""))
    fingerprint = (inner_id, inner_trial_name, task_checksum)

    total_bytes = sum(k["size"] for k in rel_keys)

    # Classification check
    classification = None
    if row.analysis and isinstance(row.analysis, dict):
        classification = row.analysis.get("classification")
    excluded = None
    if classification == "BAD_SUCCESS":
        excluded = "BAD_SUCCESS (reward-hacked)"

    return ValidatedTrial(
        row=row,
        src_prefix=prefix,
        all_keys=rel_keys,
        canonical=canonical,
        total_bytes=total_bytes,
        fingerprint=fingerprint,
        excluded=excluded,
    )


# -- Selection ----------------------------------------------------------------


def select_keep_and_prune(validated: list[ValidatedTrial]) -> tuple[list[ValidatedTrial], list[ValidatedTrial], list[ValidatedTrial]]:
    """Return (keep, pruned_over_cap, excluded). Operates on a single task list."""

    valid: list[ValidatedTrial] = []
    excluded: list[ValidatedTrial] = []
    seen_fp: dict[tuple[str, str, str], ValidatedTrial] = {}
    for v in validated:
        if v.canonical is None or v.excluded:
            excluded.append(v)
            continue
        fp = v.fingerprint
        # Dedup: prefer the trial with the most total bytes
        if fp in seen_fp:
            other = seen_fp[fp]
            if v.total_bytes > other.total_bytes:
                # Replace; mark the other as excluded duplicate
                seen_fp[fp] = v
                other.excluded = f"duplicate of {v.row.id}"
                excluded.append(other)
            else:
                v.excluded = f"duplicate of {other.row.id}"
                excluded.append(v)
            continue
        seen_fp[fp] = v
        valid.append(v)

    # Now cap to 5 per (agent, model)
    keep: list[ValidatedTrial] = []
    pruned: list[ValidatedTrial] = []
    by_pair: dict[tuple[str, str], list[ValidatedTrial]] = {}
    for v in valid:
        pair = (v.row.agent, v.row.model or "")
        by_pair.setdefault(pair, []).append(v)
    for pair, items in by_pair.items():
        items.sort(key=lambda x: (x.total_bytes, x.row.id), reverse=True)
        keep.extend(items[:CAP_PER_AGENT_MODEL])
        pruned.extend(items[CAP_PER_AGENT_MODEL:])

    return keep, pruned, excluded


# -- Upload ------------------------------------------------------------------


async def copy_object(src_s3, dest_s3, src_key: str, dest_key: str, *, override_body: bytes | None = None) -> None:
    if override_body is not None:
        await dest_s3.put_object(Bucket=DEST_BUCKET, Key=dest_key, Body=override_body)
        return
    r = await src_s3.get_object(Bucket=SRC_BUCKET, Key=src_key)
    async with r["Body"] as body:
        data = await body.read()
    await dest_s3.put_object(Bucket=DEST_BUCKET, Key=dest_key, Body=data)


async def upload_trial(src_s3, dest_s3, v: ValidatedTrial, task_meta: dict[str, str], dest_trial_prefix: str) -> int:
    """Upload one trial: 5 root files + canonical inner dir.

    Returns number of objects uploaded.
    """
    n_uploaded = 0
    canonical_name = v.canonical.inner_name
    inner_prefix_rel = f"{canonical_name}/"

    # Load existing root config.json and result.json so we can patch in
    # task_name/task_hash/task_version_id and re-upload.
    root_config_key = f"{v.src_prefix}config.json"
    root_result_key = f"{v.src_prefix}result.json"
    root_config = await get_json(src_s3, root_config_key) or {}
    root_result = await get_json(src_s3, root_result_key) or {}
    root_config["task_name"] = task_meta["task_name"]
    root_config["task_hash"] = task_meta["task_hash"]
    root_config["task_version_id"] = task_meta["task_version_id"]
    root_result["task_name"] = task_meta["task_name"]
    root_result["task_hash"] = task_meta["task_hash"]
    root_result["task_version_id"] = task_meta["task_version_id"]

    await dest_s3.put_object(
        Bucket=DEST_BUCKET,
        Key=f"{dest_trial_prefix}config.json",
        Body=json.dumps(root_config, indent=4).encode("utf-8"),
        ContentType="application/json",
    )
    n_uploaded += 1
    await dest_s3.put_object(
        Bucket=DEST_BUCKET,
        Key=f"{dest_trial_prefix}result.json",
        Body=json.dumps(root_result, indent=4).encode("utf-8"),
        ContentType="application/json",
    )
    n_uploaded += 1

    # Copy other required root files verbatim (job.log, lock.json,
    # modal-output.log) -- plus any other files that live at the trial
    # root directly (defensive; usually nothing else).
    for k in v.all_keys:
        rel = k["rel"]
        if "/" in rel:
            continue
        if rel in ("config.json", "result.json"):
            continue
        await copy_object(src_s3, dest_s3, k["key"], f"{dest_trial_prefix}{rel}")
        n_uploaded += 1

    # Copy canonical inner dir.
    for k in v.all_keys:
        rel = k["rel"]
        if not rel.startswith(inner_prefix_rel):
            continue
        await copy_object(src_s3, dest_s3, k["key"], f"{dest_trial_prefix}{rel}")
        n_uploaded += 1

    return n_uploaded


# -- Manifest -----------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def build_manifest_entry(v: ValidatedTrial, dest_trial_prefix: str, task_meta: dict[str, str]) -> dict[str, Any]:
    row = v.row
    return {
        "agent": row.agent,
        "model": row.model,
        "provider": row.provider,
        "reward": row.reward,
        "status": row.status.lower(),
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "task_version_id": row.task_version_id,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "input_tokens": row.input_tokens,
        "cache_tokens": row.cache_tokens,
        "output_tokens": row.output_tokens,
        "cost_usd": row.cost_usd,
        "has_trajectory": bool(row.has_trajectory),
        "origin": "oddish",
        "artifact_layout": "harbor-nested",
        "artifact_prefix": f"s3://{DEST_BUCKET}/{dest_trial_prefix}",
        "source_trial_name": v.canonical.inner_name,
        "source_oddish_trial_id": row.id,
        "source_oddish_task_id": row.task_id,
        "error_message": row.error_message,
        "phase_timing": row.phase_timing,
        "total_artifact_bytes": v.total_bytes,
    }


# -- Main ---------------------------------------------------------------------


async def main() -> None:
    dry_run = "--apply" not in sys.argv
    only_task = None
    for arg in sys.argv[1:]:
        if arg.startswith("--task="):
            only_task = arg.split("=", 1)[1]
    target_tasks = TASKS if only_task is None else {tid: meta for tid, meta in TASKS.items() if meta["task_name"] == only_task or tid == only_task}
    if not target_tasks:
        print(f"No task matched --task={only_task}; valid task_names: {[t['task_name'] for t in TASKS.values()]}")
        sys.exit(2)

    print(f"Mode: {'APPLY (will upload)' if not dry_run else 'DRY RUN (no uploads)'}")
    print(f"Tasks: {[t['task_name'] for t in target_tasks.values()]}")

    # Pull DB rows
    print("Fetching candidate trials from DB...")
    all_rows = await fetch_candidates()
    rows_by_task: dict[str, list[TrialRow]] = {}
    for r in all_rows:
        if r.task_id in target_tasks:
            rows_by_task.setdefault(r.task_id, []).append(r)
    for tid, rows in rows_by_task.items():
        print(f"  {tid}: {len(rows)} candidates")

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict[str, dict[str, Any]] = {}

    async with make_src_client() as src_s3, make_dest_async_client() as dest_s3:
        for task_id, meta in target_tasks.items():
            rows = rows_by_task.get(task_id, [])
            task_name = meta["task_name"]
            task_hash = meta["task_hash"]
            print(f"\n=== {task_id} ({task_name}) ===")
            # Validate in parallel (bounded)
            sem = asyncio.Semaphore(16)

            async def _val(row):
                async with sem:
                    return await validate_trial(src_s3, row)

            validated = await asyncio.gather(*(_val(r) for r in rows))
            validated = [v for v in validated if v is not None]

            keep, pruned, excluded = select_keep_and_prune(validated)
            print(f"  validated total: {len(validated)}")
            print(f"  excluded: {len(excluded)}")
            for v in excluded:
                print(f"    - {v.row.id}: {v.excluded}")
            print(f"  pruned over cap: {len(pruned)}")
            print(f"  keep: {len(keep)}")
            # Per-pair counts
            pair_counts: dict[tuple[str, str], int] = {}
            for v in keep:
                p = (v.row.agent, v.row.model or "")
                pair_counts[p] = pair_counts.get(p, 0) + 1
            for p, c in sorted(pair_counts.items()):
                marker = "" if c <= CAP_PER_AGENT_MODEL else " ***OVER***"
                print(f"    {p[0]:14s} {p[1]:50s} -> {c}{marker}")

            task_summary = {
                "task": f"{task_name}-{task_hash}",
                "task_name": task_name,
                "task_hash": task_hash,
                "task_version_id": meta["task_version_id"],
                "n_keep": len(keep),
                "n_pruned": len(pruned),
                "n_excluded": len(excluded),
                "kept_trial_ids": [v.row.id for v in keep],
                "pruned_trial_ids": [v.row.id for v in pruned],
                "excluded_trial_ids": [(v.row.id, v.excluded) for v in excluded],
            }
            summary[task_id] = task_summary

            if dry_run:
                continue

            # Upload kept trials
            trials_section: dict[str, dict[str, Any]] = {}
            for v in keep:
                dest_dir_name = v.row.id  # already <task_name>-<task_hash>-<n>
                dest_prefix = f"{task_name}/{dest_dir_name}/"
                print(f"  -> uploading {dest_prefix} ({v.total_bytes/1e6:.2f} MB)")
                n_uploaded = await upload_trial(src_s3, dest_s3, v, meta, dest_prefix)
                print(f"     {n_uploaded} objects")
                trials_section[dest_dir_name] = build_manifest_entry(v, dest_prefix, meta)

            # Archive pruned trials under trash
            for v in pruned + [e for e in excluded if e.canonical is not None and not (e.excluded and e.excluded.startswith("duplicate"))]:
                # Only archive over-cap prunes (with full data) and BAD_SUCCESS exclusions
                # to be polite. Skip data-incomplete excludes.
                if v not in pruned and v.canonical is None:
                    continue
                if v not in pruned and (v.excluded or "") not in ("BAD_SUCCESS (reward-hacked)",):
                    continue
                dest_dir_name = v.row.id
                reason = "over-cap" if v in pruned else (v.excluded or "excluded").lower().replace(" ", "-")
                dest_prefix = f"trash/{task_name}/{run_ts}/{reason}/{dest_dir_name}/"
                print(f"  -> archiving (trash) {dest_prefix}")
                await upload_trial(src_s3, dest_s3, v, meta, dest_prefix)

            # Write README under the trash run prefix (if anything was archived)
            archived = pruned + [e for e in excluded if (e.canonical is not None and (e in pruned or (e.excluded or "") == "BAD_SUCCESS (reward-hacked)"))]
            if archived:
                trash_root = f"trash/{task_name}/{run_ts}/"
                readme_lines = [
                    f"# Trash archive for {task_name}",
                    f"Run: {run_ts}",
                    f"Source experiment: {EXPERIMENT_ID}",
                    "",
                    "## Pruned (over the per-(agent,model) cap of 5)",
                ]
                for v in pruned:
                    readme_lines.append(
                        f"- {v.row.id} ({v.row.agent}/{v.row.model}, reward={v.row.reward}, bytes={v.total_bytes})"
                    )
                bad = [e for e in excluded if (e.excluded or '') == "BAD_SUCCESS (reward-hacked)"]
                if bad:
                    readme_lines.extend(["", "## Excluded (reward-hacked / BAD_SUCCESS)"])
                    for v in bad:
                        readme_lines.append(
                            f"- {v.row.id} ({v.row.agent}/{v.row.model}, reward={v.row.reward})"
                        )
                await dest_s3.put_object(
                    Bucket=DEST_BUCKET,
                    Key=f"{trash_root}_README.md",
                    Body="\n".join(readme_lines).encode("utf-8"),
                    ContentType="text/markdown",
                )

            # Write _manifest.json
            manifest = {
                "task": f"{task_name}-{task_hash}",
                "task_name": task_name,
                "task_hash": task_hash,
                "task_version_id": meta["task_version_id"],
                "task_versions": [meta["task_version_id"]],
                "experiment_id": EXPERIMENT_ID,
                "n_trials": len(trials_section),
                "note": (
                    f"Generated from Oddish experiment {EXPERIMENT_ID} on {run_ts}. "
                    "Only rewritten-task trials (latest task_version) with numeric "
                    "verifier reward (including 0.0) kept; BAD_SUCCESS classifications "
                    "excluded as reward-hacked; <=5 trials per (agent,model) ranked by "
                    "total artifact bytes."
                ),
                "trials": trials_section,
            }
            manifest_key = f"{task_name}/_manifest.json"
            await dest_s3.put_object(
                Bucket=DEST_BUCKET,
                Key=manifest_key,
                Body=json.dumps(manifest, indent=2, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            print(f"  wrote manifest s3://{DEST_BUCKET}/{manifest_key}")

    # Persist summary locally
    out = Path(f"/tmp/ralphbench_copy_summary_{run_ts}.json")
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nSummary written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
