#!/usr/bin/env python3
"""Summarise task_metrics.csv: coverage, per-category medians, and reconciliation flags."""
import csv
import statistics
import sys
from collections import Counter, defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "task_metrics.csv"
rows = list(csv.DictReader(open(path)))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


print(f"rows: {len(rows)}")
print("model tier:", Counter(r["model_tier"] or "(none)" for r in rows))
print("model used:", Counter(r["model_used"] or "(none)" for r in rows).most_common(12))

print("\n== per-category medians (median across tasks of the per-task median) ==")
print(f"{'category':28s} {'tasks':>5s} {'w/data':>6s} {'opus48':>6s} {'steps':>7s} {'runtime_min':>11s} {'tokens_total':>13s} {'tokens_out':>10s}")
by_cat = defaultdict(list)
for r in rows:
    by_cat[r["category"]].append(r)
for cat, rs in sorted(by_cat.items()):
    withdata = [r for r in rs if f(r["steps_median"]) is not None]
    opus = [r for r in withdata if r["model_tier"] == "opus-4-8"]

    def med(key):
        xs = [f(r[key]) for r in withdata if f(r[key]) is not None]
        return statistics.median(xs) if xs else float("nan")

    print(f"{cat:28s} {len(rs):5d} {len(withdata):6d} {len(opus):6d} {med('steps_median'):7.0f} {med('runtime_min_median'):11.1f} {med('tokens_total_median'):13,.0f} {med('tokens_out_median'):10,.0f}")

print("\n== tasks with no metrics ==")
for r in rows:
    if f(r["steps_median"]) is None:
        print(f"  {r['task']:55s} {r['note']}")

print("\n== non-opus fallbacks ==")
for r in rows:
    if r["model_tier"] and r["model_tier"] != "opus-4-8" and f(r["steps_median"]) is not None:
        print(f"  {r['task']:55s} {r['model_used']:40s} n={r['n_trials']} sheet_opus={r['opus_table'] or '-'}  {r['note']}")

print("\n== sheet/fetch opus mismatches ==")
n_mis = 0
for r in rows:
    if "sheet opus" in (r["note"] or ""):
        n_mis += 1
        print(f"  {r['task']:55s} {r['note']}")
print(f"  total mismatches: {n_mis}")

print("\n== other notes ==")
for r in rows:
    note = r["note"] or ""
    if note and "sheet opus" not in note and "errored/non-terminal" not in note and f(r["steps_median"]) is not None:
        print(f"  {r['task']:55s} {note}")
