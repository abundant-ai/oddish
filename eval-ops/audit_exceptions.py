#!/usr/bin/env python3
"""Tally why trials actually failed, using Harbor's own classification.

The authoritative answer is result.harbor_exception.exception_type -- Harbor
names the failure itself. The error_message string is a red herring: it always
opens with "Command failed (exit 1): opencode ..." regardless of cause, and its
middle is elided server-side, so grepping it tells you very little.

Also counts trials that carry a real verifier result despite the exception,
since those solved the task and are being discarded by the validity rule.
"""
import json, subprocess, sys, collections

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
EXP = "826d7d88"
PENDING = ("pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying")


def api(args, tries=5):
    for _ in range(tries):
        try:
            r = subprocess.run([ODDISH] + args, capture_output=True, text=True,
                               timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None


d = api(["status", EXP, "--json"])
if d is None:
    sys.exit("experiment fetch FAILED")
tasks = {t["id"]: t["name"] for t in d.get("tasks") or []}

kinds = collections.Counter()
by_task = collections.defaultdict(collections.Counter)
scored_anyway = []      # exception raised, but the verifier still produced a reward
missing = []

for tid, tname in tasks.items():
    td = api(["status", tid, "--json"])
    if td is None:
        missing.append(tname)
        continue
    for tr in td.get("trials") or []:
        if tr.get("experiment_id") != EXP or tr.get("superseded_by_trial_id"):
            continue
        if (tr.get("status") or "").lower() in PENDING:
            continue
        res = tr.get("result") or {}
        exc = (res.get("harbor_exception") or {}).get("exception_type")
        if not exc:
            kinds["(no exception -- clean run)"] += 1
            continue
        kinds[exc] += 1
        by_task[tname][exc] += 1
        rew = tr.get("reward")
        main = (res.get("main") or {})
        if rew is not None or main:
            scored_anyway.append((tname, tr.get("id"), exc, rew,
                                  main.get("passed"), main.get("total"),
                                  (res.get("holdout") or {}).get("pass_rate")))

if missing:
    print(f"WARNING: {len(missing)} task(s) unreadable: {missing}")

print("=== why trials ended, by Harbor's own exception_type ===")
for k, v in kinds.most_common():
    print(f"  {v:>4}  {k}")

print("\n=== exceptions by task ===")
for tname in sorted(by_task):
    row = ", ".join(f"{k}={v}" for k, v in by_task[tname].most_common())
    print(f"  {tname:<32} {row}")

print(f"\n=== trials that raised an exception but STILL have a verifier result "
      f"({len(scored_anyway)}) ===")
for tname, tid, exc, rew, passed, total, hold in sorted(scored_anyway,
                                                        key=lambda r: -(r[3] or 0)):
    print(f"  {tid:<40} rew={rew} main={passed}/{total} holdout_rate={hold} {exc}")
