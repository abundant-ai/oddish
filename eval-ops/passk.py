#!/usr/bin/env python3
"""pass@1 / pass@3 / pass@8, following the oddish runbook's rules.

VALIDITY (docs/swe-marathon-eval-runbook.md, "Target outcome" + gotcha 3.5):

    valid = the trial ran to a real terminal state -- a clean success *or* an
    AgentTimeoutError. Any other infra failure (non-zero agent exit like
    137/143/1, verifier infra failure, a Harbor ExceptionGroup) is NOT valid;
    delete and rerun those.

    Classify by error_message, never job status: infra failures frequently
    report status=success, reward=0 with a populated error_message.

So infra trials are excluded from the denominator entirely -- they are failures
of the harness, not attempts by the model, and the runbook's remedy is to delete
and rerun them rather than score them. This differs from the dashboard, which
counts every completed attempt; both are printed so the gap is visible.

ESTIMATOR (frontend/src/lib/pass-at-k.ts, the only definition in the repo):

    pass@k = 1 - C(n-c, k)/C(n, k), product form; n is PER TASK per agent;
    c counts reward == 1; the reported value is the unweighted MEAN of the
    per-task pass@k. nop/oracle baselines are excluded.

Raw trials are cached to passk-raw.json so the numbers can be recomputed under
different rules without refetching.
"""
import json, subprocess, sys, os, collections

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
RAW = "/home/user/terra-run/passk-raw.json"
KS = (1, 3, 8)
BASELINES = {"nop", "oracle"}
PENDING = {"pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying"}
# Prose form matters: this fleet writes "Agent execution timed out after N
# seconds", not the exception-class name. Match both or a genuine timeout --
# which the runbook counts as VALID -- gets thrown out as infra.
OK_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")


def api(args, tries=3):
    for _ in range(tries):
        try:
            r = subprocess.run([ODDISH] + args, capture_output=True, text=True,
                               timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None


def fetch(exps):
    raw = json.load(open(RAW)) if os.path.exists(RAW) else {}
    for exp in exps:
        if exp in raw:
            print(f"{exp}: cached ({len(raw[exp]['trials'])} trials)", file=sys.stderr)
            continue
        d = api(["status", exp, "--json"])
        if d is None:
            print(f"{exp}: FETCH FAILED", file=sys.stderr)
            continue
        tasks = {t["id"]: t["name"] for t in d.get("tasks") or []}
        name = next((t.get("experiment_name") for t in (d.get("tasks") or [])
                     if t.get("experiment_id") == exp), exp)
        trials = []
        for tid, tname in tasks.items():
            td = api(["status", tid, "--json"])
            if td is None:
                print(f"  {exp}/{tname}: FETCH FAILED", file=sys.stderr)
                continue
            for tr in td.get("trials") or []:
                if tr.get("experiment_id") != exp:
                    continue
                if tr.get("superseded_by_trial_id") is not None:
                    continue
                trials.append({
                    "task": tname, "agent": tr.get("agent") or "?",
                    "model": tr.get("model") or "?",
                    "status": (tr.get("status") or "").lower(),
                    "reward": tr.get("reward"),
                    "err": (tr.get("error_message") or "")})
        raw[exp] = {"name": name, "trials": trials}
        print(f"{exp} ({name}): {len(trials)} trials over {len(tasks)} tasks",
              file=sys.stderr)
        with open(RAW, "w") as f:
            json.dump(raw, f)
    return raw


def pass_at_k(n, c, k):
    """Verbatim port of calculatePassAtK (frontend/src/lib/pass-at-k.ts)."""
    if k > n:
        return 1.0 if c > 0 else 0.0
    if c == 0:
        return 0.0
    if c >= n:
        return 1.0
    if k <= 0:
        return 0.0
    if k > n - c:
        return 1.0
    p = 1.0
    for i in range(k):
        p *= (n - c - i) / (n - i)
    return 1.0 - p


def is_valid(tr):
    """Runbook validity: clean terminal state, or an honest agent/verifier
    timeout. Everything else with an error_message is infra."""
    err = tr["err"].lower()
    if not err:
        return True
    return any(k in err for k in OK_ERR)


def curve(raw, exps, rule):
    """rule='runbook' -> valid trials only; rule='dashboard' -> all completed."""
    rows = []
    for exp in exps:
        if exp not in raw:
            continue
        per = collections.defaultdict(lambda: collections.defaultdict(
            lambda: {"n": 0, "c": 0}))
        infra = collections.Counter()
        for tr in raw[exp]["trials"]:
            if tr["agent"] in BASELINES or tr["status"] in PENDING:
                continue
            key = f"{tr['agent']} / {tr['model']}"
            if rule == "runbook" and not is_valid(tr):
                infra[key] += 1
                continue
            cell = per[key][tr["task"]]
            cell["n"] += 1
            if tr["reward"] is not None and float(tr["reward"]) == 1.0:
                cell["c"] += 1
        for key, tasks in per.items():
            vals = [sum(pass_at_k(v["n"], v["c"], k) for v in tasks.values())
                    / len(tasks) for k in KS]
            rows.append({
                "exp": exp, "name": raw[exp]["name"], "key": key,
                "tasks": len(tasks), "trials": sum(v["n"] for v in tasks.values()),
                "passes": sum(v["c"] for v in tasks.values()),
                "infra_excluded": infra[key],
                **{f"pass@{k}": v for k, v in zip(KS, vals)}})
    return rows


def show(title, rows):
    print(f"\n{title}")
    print("-" * 104)
    print(f"{'experiment':<32}{'agent / model':<30}{'tasks':>6}{'trials':>7}"
          f"{'pass':>6}" + "".join(f"{'pass@'+str(k):>9}" for k in KS)
          + f"{'infra':>7}")
    print("-" * 104)
    for r in rows:
        print(f"{r['name'][:30]:<32}{r['key'][:28]:<30}{r['tasks']:>6}"
              f"{r['trials']:>7}{r['passes']:>6}"
              + "".join(f"{100*r['pass@'+str(k)]:>8.1f}%" for k in KS)
              + f"{r['infra_excluded']:>7}")


if __name__ == "__main__":
    exps = sys.argv[1:]
    raw = fetch(exps)
    rb = curve(raw, exps, "runbook")
    show("RUNBOOK RULE — valid trials only (infra deleted/rerun, so not scored)", rb)
    show("DASHBOARD RULE — every completed attempt counts", curve(raw, exps, "dashboard"))
    json.dump(rb, open("/home/user/terra-run/passk.json", "w"), indent=1)
