#!/usr/bin/env python3
"""Per-task trajectory metrics (steps, agent runtime, tokens) from the oddish API.

Input : tasks.tsv  (task, category, opus_table, learn_table, experiments[, alt_names])
Output: raw_trials.json  (every trial fetched, keyed by task name; reused as a cache)
        task_metrics.csv (one row per task)

Selection rules, per task:
  * Task lookup: GET /tasks/browse?query=<name>, exact match on the sheet name
    or one of its ``alt_names``; if nothing matches, the name with its last
    ".xxx" suffix removed is tried too (oddish stores "post-train-apps-qwen2.5-1"
    for "post-train-apps-qwen2.5-1.5b"). When several task ids match, the ones
    whose linked experiments intersect the sheet's experiment ids win.
  * Trials: kind == "agent", not a probe, not superseded, agent not nop/oracle.
    Restricted to the sheet's linked experiment ids when the task has trials
    there (fallback: every experiment), then to the latest task_version seen in
    that set (versions are pooled only when that reproduces the sheet's Opus
    denominator) -- this mirrors the sheet's "latest experiment version only".
  * A trial counts when it produced a real trajectory: terminal status,
    ``total_steps`` present, and non-zero token usage. Agent timeouts and
    runs that ended with a non-zero agent exit but still have a trajectory are
    kept (and counted in ``n_timeouts`` / ``n_exit_errors``); sandbox failures
    and zero-token runs are dropped (``n_excluded``).
  * Model preference: opus-4-8 (its provider spellings pooled) > any other
    opus > the non-opus model with the most valid trials, ties -> most recent.

Metrics per trial:
  steps        = total_steps (ATIF trajectory steps)
  runtime_s    = phase_timing.agent_execution.duration_sec, else
                 trajectory_duration_seconds, else finished_at - started_at
  tokens_in    = input_tokens (includes cache reads)   tokens_cache = cache_tokens
  tokens_out   = output_tokens                          tokens_total = in + out

Environment: ODDISH_API_KEY (required), ODDISH_API_URL, TASKS_FILE, WORKERS,
RAW_CACHE (path of a previous raw_trials.json to reuse; default: the one next
to this script if present). Tasks with ``alt_names`` are always refetched.
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
KEY = os.environ.get("ODDISH_API_KEY", "")
WORKERS = int(os.environ.get("WORKERS", "8"))

TIMEOUT_ERR = ("agenttimeouterror", "verifiertimeouterror", "timed out after")
TERMINAL = {"success", "failed", "completed", "error", "cancelled", "timeout"}
BASELINE_AGENTS = {"nop", "oracle"}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def get(path: str, tries: int = 4, **params):
    if not KEY:
        raise RuntimeError("ODDISH_API_KEY is not set")
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


def is_solver(tr: dict) -> bool:
    """A real solution attempt row (not baseline, probe, or superseded)."""
    if tr.get("kind", "agent") != "agent" or tr.get("is_probe"):
        return False
    if tr.get("superseded_by_trial_id"):
        return False
    return (tr.get("agent") or "").lower() not in BASELINE_AGENTS


def is_timeout(tr: dict) -> bool:
    err = (tr.get("error_message") or "").lower()
    return any(k in err for k in TIMEOUT_ERR)


def is_valid(tr: dict) -> bool:
    """The trial produced a real trajectory we can measure."""
    if not is_solver(tr):
        return False
    if (tr.get("status") or "").lower() not in TERMINAL:
        return False
    steps = tr.get("total_steps")
    if steps is None:
        return False
    tin, tout = tr.get("input_tokens"), tr.get("output_tokens")
    if tin is None and tout is None:  # usage unknown (older rows): require a real trajectory
        return steps >= 2
    return (tin or 0) + (tout or 0) > 0


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


def model_rank(model: str | None) -> int:
    m = canon_model(model)
    if "opus" in m and ("4-8" in m or "4.8" in m or "4_8" in m):
        return 0
    if "opus" in m:
        return 1
    return 2


MODEL_TIER = ["opus-4-8", "other-opus", "non-opus"]


def choose_model(trials: list[dict]) -> str | None:
    """Canonical model whose trials are reported: opus-4-8 > other opus >
    the non-opus model with the most trials; ties -> most recent finish."""
    by_model: dict[str, list[dict]] = {}
    for tr in trials:
        by_model.setdefault(canon_model(tr.get("model")), []).append(tr)
    if not by_model:
        return None

    def latest_ts(rows: list[dict]) -> float:
        ts = [parse_dt(tr.get("finished_at")) for tr in rows]
        return max((t.timestamp() for t in ts if t), default=0.0)

    return min(by_model, key=lambda m: (model_rank(m), -len(by_model[m]), -latest_ts(by_model[m])))


def task_id_of(tr: dict) -> str:
    return tr["id"].rsplit("-", 1)[0]


def resolve_task_ids(name: str, alt_names: list[str], sheet_exps: set[str]) -> tuple[list[dict], str]:
    """Browse for the task; return (matching browse items, note)."""
    wanted = [name] + alt_names
    if "." in name:
        wanted.append(name.rsplit(".", 1)[0])
    seen: dict[str, dict] = {}
    matched_as = ""
    for q in wanted:
        for it in get("/tasks/browse", query=q, limit=100).get("items", []):
            if it.get("name") in wanted and it["id"] not in seen:
                seen[it["id"]] = it
        if seen:
            break
    items = list(seen.values())
    if not items:
        return [], ""
    if sheet_exps:
        hit = [it for it in items if {e["id"] for e in it.get("experiments", [])} & sheet_exps]
        if hit:
            items = hit
    if any(it["name"] != name for it in items):
        matched_as = ";".join(sorted({it["name"] for it in items if it["name"] != name}))
    return items, matched_as


def process(row: dict, cache: dict | None) -> dict:
    name = row["task"]
    sheet_exps = {e for e in (row.get("experiments") or "").split(";") if e}
    alt_names = [a for a in (row.get("alt_names") or "").split(";") if a]
    out = {**row, "task_ids": "", "task_version": "", "experiments_used": "", "note": ""}
    notes: list[str] = []

    raw: list[dict] = []
    if cache and not alt_names:
        raw = list(cache.get("trials") or [])
    if not raw:
        try:
            items, matched_as = resolve_task_ids(name, alt_names, sheet_exps)
        except Exception as e:  # noqa: BLE001
            out["note"] = f"browse failed: {e}"
            out["_raw"] = []
            return out
        if not items:
            out["note"] = "task not found by exact name"
            out["_raw"] = []
            return out
        if matched_as:
            notes.append(f"matched oddish task name {matched_as}")
        for it in items:
            try:
                raw.extend(get(f"/tasks/{it['id']}/trials"))
            except Exception as e:  # noqa: BLE001
                notes.append(f"trials fetch failed for {it['id']}: {e}")
    out["_raw"] = raw
    task_ids = sorted({task_id_of(tr) for tr in raw})
    out["task_ids"] = ";".join(task_ids)
    if len(task_ids) > 1:
        notes.append(f"{len(task_ids)} task ids share this name; pooled")

    cand = [tr for tr in raw if is_solver(tr)]
    if sheet_exps:
        in_exp = [tr for tr in cand if tr.get("experiment_id") in sheet_exps]
        if in_exp:
            cand = in_exp
        else:
            notes.append("no trials in sheet experiments; used all experiments")

    # Latest task version (the sheet's "latest experiment version only" rule);
    # pool versions only when that is what reproduces the sheet's Opus denominator.
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
        out["note"] = "; ".join(notes + [f"no measurable solver trials ({len(cand)} candidates, none produced a trajectory)"])
        return out
    rows = [tr for tr in valid if canon_model(tr.get("model")) == model]
    n_excluded = sum(1 for tr in cand if canon_model(tr.get("model")) == model and not is_valid(tr))
    n_timeouts = sum(1 for tr in rows if is_timeout(tr))
    n_exit_errors = sum(1 for tr in rows if tr.get("error_message") and not is_timeout(tr))
    other_models = sorted({canon_model(tr.get("model")) for tr in valid if canon_model(tr.get("model")) != model})
    if n_excluded:
        notes.append(f"{n_excluded} {model} trials without a trajectory excluded")

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

    # "Clean" subset: runs with no error string at all (no timeout, no non-zero
    # agent exit). This is the population the DB-debug sheet used for its
    # pass-rate denominators.
    clean = [tr for tr in rows if not tr.get("error_message")]
    clean_rts = [r for r in (runtime_seconds(tr) for tr in clean) if r is not None]
    clean_tot = [(tr.get("input_tokens") or 0) + (tr.get("output_tokens") or 0) for tr in clean
                 if tr.get("input_tokens") is not None or tr.get("output_tokens") is not None]

    out.update({
        "model_used": model,
        "model_tier": MODEL_TIER[model_rank(model)],
        "model_variants": ";".join(sorted({tr.get("model") or "?" for tr in rows})),
        "n_trials": len(rows),
        "passes": passes,
        "n_timeouts": n_timeouts,
        "n_exit_errors": n_exit_errors,
        "n_excluded": n_excluded,
        "steps_median": med(steps), "steps_mean": mean(steps),
        "steps_min": min(steps) if steps else "", "steps_max": max(steps) if steps else "",
        "runtime_min_median": med([r / 60 for r in rts]), "runtime_min_mean": mean([r / 60 for r in rts]),
        "runtime_min_max": round(max(rts) / 60, 1) if rts else "",
        "tokens_total_median": med(ttot), "tokens_total_mean": mean(ttot),
        "tokens_in_median": med(tin), "tokens_out_median": med(tout), "tokens_cache_median": med(tcache),
        "n_clean": len(clean),
        "passes_clean": sum(1 for tr in clean if (tr.get("reward") or 0) >= 1.0 - 1e-9),
        "steps_median_clean": med([tr["total_steps"] for tr in clean]),
        "runtime_min_median_clean": med([r / 60 for r in clean_rts]),
        "tokens_total_median_clean": med(clean_tot),
        "other_models_available": ";".join(other_models),
    })
    if row.get("opus_table") and model_rank(model) == 0:
        want_p, want_n = (int(x) for x in row["opus_table"].split("/"))
        if want_n != len(rows) or want_p != passes:
            notes.append(f"sheet opus {row['opus_table']} vs fetched {passes}/{len(rows)}")
    out["note"] = "; ".join(notes)
    return out


COLS = ["task", "category", "model_used", "model_tier", "model_variants",
        "n_trials", "passes", "n_timeouts", "n_exit_errors", "n_excluded",
        "steps_median", "steps_mean", "steps_min", "steps_max",
        "runtime_min_median", "runtime_min_mean", "runtime_min_max",
        "tokens_total_median", "tokens_total_mean",
        "tokens_in_median", "tokens_out_median", "tokens_cache_median",
        "n_clean", "passes_clean", "steps_median_clean", "runtime_min_median_clean", "tokens_total_median_clean",
        "opus_table", "learn_table", "other_models_available",
        "task_ids", "task_version", "experiments", "experiments_used", "note"]


def load_cache(path: str) -> dict[str, dict]:
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    cache: dict[str, dict] = {}
    for name, val in data.items():
        if isinstance(val, list):  # older cache layout: bare trial list
            cache[name] = {"trials": val}
        elif isinstance(val, dict):
            cache[name] = val
    return cache


def main() -> None:
    tasks_file = os.path.join(HERE, os.environ.get("TASKS_FILE", "tasks.tsv"))
    with open(tasks_file, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    cache_path = os.environ.get("RAW_CACHE", os.path.join(HERE, "raw_trials.json"))
    cache = load_cache(cache_path)
    log(f"{len(rows)} tasks, {WORKERS} workers, {sum(1 for r in rows if cache.get(r['task'], {}).get('trials'))} cached")
    results: list[dict] = []
    raw_by_task: dict[str, dict] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, r, cache.get(r["task"])): r["task"] for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            raw_by_task[res["task"]] = {"trials": res.pop("_raw", [])}
            results.append(res)
            if i % 25 == 0 or i == len(rows):
                log(f"  {i}/{len(rows)} done ({time.time()-t0:.0f}s)")
    order = {r["task"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order[r["task"]])

    with open(os.path.join(HERE, "raw_trials.json"), "w") as f:
        json.dump(raw_by_task, f)
    with open(os.path.join(HERE, "task_metrics.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({c: r.get(c, "") for c in COLS})
    log(f"wrote task_metrics.csv ({len(results)} rows) in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
