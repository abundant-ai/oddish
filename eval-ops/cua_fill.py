#!/usr/bin/env python3
"""Throttled filler for the 4 CUA-verifier tasks.

Runbook 3.6 caps concurrent CUA trials at ~10: the browser verifier runs on the
shared platform ANTHROPIC_API_KEY, and each CUA task.toml hard-fails (emits no
reward file, so the trial ERRORS instead of scoring) when the grader dies for an
infra reason. Over-parallelising does not just queue -- it destroys trials.
The cap is a verifier-safety limit, NOT a target: it does not relax in phase B.

Each pass: count judge-consuming trials in flight, and submit only enough +1
sweeps to refill up to the cap. Sweeps are target-based, so `--n-trials held+1`
adds exactly one trial to that cell.

`nop` does NOT count against the cap: its verifier short-circuits in ~1.6 s
because the agent never brings the app up.

Targets come from config.json so a phase flip needs no relaunch.

Usage: cua_fill.py [--target N] [--cap N] [--dry]
"""
import json, subprocess, sys, os, datetime

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
HERE = "/home/user/terra-run"
EXPS = {"17b6f7d9": "LOW", "c071229f": "MEDIUM", "a706e700": "HIGH"}
EXP_OF = {v: k for k, v in EXPS.items()}
CUA_TASKS = {
    "excel-clone": "excel-clone-685bff89",
    "mastodon-clone": "mastodon-clone-e9964b7a",
    "s3-clone": "s3-clone-23996371",
    "slack-clone": "slack-clone-b0a98beb",
}
# "retrying" is pending, not failed -- Oddish is re-running the trial itself.
# Omitting it understates in-flight (a retrying trial counts as neither valid
# nor pending) and so overstates headroom. Same fix as poll_all.py.
PENDING = ("pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying")
OK_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")


def arg(flag, default):
    return type(default)(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


def load_env():
    env = dict(os.environ)
    for line in open("/home/user/oddish/.env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def scan(env):
    cells, inflight, unknown = {}, 0, []
    for task, tid in CUA_TASKS.items():
        d = None
        for _ in range(3):
            r = subprocess.run([ODDISH, "status", tid, "--json"],
                               capture_output=True, text=True, env=env, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                break
        if d is None:
            unknown.append(task)
            continue
        for tr in d.get("trials") or []:
            arm = EXPS.get(tr.get("experiment_id"))
            if arm is None or tr.get("superseded_by_trial_id") is not None:
                continue
            agent = tr.get("agent") or "?"
            if agent in ("nop", "oracle"):
                if (tr.get("status") or "").lower() in PENDING and agent == "oracle":
                    inflight += 1
                continue
            c = cells.setdefault((arm, task), {"valid": 0, "pending": 0, "held": 0})
            c["held"] += 1
            st = (tr.get("status") or "").lower()
            err = (tr.get("error_message") or "").lower()
            if st in PENDING:
                c["pending"] += 1
                inflight += 1
            elif not err or any(k in err for k in OK_ERR):
                c["valid"] += 1
    return cells, inflight, unknown


def run(target, cap, dry):
    env = load_env()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    cells, inflight, unknown = scan(env)

    grid = []
    for arm in ("LOW", "MEDIUM", "HIGH"):
        for task in CUA_TASKS:
            grid.append((arm, task,
                         cells.get((arm, task), {"valid": 0, "pending": 0, "held": 0})))

    done = sum(1 for _, _, c in grid if c["held"] >= target)
    tv = sum(c["valid"] for _, _, c in grid)
    print(f"{now}  CUA fill: target={target}/cell cap={cap} in-flight={inflight} "
          f"valid={tv}/{12*target} cells_at_target={done}/12"
          + (f"  UNKNOWN={unknown}" if unknown else ""))
    for arm, task, c in grid:
        print(f"  {arm:6} {task:15} valid={c['valid']} pending={c['pending']} held={c['held']}")

    if unknown:
        print("ABORT: could not read every CUA task; refusing to submit blind "
              "(an unread task could already be at the cap).")
        return

    headroom = cap - inflight
    need = sorted([(c["held"], arm, task, c) for arm, task, c in grid
                   if c["held"] < target])
    if not need:
        print("all 12 cells hold their target — CUA fill complete")
        return
    if headroom <= 0:
        print(f"at cap ({inflight}/{cap}) — nothing submitted this pass")
        return

    sent = 0
    for held, arm, task, c in need:
        if headroom <= 0:
            break
        n = c["held"] + 1
        print(f"submit {arm}/{task}: held={c['held']} -> --n-trials {n}")
        if dry:
            headroom -= 1
            sent += 1
            continue
        for attempt in range(1, 6):
            r = subprocess.run(
                [ODDISH, "run", "-p", f"{HERE}/ds-{arm.lower()}", "-t", task,
                 "-a", "codex", "-m", "openai/gpt-5.6-terra",
                 "--n-trials", str(n), "-e", "modal", "-E", EXP_OF[arm],
                 "--override-memory-mb", "65536", "--no-baseline-gate",
                 "--ak", f"reasoning_effort={arm.lower()}",
                 "--ae", f"ODDISH_EVAL_NONCE={arm}-{task}-{now}-{attempt}",
                 "--background", "--force"],
                capture_output=True, text=True, env=env, timeout=400)
            if "new version" in r.stdout.lower():
                print(f"  ABORT {arm}/{task}: submit wants a NEW TASK VERSION "
                      f"— dataset copy has drifted from the uploaded task.")
                return
            if "Task submitted!" in r.stdout:
                line = [l.strip() for l in r.stdout.splitlines() if "Trials:" in l]
                print(f"  ok {line[0] if line else ''}")
                headroom -= 1
                sent += 1
                break
            print(f"  attempt {attempt} failed")
            if attempt < 5:
                subprocess.run(["sleep", str(5 * attempt * attempt)])
    print(f"submitted {sent} this pass; in-flight now ~{inflight + sent}/{cap}")


def main():
    cfg = json.load(open(f"{HERE}/config.json"))
    target = arg("--target", cfg["cua_target"])
    cap = arg("--cap", cfg["cua_cap"])
    dry = "--dry" in sys.argv

    # Single-writer lock. A pass routinely outlasts the babysit interval, and
    # two concurrent fillers would each read a stale in-flight count and submit
    # against the same headroom -- silently doubling us past the cap.
    lock = f"{HERE}/.cua_fill.lock"
    if not dry:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                pid = int(open(lock).read().strip())
                os.kill(pid, 0)
                print(f"another cua_fill (pid {pid}) is running — skipping this pass")
                return
            except (ValueError, ProcessLookupError, PermissionError):
                print("stale lock — taking over")
                os.unlink(lock)
                open(lock, "w").write(str(os.getpid()))
    try:
        run(target, cap, dry)
    finally:
        if not dry and os.path.exists(lock):
            try:
                if int(open(lock).read().strip()) == os.getpid():
                    os.unlink(lock)
            except ValueError:
                pass


if __name__ == "__main__":
    main()
