#!/usr/bin/env python3
"""Fleet poller across the three gpt-5.6-terra reasoning-effort experiments.

Task IDs are content-derived and SHARED across experiments, so each task is
fetched once and its trials are bucketed by experiment_id.

Classifies by error_message, never by job status (runbook 3.5).
Writes /home/user/terra-run/state.json for the babysit loop.
"""
import json, subprocess, datetime, collections, sys, os

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
STATE = "/home/user/terra-run/state.json"

EXPS = {"17b6f7d9": "LOW", "c071229f": "MEDIUM", "a706e700": "HIGH"}

CUA_TASKS = {
    "excel-clone": "excel-clone-685bff89",
    "mastodon-clone": "mastodon-clone-e9964b7a",
    "s3-clone": "s3-clone-23996371",
    "slack-clone": "slack-clone-b0a98beb",
}
NONCUA_TASKS = {
    "biofabric-rust-rewrite": "biofabric-rust-rewrite-897c0fc8",
    "embedding-eval": "embedding-eval-be1dc228",
    "find-network-alignments": "find-network-alignments-b2ecebb7",
    "jax-pytorch-rewrite": "jax-pytorch-rewrite-93c38683",
    "kubernetes-rust-rewrite": "kubernetes-rust-rewrite-bae0f616",
    "nextjs-vite-rewrite": "nextjs-vite-rewrite-b0d028b0",
    "parameter-golf": "parameter-golf-61e11605",
    "post-train-ifeval-gpu": "post-train-ifeval-gpu-1de1166a",
    "ruby-rust-port": "ruby-rust-port-383776ff",
    "rust-c-compiler": "rust-c-compiler-57913cbe",
    "rust-java-lsp": "rust-java-lsp-0a9c67d6",
    "stripe-clone": "stripe-clone-66061f4d",
    "trimul-cuda": "trimul-cuda-fc34aaba",
    "vliw-kernel-optimization": "vliw-kernel-optimization-e4d25121",
    "wasm-simd": "wasm-simd-40e18127",
    "zstd-decoder": "zstd-decoder-72bbfded",
}
ALL_TASKS = {**NONCUA_TASKS, **CUA_TASKS}

# VALID = reached a real terminal state: a clean run, or an honest agent/verifier
# timeout. This fleet spells timeouts in prose ("Agent execution timed out after
# N seconds"), not as the exception-class name -- match both or a real timeout
# gets misfiled as infra and deleted.
OK_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")
# "worker heartbeat stalled" is recoverable while status is "retrying" (Oddish
# re-runs it), but once it lands in a terminal status the trial is genuinely
# lost and must be deleted + re-run like any other infra failure.
INFRA_ERR = ("command failed", "exceptiongroup", "no reward file",
             "ratelimit", "rate limit", "429", "internal server error",
             "worker heartbeat stalled")
# "retrying" is a PENDING state, not a failure: Oddish is re-running the trial
# itself. It carries an error_message ("Worker heartbeat stalled for over 15
# minutes"), so leaving it out of this list would classify it OTHER and
# --delete would destroy trials that were about to succeed on their own.
PENDING = ("pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying")


def classify(tr):
    st = (tr.get("status") or "").lower()
    err = (tr.get("error_message") or "").lower()
    if st in PENDING:
        return "PENDING"
    if st == "skipped":
        return "SKIPPED"
    if not err:
        return "VALID"
    if any(k in err for k in OK_ERR):
        return "VALID"
    if any(k in err for k in INFRA_ERR):
        return "INFRA"
    return "OTHER"


def fetch(task_id, tries=3):
    for _ in range(tries):
        try:
            r = subprocess.run([ODDISH, "status", task_id, "--json"],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None


def main():
    only = sys.argv[1:] or list(ALL_TASKS)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    state = {"polled_at": now, "cells": {}, "unknown": [], "trials": {}}

    for task in only:
        d = fetch(ALL_TASKS[task])
        if d is None:
            state["unknown"].append(task)
            print(f"UNKNOWN (fetch failed) {task}", file=sys.stderr)
            continue
        for tr in d.get("trials") or []:
            arm = EXPS.get(tr.get("experiment_id"))
            if arm is None or tr.get("superseded_by_trial_id") is not None:
                continue
            agent = tr.get("agent") or "?"
            kind = "terra" if agent not in ("nop", "oracle") else agent
            k = classify(tr)
            c = state["cells"].setdefault(
                f"{arm}|{task}|{kind}",
                {"VALID": 0, "PENDING": 0, "INFRA": 0, "OTHER": 0,
                 "SKIPPED": 0, "pass": 0})
            c[k] += 1
            if k == "VALID" and (tr.get("reward") or 0) and float(tr["reward"]) >= 1.0:
                c["pass"] += 1
            state["trials"].setdefault(k, []).append({
                "arm": arm, "task": task, "agent": agent, "kind": kind,
                "id": tr.get("id"), "status": tr.get("status"),
                "reward": tr.get("reward"), "steps": tr.get("total_steps"),
                "cost": tr.get("cost_usd"),
                "err": (tr.get("error_message") or "")[:160]})

    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)

    tot = collections.Counter()
    cua_live = 0
    for key, c in state["cells"].items():
        _, task, kind = key.split("|")
        for k in ("VALID", "PENDING", "INFRA", "OTHER", "SKIPPED"):
            tot[k] += c[k]
        if task in CUA_TASKS and kind != "nop":
            cua_live += c["PENDING"]

    print(f"{now}  valid={tot['VALID']} pending={tot['PENDING']} "
          f"infra={tot['INFRA']} other={tot['OTHER']} skipped={tot['SKIPPED']}")
    print(f"CUA judge-consuming in-flight (oracle+terra, excl nop) = {cua_live} / cap 10")
    if state["unknown"]:
        print("UNKNOWN tasks:", ", ".join(state["unknown"]))


if __name__ == "__main__":
    main()
