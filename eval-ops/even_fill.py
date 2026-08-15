#!/usr/bin/env python3
"""One even-fill pass: top every task up toward --target while respecting a
GLOBAL in-flight --cap, spreading new work across tasks instead of draining one.

Why round-robin: a plain `--n-trials 10` sweep hands the whole backlog to the
scheduler at once, which then runs whatever it likes -- in practice one task at
a time. Filling fewest-held-first means every task advances together, so a
partial run is still a usable cross-section of the dataset.

Sweeps are TARGET-based (runbook 3.1): `--n-trials N` creates N - existing for
that (task, agent, model, experiment). So submitting held+1 creates exactly one
trial, and re-running this pass is never double-submission.

held = valid + pending. Failed/deleted trials do not count toward the target,
which is why a cell can sit below target with plenty of trials in its history.

--ak flags are load-bearing: opencode's `variant` is a CliFlag with NO default,
so omitting `--ak variant=high` silently runs a different configuration than the
original arm. Pass every knob explicitly, every time.
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
    p.add_argument("agent")
    p.add_argument("model")
    p.add_argument("--target", type=int, required=True)
    p.add_argument("--cap", type=int, required=True, help="global in-flight cap")
    p.add_argument("--batch", type=int, default=0,
                   help="max NEW trials this pass (0 = fill all headroom)")
    p.add_argument("--ak", action="append", default=[], help="agent kwarg, repeatable")
    p.add_argument("--dataset", default="/home/user/terra-run/ds-none")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    d = api(["status", a.exp, "--json"])
    if d is None:
        sys.exit(f"{stamp()} experiment fetch FAILED -- refusing to submit blind")
    tasks = {t["id"]: t["name"] for t in d.get("tasks") or []}

    cells, missing = {}, []
    for tid, tname in tasks.items():
        td = api(["status", tid, "--json"])
        if td is None:
            missing.append(tname)
            continue
        held = pend = valid = 0
        for tr in td.get("trials") or []:
            if tr.get("experiment_id") != a.exp or tr.get("superseded_by_trial_id"):
                continue
            st = (tr.get("status") or "").lower()
            err = (tr.get("error_message") or "").lower()
            if st in PENDING:
                pend += 1
                held += 1
            elif not err or any(k in err for k in OK_ERR):
                valid += 1
                held += 1
        cells[tname] = {"held": held, "pend": pend, "valid": valid}

    # A failed read looks exactly like an empty cell, and acting on that
    # over-submits. Refuse the pass instead of guessing.
    if missing:
        sys.exit(f"{stamp()} {len(missing)} task(s) unreadable "
                 f"({', '.join(missing[:4])}) -- skipping this pass")

    in_flight = sum(c["pend"] for c in cells.values())
    headroom = a.cap - in_flight
    # Batch size is the real throttle. Gemini's 20M input-tokens/minute quota is
    # shared across every concurrent trial, so a big burst makes ALL of them 429
    # -- and a 429 makes opencode retry with the full context re-sent, which
    # inflates token use further. Small batches keep the fleet under the ceiling.
    if a.batch:
        headroom = min(headroom, a.batch)
    short = [(c["held"], n) for n, c in cells.items() if c["held"] < a.target]
    print(f"{stamp()} in_flight={in_flight}/{a.cap} headroom={max(headroom,0)} "
          f"short={len(short)} valid={sum(c['valid'] for c in cells.values())}"
          f"/{a.target * len(cells)}")

    if not short:
        print("COMPLETE -- every task at target")
        return 0
    if headroom <= 0:
        print("at cap -- nothing submitted")
        return 1

    created = 0
    for held, name in sorted(short):          # fewest-held first == even progress
        if headroom <= 0:
            break
        n = held + 1                          # target-based: creates exactly one
        cmd = [ODDISH, "run", "-p", a.dataset, "-t", name, "-a", a.agent,
               "-m", a.model, "--n-trials", str(n), "-e", "modal",
               "-E", a.exp, "--force", "--background"]
        for kv in a.ak:
            cmd += ["--ak", kv]
        if a.dry_run:
            print(f"  DRY {name:<32} held={held} -> --n-trials {n}")
            headroom -= 1
            continue
        ok = False
        for attempt in (1, 2, 3):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                out = r.stdout + r.stderr
            except Exception as e:
                out = str(e)
            if "Task submitted!" in out:
                ok = True
                break
            if "new version" in out.lower() or "budget" in out.lower():
                print(f"  {name:<32} ABORT: {out.strip().splitlines()[-1][:80]}")
                break
            subprocess.run(["sleep", str(10 * attempt)])
        print(f"  {name:<32} held={held} -> n={n} {'ok' if ok else 'FAILED'}")
        if ok:
            created += 1
            headroom -= 1
    print(f"{stamp()} created={created}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
