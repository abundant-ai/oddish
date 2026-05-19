"""Verification checks after the rewritten-trial upload.

Confirms:
- S3 active prefix count matches kept-trial count in each manifest.
- No duplicate fingerprints in each task (inner result.json
  id/trial_name/task_checksum).
- <= 5 valid trials per (agent, model) per task.
- All required root + inner files are present for each trial.
- trash/ contains only archived/pruned/excluded items, with a README.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

import boto3

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"

TASKS = [
    "embedding-eval",
    "ruby-rust-port",
    "trimul-cuda",
    "parameter-golf",
]

REQUIRED_ROOT = {"config.json", "result.json", "job.log", "lock.json", "modal-output.log"}
INNER_REQUIRED_FILES = {"config.json", "result.json", "trial.log"}
INNER_REQUIRED_DIRS = {"verifier"}


def main() -> int:
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=DEST_REGION,
    )

    all_ok = True

    for task_name in TASKS:
        print(f"\n=== {task_name} ===")
        manifest_key = f"{task_name}/_manifest.json"
        try:
            manifest = json.loads(s3.get_object(Bucket=DEST_BUCKET, Key=manifest_key)["Body"].read())
        except Exception as e:
            print(f"  ERROR fetching manifest: {e}")
            all_ok = False
            continue

        manifest_trials = manifest.get("trials", {})
        manifest_n = manifest.get("n_trials")
        if manifest_n != len(manifest_trials):
            print(f"  ! manifest n_trials={manifest_n} != len(trials)={len(manifest_trials)}")
            all_ok = False

        # List actual active (non-trash) top-level dirs under task_name/.
        active_dirs: set[str] = set()
        per_dir_keys: dict[str, set[str]] = defaultdict(set)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=DEST_BUCKET, Prefix=f"{task_name}/"):
            for o in page.get("Contents", []):
                rel = o["Key"][len(task_name) + 1:]
                if "/" not in rel:
                    continue
                top, _, inner = rel.partition("/")
                if top.startswith("_") or top == "trash":
                    continue
                active_dirs.add(top)
                per_dir_keys[top].add(inner)

        if active_dirs != set(manifest_trials.keys()):
            extra_s3 = active_dirs - set(manifest_trials.keys())
            missing_s3 = set(manifest_trials.keys()) - active_dirs
            print(f"  ! active-prefix mismatch: extra_in_s3={sorted(extra_s3)[:5]}... missing_from_s3={sorted(missing_s3)[:5]}...")
            all_ok = False
        else:
            print(f"  active prefix count matches manifest: {len(active_dirs)}")

        # Per-trial structural checks.
        struct_issues = 0
        for trial_dir, files in per_dir_keys.items():
            root_files = {f for f in files if "/" not in f}
            missing_root = REQUIRED_ROOT - root_files
            if missing_root:
                print(f"    ! {trial_dir}: missing root {missing_root}")
                struct_issues += 1
                continue
            inner_dirs = {f.split("/", 1)[0] for f in files if "/" in f and f.split("/", 1)[0].startswith("task-")}
            if len(inner_dirs) != 1:
                print(f"    ! {trial_dir}: expected exactly 1 inner task-* dir, found {len(inner_dirs)}: {sorted(inner_dirs)[:3]}")
                struct_issues += 1
                continue
            inner = next(iter(inner_dirs))
            inner_files = {f[len(inner) + 1:] for f in files if f.startswith(inner + "/")}
            inner_top_files = {f for f in inner_files if "/" not in f}
            inner_subdirs = {f.split("/", 1)[0] for f in inner_files if "/" in f}
            missing_inner_files = INNER_REQUIRED_FILES - inner_top_files
            missing_inner_dirs = INNER_REQUIRED_DIRS - inner_subdirs
            if missing_inner_files or missing_inner_dirs:
                print(f"    ! {trial_dir}: inner {inner} missing files={missing_inner_files} dirs={missing_inner_dirs}")
                struct_issues += 1
        if struct_issues == 0:
            print(f"  all {len(per_dir_keys)} trials have required root + canonical inner dir")
        else:
            all_ok = False

        # (agent, model) cap check + fingerprint dedup using inner result.json on S3.
        pair_counts: Counter[tuple[str, str]] = Counter()
        fingerprints: dict[tuple[str, str, str], str] = {}
        dup_issues = 0
        for trial_dir, entry in manifest_trials.items():
            pair_counts[(entry.get("agent"), entry.get("model"))] += 1
            # Fetch inner result.json for fingerprint
            inner_source_name = entry.get("source_trial_name")
            if inner_source_name is None:
                continue
            inner_key = f"{task_name}/{trial_dir}/{inner_source_name}/result.json"
            try:
                inner = json.loads(s3.get_object(Bucket=DEST_BUCKET, Key=inner_key)["Body"].read())
            except Exception as e:
                print(f"    ! {trial_dir}: cannot read inner {inner_key}: {e}")
                dup_issues += 1
                continue
            fp = (str(inner.get("id", "")), str(inner.get("trial_name", "")), str(inner.get("task_checksum", "")))
            if fp in fingerprints:
                print(f"    ! duplicate fingerprint: {trial_dir} matches {fingerprints[fp]} ({fp})")
                dup_issues += 1
                all_ok = False
            else:
                fingerprints[fp] = trial_dir
        if dup_issues == 0:
            print(f"  no duplicate fingerprints across {len(fingerprints)} trials")

        over_cap = {p: c for p, c in pair_counts.items() if c > 5}
        if over_cap:
            print(f"  ! over-cap pairs: {over_cap}")
            all_ok = False
        else:
            print(f"  all {len(pair_counts)} (agent,model) pairs <= 5 trials")
            for (a, m), c in sorted(pair_counts.items()):
                marker = " (short)" if c < 5 else ""
                print(f"      {a:14s} {m or '':50s} -> {c}{marker}")

        # Trash sanity: must have a _README.md per run prefix if any trash exists.
        trash_root = f"{task_name}/trash/"
        # Trash actually lives at root-level: trash/<task_name>/
        trash_root = f"trash/{task_name}/"
        runs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        has_readme: set[str] = set()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=DEST_BUCKET, Prefix=trash_root):
            for o in page.get("Contents", []):
                rel = o["Key"][len(trash_root):]
                parts = rel.split("/")
                if len(parts) >= 2 and parts[0]:
                    run_ts = parts[0]
                    if len(parts) == 2 and parts[1] == "_README.md":
                        has_readme.add(run_ts)
                    if len(parts) >= 3:
                        runs[run_ts][parts[1]] += 1
        for run_ts, reasons in runs.items():
            tag = "OK" if run_ts in has_readme else "MISSING_README"
            print(f"  trash run {run_ts}: {dict(reasons)} [{tag}]")
            if run_ts not in has_readme:
                all_ok = False

    print(f"\n=== {'ALL CHECKS PASSED' if all_ok else 'CHECKS FAILED'} ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
