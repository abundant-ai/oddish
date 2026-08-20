#!/usr/bin/env python3
"""Delete infra-failed trials from an experiment (runbook 4b: delete and re-run).

An infra failure is a harness failure, not an attempt by the model. The runbook
scores only trials that reached a real terminal state, so these are dead weight
-- and worse than dead weight, because the server counts them when resolving
--n-trials N, so every burnt trial permanently occupies a slot against the
target until it is removed.

Safety rules, in order of how badly each one would hurt to get wrong:

  * Never touch anything still in flight. A pending trial carries an
    error_message while it is being retried; deleting it destroys work that was
    about to succeed on its own.
  * Never touch a VALID trial. Valid means a clean terminal state OR an honest
    agent/verifier timeout -- the runbook counts a timeout as a real attempt.
  * Refuse the whole pass if any task could not be read. A failed fetch is
    indistinguishable from an empty task, and acting on that deletes nothing
    but reports success, hiding the failure.

Classification is by error_message, never by job status: infra failures
routinely report status=success with reward=0 (runbook 3.5).
"""
import argparse, json, subprocess, sys, datetime

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
PENDING = ("pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying")
OK_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")


def api(args, tries=6):
    for _ in range(tries):
        try:
            r = subprocess.run([ODDISH] + args, capture_output=True, text=True,
                               timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("exp")
    p.add_argument("--batch", type=int, default=25,
                   help="trials per delete call")
    p.add_argument("--max", type=int, default=0,
                   help="stop after deleting this many (0 = no limit)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    d = api(["status", a.exp, "--json"])
    if d is None:
        sys.exit(f"{stamp()} experiment fetch FAILED -- refusing to delete blind")
    tasks = {t["id"]: t["name"] for t in d.get("tasks") or []}

    doomed, missing, kept = [], [], {"valid": 0, "pending": 0}
    for tid, tname in tasks.items():
        td = api(["status", tid, "--json"])
        if td is None:
            missing.append(tname)
            continue
        for tr in td.get("trials") or []:
            if tr.get("experiment_id") != a.exp or tr.get("superseded_by_trial_id"):
                continue
            st = (tr.get("status") or "").lower()
            err = (tr.get("error_message") or "").lower()
            if st in PENDING:
                kept["pending"] += 1
                continue
            if not err or any(k in err for k in OK_ERR):
                kept["valid"] += 1
                continue
            doomed.append((tname, tr.get("id")))

    if missing:
        sys.exit(f"{stamp()} {len(missing)} task(s) unreadable "
                 f"({', '.join(missing[:4])}) -- refusing to prune")

    if a.max:
        doomed = doomed[:a.max]
    per_task = {}
    for tname, _ in doomed:
        per_task[tname] = per_task.get(tname, 0) + 1
    print(f"{stamp()} infra={len(doomed)} to delete | keeping "
          f"valid={kept['valid']} pending={kept['pending']}")
    for tname, n in sorted(per_task.items(), key=lambda kv: -kv[1]):
        print(f"  {tname:<34} {n}")
    if not doomed:
        return 0

    ids = [tid for _, tid in doomed]
    if a.dry_run:
        print(f"{stamp()} DRY RUN -- nothing deleted")
        return 0

    deleted = 0
    for i in range(0, len(ids), a.batch):
        chunk = ids[i:i + a.batch]
        cmd = [ODDISH, "delete", "--json"]
        for tid in chunk:
            cmd += ["--trial", tid]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            ok = r.returncode == 0
        except Exception:
            ok = False
        deleted += len(chunk) if ok else 0
        print(f"  batch {i // a.batch + 1}: {len(chunk)} trials "
              f"{'deleted' if ok else 'FAILED'}")
    print(f"{stamp()} deleted={deleted}/{len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
