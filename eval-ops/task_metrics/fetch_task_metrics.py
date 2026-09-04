#!/usr/bin/env python3
"""Per-task trajectory metrics (steps, agent runtime, tokens) from the oddish API.

Input : tasks.tsv  (task, category, opus_table, learn_table, experiments)
Output: raw_trials.json (every trial fetched, keyed by task name)
        task_metrics.csv (one row per task)

Selection rules, per task:
  * exact task-name match via GET /tasks/browse?query=<name>; when several task
    ids share a name, keep the ones whose linked experiments intersect the
    sheet's experiment ids (fallback: all of them).
  * trials: kind == "agent", not a probe, not superseded, agent not nop/oracle.
    Restricted to the sheet's linked experiment ids when the task has trials
    there (fallback: every experiment), then to the latest task_version seen in
    that set (fallback: every version) -- this mirrors the sheet's
    "latest experiment version only" rule.
  * valid attempt: terminal status, error_message empty or an agent/verifier
    timeout, and total_steps present. Harness errors (exit 137/143, missing
    reward file, ...) are dropped, matching the sheet's denominators.
  * model preference: opus-4-8 > any other opus > the non-opus model with the
    most valid trials (ties -> most recently finished).

Metrics per trial:
  steps        = total_steps (ATIF trajectory steps)
  runtime_s    = phase_timing.agent_execution.duration_sec, else
                 trajectory_duration_seconds, else finished_at - started_at
  tokens_in    = input_tokens (includes cache reads)   tokens_cache = cache_tokens
  tokens_out   = output_tokens                          tokens_total = in + out
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ODDISH_API_URL", "https://abundant-ai--api.modal.run").rstrip("/")
KEY = os.environ["ODDISH_API_KEY"]
WORKERS = int(os.environ.get("WORKERS", "8"))

TIMEOUT_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")
TERMINAL = {"success", "failed", "completed", "error", "cancelled", "timeout"}
BASELINE_AGENTS = {"nop", "oracle"}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def get(path: str, tries: int = 4, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_valid(tr: dict) -> bool:
    if tr.get("kind", "agent") != "agent" or tr.get("is_probe"):
        return False
    if tr.get("superseded_by_trial_id"):
        return False
    if (tr.get("agent") or "").lower() in BASELINE_AGENTS:
        return False
    status = (tr.get("status") or "").lower()
    if status not in TERMINAL:
        return False
    err = (tr.get("error_message") or "").lower()
    if err and not any(k in err for k in TIMEOUT_ERR):
        return False
    return tr.get("total_steps") is not None


def runtime_seconds(tr: dict) -> float | None:
    pt = tr.get("phase_timing") or {}
    ae = pt.get("agent_execution") or {}
    if isinstance(ae, dict) and ae.get("duration_sec") is not None:
        return float(ae["duration_sec"])
    if tr.get("trajectory_duration_seconds") is not None:
        return float(tr["trajectory_duration_seconds"])
    s, f = parse_dt(tr.get("started_at")), parse_dt(tr.get("finished_at"))
    if s and f:
        return (f - s).total_seconds()
    return None


def canon_model(model: str | None) -> str:
    """Collapse provider routing prefixes so one model has one name.

    global.anthropic.claude-opus-4-8, anthropic-hdo/claude-opus-4-8 -> claude-opus-4-8
    us.anthropic.claude-opus-4-1-20250805-v1:0 -> claude-opus-4-1-20250805-v1:0
    """
    m = (model or "?").strip()
    m = m.rsplit("/", 1)[-1]
    for pre in ("global.anthropic.", "us.anthropic.", "eu.anthropic.", "anthropic."):
        if m.startswith(pre):
            m = m[len(pre):]
    return m.lower()


def model_rank(model: str) -> int:
    m = canon_model(model)
    if "opus" in m and ("4-8" in m or "4.8" in m or "4_8" in m):
        return 0
    if "opus" in m:
        return 1
    return 2


def choose_model(trials: list[dict]) -> str | None:
    """Return the canonical model whose trials should be reported for this task.

    opus-4-8 first, then any other opus, then the non-opus model with the most
    trials; ties broken by most recent finish.
    """
    by_model: dict[str, list[dict]] = {}
    for tr in trials:
        by_model.setdefault(canon_model(tr.get("model")), []).append(tr)
    if not by_model:
        return None

    def latest_ts(rows: list[dict]) -> float:
        ts = [parse_dt(tr.get("finished_at")) for tr in rows]
        return max((t.timestamp() for t in ts if t), default=0.0)

    return min(by_model, key=lambda m: (model_rank(m), -len(by_model[m]), -latest_ts(by_model[m])))


def pick_task_ids(items: list[dict], name: str, sheet_exps: set[str]) -> list[dict]:
    exact = [it for it in items if it.get("name") == name]
    if not exact:
        return []
    if sheet_exps:
        hit = [it for it in exact if {e["id"] for e in it.get("experiments", [])} & sheet_exps]
        if hit:
            return hit
    return exact


def process(row: dict) -> dict:
    name = row["task"]
    sheet_exps = {e for e in (row.get("experiments") or "").split(";") if e}
    out = {**row, "task_ids": "", "task_version": "", "experiments_used": "", "note": ""}
    notes: list[str] = []
    try:
        browse = get("/tasks/browse", query=name, limit=100)
    except Exception as e:  # noqa: BLE001
        out["note"] = f"browse failed: {e}"
        out["_raw"] = []
        return out
    picked = pick_task_ids(browse.get("items", []), name, sheet_exps)
    if not picked:
        out["note"] = "task not found by exact name"
        out["_raw"] = []
        return out
    if len(picked) > 1:
        notes.append(f"{len(picked)} task ids share this name; pooled")
    out["task_ids"] = ";".join(it["id"] for it in picked)

    raw: list[dict] = []
    for it in picked:
        try:
            raw.extend(get(f"/tasks/{it['id']}/trials"))
        except Exception as e:  # noqa: BLE001
            notes.append(f"trials fetch failed for {it['id']}: {e}")
    out["_raw"] = raw

    cand = [tr for tr in raw if tr.get("kind", "agent") == "agent" and not tr.get("is_probe")
            and not tr.get("superseded_by_trial_id")
            and (tr.get("agent") or "").lower() not in BASELINE_AGENTS]
    # restrict to the sheet's experiments when possible
    if sheet_exps:
        in_exp = [tr for tr in cand if tr.get("experiment_id") in sheet_exps]
        if in_exp:
            cand = in_exp
        else:
            notes.append("no trials in sheet experiments; used all experiments")
    # Latest task version among candidates (the sheet's "latest experiment
    # version only" rule). Pool versions only when that is what reproduces the
    # sheet's opus denominator (a few multi-version sweeps were tallied that way).
    versions = [tr.get("task_version") for tr in cand if tr.get("task_version") is not None]
    want_n = int(row["opus_table"].split("/")[1]) if row.get("opus_table") else None

    def n_opus(rows: list[dict]) -> int:
        return sum(1 for tr in rows if is_valid(tr) and model_rank(tr.get("model")) == 0)

    if versions:
        latest = max(versions)
        latest_rows = [tr for tr in cand if tr.get("task_version") == latest]
        all_versions = ";".join(str(v) for v in sorted(set(versions)))
        if not any(is_valid(tr) for tr in latest_rows):
            notes.append("latest version has no valid trials; used all versions")
            out["task_version"] = all_versions
        elif want_n is not None and n_opus(latest_rows) != want_n and n_opus(cand) == want_n:
            notes.append("pooled task versions to match the sheet's opus denominator")
            out["task_version"] = all_versions
        else:
            cand = latest_rows
            out["task_version"] = str(latest)
    out["experiments_used"] = ";".join(sorted({tr.get("experiment_id") or "" for tr in cand}))

    valid = [tr for tr in cand if is_valid(tr)]
    model = choose_model(valid)
    if model is None:
        out["note"] = "; ".join(notes + [f"no valid solver trials ({len(cand)} candidates all errored/non-terminal)"])
        return out
    rows = [tr for tr in valid if canon_model(tr.get("model")) == model]
    n_errored = sum(1 for tr in cand if canon_model(tr.get("model")) == model and not is_valid(tr))
    if n_errored:
        notes.append(f"{n_errored} {model} trials errored/non-terminal (excluded)")
    other_models = sorted({canon_model(tr.get("model")) for tr in valid if canon_model(tr.get("model")) != model})
    out["model_variants"] = ";".join(sorted({tr.get("model") or "?" for tr in rows}))

    steps = [tr["total_steps"] for tr in rows]
    rts = [r for r in (runtime_seconds(tr) for tr in rows) if r is not None]
    tin = [tr.get("input_tokens") for tr in rows if tr.get("input_tokens") is not None]
    tout = [tr.get("output_tokens") for tr in rows if tr.get("output_tokens") is not None]
    tcache = [tr.get("cache_tokens") for tr in rows if tr.get("cache_tokens") is not None]
    ttot = [(tr.get("input_tokens") or 0) + (tr.get("output_tokens") or 0) for tr in rows
            if tr.get("input_tokens") is not None or tr.get("output_tokens") is not None]
    passes = sum(1 for tr in rows if (tr.get("reward") or 0) >= 1.0 - 1e-9)

    def med(xs):
        return round(statistics.median(xs), 1) if xs else ""

    def mean(xs):
        return round(statistics.fmean(xs), 1) if xs else ""

    out.update({
        "model_used": model,
        "model_tier": ["opus-4-8", "other-opus", "non-opus"][model_rank(model)],
        "n_trials": len(rows),
        "passes": passes,
        "steps_median": med(steps), "steps_mean": mean(steps),
        "steps_min": min(steps) if steps else "", "steps_max": max(steps) if steps else "",
        "runtime_min_median": med([r / 60 for r in rts]), "runtime_min_mean": mean([r / 60 for r in rts]),
        "tokens_total_median": med(ttot), "tokens_total_mean": mean(ttot),
        "tokens_in_median": med(tin), "tokens_out_median": med(tout), "tokens_cache_median": med(tcache),
        "other_models_available": ";".join(other_models),
    })
    # cross-check against the sheet's opus denominator
    if row.get("opus_table") and model_rank(model) == 0:
        want_n = int(row["opus_table"].split("/")[1])
        want_p = int(row["opus_table"].split("/")[0])
        if want_n != len(rows) or want_p != passes:
            notes.append(f"sheet opus {row['opus_table']} vs fetched {passes}/{len(rows)}")
    out["note"] = "; ".join(notes)
    return out


def main() -> None:
    with open(os.path.join(HERE, os.environ.get("TASKS_FILE", "tasks.tsv")), newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    log(f"{len(rows)} tasks, {WORKERS} workers")
    results: list[dict] = []
    raw_by_task: dict[str, list[dict]] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, r): r["task"] for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            raw_by_task[res["task"]] = res.pop("_raw", [])
            results.append(res)
            if i % 25 == 0 or i == len(rows):
                log(f"  {i}/{len(rows)} done ({time.time()-t0:.0f}s)")
    order = {r["task"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order[r["task"]])

    with open(os.path.join(HERE, "raw_trials.json"), "w") as f:
        json.dump(raw_by_task, f)

    cols = ["task", "category", "model_used", "model_tier", "model_variants", "n_trials", "passes",
            "steps_median", "steps_mean", "steps_min", "steps_max",
            "runtime_min_median", "runtime_min_mean",
            "tokens_total_median", "tokens_total_mean",
            "tokens_in_median", "tokens_out_median", "tokens_cache_median",
            "opus_table", "learn_table", "other_models_available",
            "task_ids", "task_version", "experiments", "experiments_used", "note"]
    with open(os.path.join(HERE, "task_metrics.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({c: r.get(c, "") for c in cols})
    log(f"wrote task_metrics.csv ({len(results)} rows) in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
