#!/usr/bin/env python3
"""Poll the opencode experiment: per-task counts + the newest completions.

Classifies by error_message (runbook 3.5). The question this answers is
narrow: are trials that FINISH coming back clean, or still dying on 429s?
"""
import json, subprocess, sys, collections, datetime

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
EXP = "826d7d88"
PENDING = ("pending","queued","running","blocked","preparing","submitted",
           "claimed","in_progress","initializing","retrying")
OK_ERR = ("agenttimeouterror","verifiertimeouterror","timed out after")

def api(a, tries=6):
    for i in range(tries):
        try:
            r = subprocess.run([ODDISH]+a, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None

d = api(["status", EXP, "--json"])
if d is None:
    sys.exit("experiment fetch FAILED")
tasks = {t["id"]: t["name"] for t in d.get("tasks") or []}

rows, cells, miss = [], {}, []
for tid, tname in tasks.items():
    td = api(["status", tid, "--json"])
    if td is None:
        miss.append(tname); continue
    c = {"valid":0,"pend":0,"infra":0,"pass":0}
    for tr in td.get("trials") or []:
        if tr.get("experiment_id") != EXP or tr.get("superseded_by_trial_id"): continue
        st = (tr.get("status") or "").lower(); err = (tr.get("error_message") or "")
        e = err.lower()
        if st in PENDING: c["pend"] += 1; k = "PEND"
        elif not e or any(x in e for x in OK_ERR):
            c["valid"] += 1; k = "VALID"
            if (tr.get("reward") or 0) and float(tr["reward"]) >= 1.0: c["pass"] += 1
        else:
            c["infra"] += 1; k = "INFRA"
        rows.append({"task":tname,"id":tr.get("id"),"k":k,"st":st,
                     "rew":tr.get("reward"),"in_tok":tr.get("input_tokens"),
                     "steps":tr.get("total_steps"),"cost":tr.get("cost_usd"),
                     "created":tr.get("created_at"),"fin":tr.get("finished_at") or tr.get("updated_at"),
                     "err":err[:110]})
    cells[tname] = c

tot = collections.Counter()
for t, c in sorted(cells.items()):
    for k in c: tot[k] += c[k]
print(f"{datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}  "
      f"valid={tot['valid']} pass={tot['pass']} pending={tot['pend']} infra={tot['infra']}"
      + (f"  MISSING={len(miss)}" if miss else ""))
for t, c in sorted(cells.items()):
    print(f"  {t:<34} valid={c['valid']:>2} pass={c['pass']:>2} pend={c['pend']:>2} infra={c['infra']:>3}")
fin = [r for r in rows if r["k"] != "PEND" and r["fin"]]
fin.sort(key=lambda r: r["fin"], reverse=True)
print("\nNEWEST 15 COMPLETIONS (most recent first)")
for r in fin[:15]:
    print(f"  {r['fin'][:19]}  {str(r['id'])[-28:]:<30} {r['k']:<5} rew={r['rew']} "
          f"in_tok={r['in_tok']} steps={r['steps']} {r['err']}")
json.dump({"cells":cells,"rows":rows}, open("/home/user/terra-run/oc_state.json","w"), indent=1)
