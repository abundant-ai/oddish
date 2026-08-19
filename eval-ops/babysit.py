#!/usr/bin/env python3
"""Babysit cycle for the three gpt-5.6-terra reasoning-effort experiments.

One cycle = poll -> classify -> report -> (delete infra) -> (re-top-up).

Targets live in config.json so they survive a container restart (see PLAN.md).
Non-CUA cells are topped up directly; CUA cells are left to cua_fill.py, which
respects the runbook 3.6 concurrency cap.

Degenerate trials are REPORTED but never auto-deleted: in the LOW arm short
trajectories are the treatment effect being measured, and systematically
pruning them biases pass@k (runbook 4b).

Usage:  babysit.py [--delete] [--topup]
"""
import json, subprocess, os, sys, statistics, collections

HERE = "/home/user/terra-run"
ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
ARMS = ["LOW", "MEDIUM", "HIGH"]
EXP_OF = {"LOW": "17b6f7d9", "MEDIUM": "c071229f", "HIGH": "a706e700"}
CUA = {"excel-clone", "mastodon-clone", "s3-clone", "slack-clone"}

CFG = json.load(open(f"{HERE}/config.json"))
N_NONCUA, N_CUA, CUA_CAP = CFG["noncua_target"], CFG["cua_target"], CFG["cua_cap"]


def load_env():
    env = dict(os.environ)
    for line in open("/home/user/oddish/.env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    do_delete, do_topup = "--delete" in sys.argv, "--topup" in sys.argv
    env = load_env()
    r = subprocess.run(["python3", f"{HERE}/poll_all.py"], capture_output=True,
                       text=True, env=env, cwd=HERE, timeout=2400)
    print(r.stdout.strip() or r.stderr.strip()[:500])
    st = json.load(open(f"{HERE}/state.json"))

    cells, now = st["cells"], st["polled_at"]
    tasks = sorted({k.split("|")[1] for k in cells})
    noncua = [t for t in tasks if t not in CUA]
    cua = [t for t in tasks if t in CUA]
    cua_live = sum(c["PENDING"] for k, c in cells.items()
                   if k.split("|")[1] in CUA and k.split("|")[2] != "nop")

    out = [f"# Babysit report — {now}", "",
           f"Phase **{CFG['phase']}** — targets: non-CUA **{N_NONCUA}**/cell, "
           f"CUA **{N_CUA}**/cell.", "",
           f"CUA in-flight (judge-consuming): **{cua_live}** / cap {CUA_CAP} "
           f"— {'OK' if cua_live <= CUA_CAP else 'OVER CAP'}", ""]

    grand = collections.Counter()
    for label, tlist, target in (("non-CUA", noncua, N_NONCUA), ("CUA", cua, N_CUA)):
        for arm in ARMS:
            rows = [(t, cells.get(f"{arm}|{t}|terra",
                                  {"VALID": 0, "PENDING": 0, "INFRA": 0,
                                   "OTHER": 0, "SKIPPED": 0, "pass": 0}))
                    for t in tlist]
            v = sum(c["VALID"] for _, c in rows)
            p = sum(c["PENDING"] for _, c in rows)
            i = sum(c["INFRA"] + c["OTHER"] + c["SKIPPED"] for _, c in rows)
            ps = sum(c["pass"] for _, c in rows)
            grand.update({"valid": v, "pending": p, "infra": i, "pass": ps,
                          f"{label}_valid": v, f"{label}_pass": ps})
            done = sum(1 for _, c in rows if c["VALID"] >= target)
            out += [f"## {label} — {arm} ({EXP_OF[arm]})", "",
                    f"valid={v}/{len(tlist)*target} pending={p} infra={i} "
                    f"pass={ps} — {done}/{len(tlist)} tasks at target", "",
                    "| task | valid | pending | infra | pass |", "|---|---|---|---|---|"]
            for t, c in rows:
                flag = "" if c["VALID"] >= target else " ⏳"
                out.append(f"| `{t}`{flag} | {c['VALID']} | {c['PENDING']} | "
                           f"{c['INFRA']+c['OTHER']+c['SKIPPED']} | {c['pass']} |")
            out.append("")

    out += [f"**Fleet:** valid={grand['valid']} pending={grand['pending']} "
            f"infra={grand['infra']} pass={grand['pass']}", ""]

    bad = (st["trials"].get("INFRA", []) + st["trials"].get("OTHER", [])
           + st["trials"].get("SKIPPED", []))
    if bad:
        out += ["## Infra / skipped — delete + rerun", ""]
        for tr in bad:
            out.append(f"- `{tr['id']}` {tr['arm']}/{tr['task']}/{tr['agent']} "
                       f"[{tr['status']}] {tr['err']}")
        out.append("")

    short = [tr for tr in st["trials"].get("VALID", [])
             if tr["kind"] == "terra" and (tr["steps"] or 0) < 15]
    if short:
        out += ["## Degenerate candidates (<15 steps) — reported, not deleted", ""]
        for tr in sorted(short, key=lambda x: x["steps"] or 0):
            out.append(f"- `{tr['id']}` {tr['arm']}/{tr['task']} "
                       f"steps={tr['steps']} reward={tr['reward']}")
        out.append("")

    allsteps = [tr["steps"] for tr in st["trials"].get("VALID", [])
                if tr["kind"] == "terra" and tr["steps"]]
    if allsteps:
        out.append(f"Step distribution (valid terra, n={len(allsteps)}): "
                   f"min={min(allsteps)} p50={int(statistics.median(allsteps))} "
                   f"max={max(allsteps)}")
    cost = sum(tr["cost"] or 0 for recs in st["trials"].values() for tr in recs
               if tr["kind"] == "terra")
    out.append(f"Terra spend so far: ${cost:,.2f}")

    report = "\n".join(out) + "\n"
    open(f"{HERE}/babysit-latest.md", "w").write(report)
    print(report)
    with open(f"{HERE}/hb-fleet.txt", "w") as f:
        f.write(f"{now} | FLEET phase={CFG['phase']} | valid={grand['valid']} "
                f"pending={grand['pending']} infra={grand['infra']} | "
                f"cua_inflight={cua_live}/{CUA_CAP} | ${cost:,.0f}\n")

    if do_delete and bad:
        denv = dict(env)
        denv["ODDISH_API_KEY"] = denv["ODDISH_ADMIN_API_KEY"]
        ids = [tr["id"] for tr in bad]
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            args = [ODDISH, "delete"] + sum([["-t", x] for x in chunk], []) + ["--json"]
            rr = subprocess.run(args, capture_output=True, text=True, env=denv,
                                timeout=600)
            print(f"deleted {len(chunk)}: rc={rr.returncode} {rr.stdout[:200]}")

    if do_topup:
        # Only non-CUA here; CUA refills go through cua_fill.py's throttle.
        shortc = []
        for arm in ARMS:
            for t in noncua:
                c = cells.get(f"{arm}|{t}|terra")
                held = sum(c[k] for k in ("VALID", "PENDING", "INFRA", "OTHER",
                                          "SKIPPED")) if c else 0
                if held < N_NONCUA:
                    shortc.append((arm, t, N_NONCUA - held))
        if not shortc:
            print(f"topup: nothing short — all non-CUA cells hold {N_NONCUA}")
        for arm, t, gap in shortc:
            print(f"topup: {arm}/{t} short by {gap}")
            for attempt in range(1, 6):
                rr = subprocess.run(
                    [ODDISH, "run", "-p", f"{HERE}/ds-{arm.lower()}", "-t", t,
                     "-a", "codex", "-m", "openai/gpt-5.6-terra",
                     "--n-trials", str(N_NONCUA), "-e", "modal",
                     "-E", EXP_OF[arm], "--override-memory-mb", "65536",
                     "--no-baseline-gate",
                     "--ak", f"reasoning_effort={arm.lower()}",
                     "--ae", f"ODDISH_EVAL_NONCE={arm}-{t}-{now}-{attempt}",
                     "--background", "--force"],
                    capture_output=True, text=True, env=env, timeout=400)
                if "new version" in rr.stdout.lower():
                    print(f"  ABORT {arm}/{t}: submit wants a NEW TASK VERSION")
                    break
                if "Task submitted!" in rr.stdout:
                    line = [l.strip() for l in rr.stdout.splitlines() if "Trials:" in l]
                    print(f"  ok {line[0] if line else ''}")
                    break
                print(f"  attempt {attempt} failed")
                if attempt < 5:
                    subprocess.run(["sleep", str(5 * attempt * attempt)])


if __name__ == "__main__":
    main()
