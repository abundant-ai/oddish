"""Attribute actual AWS Bedrock spend down to trials, tasks, and experiments.

``trials.cost_usd`` already holds Bedrock spend, but it is the claude-code CLI's
*list-price* figure (``settle_cost_usd`` trusts the harness-reported
``total_cost_usd``). AWS bills something else. This script takes the Cost
Explorer actuals from ``bedrock_cost_explorer.py`` and allocates each
(month, model) invoice total across the trials that ran on it, in proportion to
each trial's recorded list-price cost.

Why proportional-to-list-price rather than proportional-to-tokens: list price
already weights input vs output vs cache-read vs cache-write at their true rate
ratios, so it is the better proxy. If Bedrock bills linearly per token for a
model, this allocation is *exact* at the (month, model) level -- the only error
is a differing cache-tier mix between trials.

Runs entirely as analytics: it never writes to ``trials``. The reconciliation
table it prints (actual / list ratio per model per month) is the input you would
need to correct ``cost_usd`` at settlement -- see the module docstring in
``bedrock_cost_explorer.py`` and the PR description for that discussion.

    cd backend
    # 1. Suggest a usage-type -> trials.model mapping, then EDIT it by hand.
    uv run python scripts/bedrock_cost_attribution.py \
        --ce bedrock_ce.json --write-mapping bedrock_mapping.json

    # 2. Allocate.
    uv run python scripts/bedrock_cost_attribution.py \
        --ce bedrock_ce.json --mapping bedrock_mapping.json --out-dir ./bedrock-attrib

Reads prod trials read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

# Bedrock inference-profile prefixes as they land in ``trials.model``.
BEDROCK_MODEL_RE = re.compile(
    r"^(global|us|eu|apac)\.(anthropic|amazon|meta|mistral)\.|^bedrock/", re.IGNORECASE
)

# Region code that prefixes a Cost Explorer usage type, e.g. "USE1-".
_REGION_PREFIX_RE = re.compile(r"^[A-Z]{2,4}\d?-")
# Token-type suffixes CE appends per metering dimension.
_TOKEN_SUFFIX_RE = re.compile(
    r"(input|output|cache[-_]?(read|write)?)[-_]?tokens?([-_]?count)?", re.IGNORECASE
)
_NOISE_RE = re.compile(
    r"(bedrock|edition|amazon|anthropic|global|apac|v\d+)", re.IGNORECASE
)


def normalize_model_key(value: str) -> str:
    """Collapse a CE usage type or a ``trials.model`` id to a comparable key.

    Both sides name the same model very differently
    (``USE1-BedrockClaude3.5Sonnet-InputTokenCount`` vs
    ``global.anthropic.claude-3-5-sonnet-v1``), so strip region, token-type,
    vendor noise and punctuation and compare what is left.
    """
    text = _REGION_PREFIX_RE.sub("", value)
    text = _TOKEN_SUFFIX_RE.sub("", text)
    text = _NOISE_RE.sub("", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def suggest_mapping(usage_types: list[str], models: list[str]) -> dict[str, str | None]:
    """Best-effort usage type -> ``trials.model`` guesses, for human review.

    Deliberately returns ``None`` rather than a weak guess: a wrong mapping
    silently misattributes real money, so an unmapped entry (which the allocator
    reports loudly) is the safer failure.
    """
    by_key: dict[str, list[str]] = defaultdict(list)
    for model in models:
        by_key[normalize_model_key(model)].append(model)

    suggestions: dict[str, str | None] = {}
    for usage_type in usage_types:
        key = normalize_model_key(usage_type)
        if key in by_key:
            suggestions[usage_type] = sorted(by_key[key])[0]
            continue
        # Fall back to substring containment, but only when exactly one model
        # matches -- ambiguity means we do not guess.
        hits = [
            m for k, ms in by_key.items() for m in ms if k and (k in key or key in k)
        ]
        suggestions[usage_type] = sorted(set(hits))[0] if len(set(hits)) == 1 else None
    return suggestions


@dataclass(frozen=True)
class TrialWeight:
    """One trial's claim on its (period, model) invoice total."""

    trial_id: str
    period: str
    model: str
    list_cost_usd: float
    task_id: str | None = None
    experiment_id: str | None = None


@dataclass
class Allocation:
    """Result of distributing invoice totals across trials."""

    per_trial: dict[str, float] = field(default_factory=dict)
    # (period, model) -> (actual, list_total, trial_count)
    reconciliation: dict[tuple[str, str], tuple[float, float, int]] = field(
        default_factory=dict
    )
    # Invoice money with no trials to attribute it to.
    unattributed: dict[tuple[str, str], float] = field(default_factory=dict)
    # Trials whose (period, model) had no invoice line.
    unreconciled: dict[tuple[str, str], float] = field(default_factory=dict)


def allocate(
    actuals: dict[tuple[str, str], float], trials: list[TrialWeight]
) -> Allocation:
    """Split each (period, model) invoice total across its trials pro rata.

    Weighting is by ``list_cost_usd``. When a group's weights sum to zero (all
    trials unpriced) the total is split evenly instead, so the money still lands
    somewhere attributable rather than silently vanishing.
    """
    grouped: dict[tuple[str, str], list[TrialWeight]] = defaultdict(list)
    for trial in trials:
        grouped[(trial.period, trial.model)].append(trial)

    result = Allocation()

    for key, actual in actuals.items():
        members = grouped.get(key)
        if not members:
            result.unattributed[key] = actual
            continue
        total_weight = sum(t.list_cost_usd for t in members)
        result.reconciliation[key] = (actual, total_weight, len(members))
        if total_weight > 0:
            for trial in members:
                result.per_trial[trial.trial_id] = (
                    actual * trial.list_cost_usd / total_weight
                )
        else:
            share = actual / len(members)
            for trial in members:
                result.per_trial[trial.trial_id] = share

    for key, members in grouped.items():
        if key not in actuals:
            result.unreconciled[key] = sum(t.list_cost_usd for t in members)
            for trial in members:
                result.per_trial.setdefault(trial.trial_id, 0.0)

    return result


def rollup(trials: list[TrialWeight], per_trial: dict[str, float], attr: str):
    """Sum allocated cost by ``task_id`` or ``experiment_id``."""
    out: dict[str, dict[str, float]] = defaultdict(
        lambda: {"actual_usd": 0.0, "list_usd": 0.0, "trials": 0}
    )
    for trial in trials:
        key = getattr(trial, attr) or "__none__"
        out[key]["actual_usd"] += per_trial.get(trial.trial_id, 0.0)
        out[key]["list_usd"] += trial.list_cost_usd
        out[key]["trials"] += 1
    return out


TRIALS_QUERY = """
SELECT
    t.id                                                   AS trial_id,
    t.task_id                                              AS task_id,
    t.experiment_id                                        AS experiment_id,
    t.model                                                AS model,
    to_char(date_trunc('month', t.finished_at AT TIME ZONE 'UTC'), 'YYYY-MM')
                                                           AS period,
    COALESCE(t.cost_usd, 0)                                AS list_cost_usd
FROM trials t
JOIN experiments e ON e.id = t.experiment_id
WHERE t.finished_at >= $1
  AND t.finished_at < $2
  AND t.origin = 'oddish'
  AND t.model ~* '^(global|us|eu|apac)\\.(anthropic|amazon|meta|mistral)\\.|^bedrock/'
  AND (t.idempotency_key IS NULL OR t.idempotency_key NOT LIKE 'combine:%')
  AND NOT EXISTS (
      SELECT 1 FROM cost_excluded_llm_keys c
      WHERE c.key_hash = t.llm_key_hash AND c.deleted_at IS NULL
  )
  AND (t.billed_user_id IS NOT NULL OR t.is_probe IS FALSE)
"""


async def load_trials(database_url: str, start: str, end: str) -> list[TrialWeight]:
    import asyncpg

    # pgbouncer in transaction mode recycles the server conn between statements.
    conn = await asyncpg.connect(database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(TRIALS_QUERY, _as_utc(start), _as_utc(end))
    finally:
        await conn.close()
    return [
        TrialWeight(
            trial_id=r["trial_id"],
            period=r["period"],
            model=r["model"],
            list_cost_usd=float(r["list_cost_usd"]),
            task_id=r["task_id"],
            experiment_id=r["experiment_id"],
        )
        for r in rows
    ]


def _as_utc(value: str):
    import datetime as dt

    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


def _resolve_database_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("ODDISH_DATABASE_URL")
    if not url:
        sys.exit("Set ODDISH_DATABASE_URL or pass --database-url.")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    if any(host in url for host in ("localhost", "127.0.0.1")) and not explicit:
        # backend/.env points at the local dev DB and modal_app appends it LAST
        # to runtime_secrets, so an unguarded default silently reports dev data.
        sys.exit(
            "ODDISH_DATABASE_URL points at localhost. That is the dev DB, not prod.\n"
            "Pass --database-url explicitly if you really mean it."
        )
    return url


def load_ce(path: str, mapping: dict[str, str | None] | None):
    """Fold CE records into {(period, model): actual_usd}."""
    records = json.load(open(path))
    actuals: dict[tuple[str, str], float] = defaultdict(float)
    unmapped: dict[str, float] = defaultdict(float)
    for rec in records:
        period = rec["period_start"][:7]
        usage_type = rec["usage_type"]
        model = (mapping or {}).get(usage_type)
        if not model:
            unmapped[usage_type] += rec["amount_usd"]
            continue
        actuals[(period, model)] += rec["amount_usd"]
    return dict(actuals), dict(unmapped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--ce", required=True, help="JSON from bedrock_cost_explorer.py"
    )
    parser.add_argument("--mapping", help="usage_type -> trials.model JSON")
    parser.add_argument("--write-mapping", help="write a suggested mapping and exit")
    parser.add_argument("--database-url", help="defaults to $ODDISH_DATABASE_URL")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--out-dir", default="./bedrock-attrib")
    args = parser.parse_args(argv)

    url = _resolve_database_url(args.database_url)
    trials = asyncio.run(load_trials(url, args.start, args.end))
    if not trials:
        print("No Bedrock-routed trials in that window.")
        return 1
    print(f"loaded {len(trials):,} Bedrock-routed trials")

    if args.write_mapping:
        usage_types = sorted({r["usage_type"] for r in json.load(open(args.ce))})
        models = sorted({t.model for t in trials})
        suggested = suggest_mapping(usage_types, models)
        with open(args.write_mapping, "w") as fh:
            json.dump(suggested, fh, indent=2, sort_keys=True)
        unresolved = [k for k, v in suggested.items() if v is None]
        print(f"wrote {args.write_mapping}: {len(suggested)} usage types")
        print(
            f"  {len(suggested) - len(unresolved)} auto-matched, {len(unresolved)} need review"
        )
        for key in unresolved:
            print(f"    UNMAPPED  {key}")
        print(
            "\nEDIT THIS FILE before allocating -- a wrong mapping misattributes money."
        )
        return 0

    if not args.mapping:
        sys.exit("Pass --mapping (build one with --write-mapping first).")
    mapping = json.load(open(args.mapping))
    actuals, unmapped = load_ce(args.ce, mapping)
    if unmapped:
        print(
            f"\nWARNING: {len(unmapped)} unmapped usage types, "
            f"${sum(unmapped.values()):,.2f} excluded:"
        )
        for ut, amount in sorted(unmapped.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    ${amount:>12,.2f}  {ut}")

    result = allocate(actuals, trials)

    os.makedirs(args.out_dir, exist_ok=True)
    with open(f"{args.out_dir}/per_trial.jsonl", "w") as fh:
        for trial in trials:
            fh.write(
                json.dumps(
                    {
                        "trial_id": trial.trial_id,
                        "task_id": trial.task_id,
                        "experiment_id": trial.experiment_id,
                        "period": trial.period,
                        "model": trial.model,
                        "list_cost_usd": trial.list_cost_usd,
                        "actual_cost_usd": result.per_trial.get(trial.trial_id, 0.0),
                    }
                )
                + "\n"
            )

    for attr, name in (("task_id", "by_task"), ("experiment_id", "by_experiment")):
        data = rollup(trials, result.per_trial, attr)
        with open(f"{args.out_dir}/{name}.json", "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)

    print("\n=== RECONCILIATION: actual vs list price ===\n")
    print(
        f"{'Period':<9}{'Model':<40}{'Actual':>13}{'List':>13}{'Ratio':>8}{'Trials':>8}"
    )
    print("-" * 91)
    for (period, model), (actual, listed, count) in sorted(
        result.reconciliation.items()
    ):
        ratio = f"{actual / listed:.2f}x" if listed else "n/a"
        print(
            f"{period:<9}{model[:39]:<40}{actual:>13,.2f}{listed:>13,.2f}{ratio:>8}{count:>8,}"
        )

    total_actual = sum(a for a, _, _ in result.reconciliation.values())
    total_list = sum(v for _, v, _ in result.reconciliation.values())
    print(
        f"\n{'TOTAL':<49}{total_actual:>13,.2f}{total_list:>13,.2f}"
        f"{(total_actual / total_list if total_list else 0):>7.2f}x"
    )

    if result.unattributed:
        print(
            f"\nUNATTRIBUTED (invoice lines with no matching trials): "
            f"${sum(result.unattributed.values()):,.2f}"
        )
        for key, amount in sorted(result.unattributed.items(), key=lambda kv: -kv[1])[
            :10
        ]:
            print(f"    ${amount:>12,.2f}  {key[0]} {key[1]}")
    if result.unreconciled:
        print(
            f"\nUNRECONCILED (trials with no invoice line): "
            f"${sum(result.unreconciled.values()):,.2f} at list price"
        )
        for key, amount in sorted(result.unreconciled.items(), key=lambda kv: -kv[1])[
            :10
        ]:
            print(f"    ${amount:>12,.2f}  {key[0]} {key[1]}")

    print(f"\nwrote {args.out_dir}/{{per_trial.jsonl,by_task.json,by_experiment.json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
