from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import text

from oddish.core.task_browse_rollup import refresh_task_browse_rollups_for_trials
from oddish.db import get_session
from oddish.model_pricing import settle_cost_usd

# Both a fake $0 (Claude Code passthrough prices unknown models at $0) and a
# NULL (a model that was unpriceable when the trial was settled) are "unsettled"
# for our purposes: if we can now price the tokens, re-price them.
_SELECT_UNSETTLED = text(
    """
    SELECT id, model, cost_usd,
           input_tokens, output_tokens, cache_tokens, cache_write_tokens
    FROM trials
    WHERE (cost_usd = 0 OR cost_usd IS NULL)
      AND (
        COALESCE(input_tokens, 0) > 0
        OR COALESCE(output_tokens, 0) > 0
        OR COALESCE(cache_write_tokens, 0) > 0
      )
      AND id > :after
    ORDER BY id
    LIMIT :page
    """
)

# Guard on the same predicate the select used so a concurrent retry that wrote a
# real positive cost between select and update is never clobbered.
_UPDATE_COST = text(
    """
    UPDATE trials
    SET cost_usd = :cost
    WHERE id = :id AND (cost_usd = 0 OR cost_usd IS NULL)
    """
)

_PAGE_SIZE = 5000
_UPDATE_CHUNK_SIZE = 500


def _plan_update(
    existing_cost: float | None, settled_cost: float | None
) -> tuple[str, float | None] | None:
    """Decide how to persist a re-priced row, or ``None`` for no write.

    - priceable -> write the token estimate.
    - unpriceable but currently a fake $0 -> flip to NULL so quota bills the
      flat unpriced rate instead of treating the $0 as authoritative.
    - unpriceable and already NULL -> nothing to do (already on the flat rate);
      skip to avoid a pointless NULL -> NULL write across every such row.
    """
    if settled_cost is not None:
        return ("cost", settled_cost)
    if existing_cost is not None:
        return ("cost", None)
    return None


async def run_backfill(*, apply: bool) -> None:
    trials: Counter[str] = Counter()
    dollars: Counter[str] = Counter()
    unpriced: Counter[str] = Counter()
    total_rows = 0
    after = ""

    while True:
        async with get_session() as session:
            rows = (
                await session.execute(
                    _SELECT_UNSETTLED, {"after": after, "page": _PAGE_SIZE}
                )
            ).fetchall()
            if not rows:
                break
            after = rows[-1].id
            total_rows += len(rows)

            updates: list[dict] = []
            for row in rows:
                cost = settle_cost_usd(
                    0.0,
                    native_cost_trusted=False,
                    model=row.model,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cache_tokens=row.cache_tokens,
                    cache_write_tokens=row.cache_write_tokens,
                )
                model = row.model or "unknown"
                if cost is None:
                    unpriced[model] += 1
                else:
                    trials[model] += 1
                    dollars[model] += cost

                if apply:
                    plan = _plan_update(row.cost_usd, cost)
                    if plan is not None:
                        updates.append({"id": row.id, "cost": plan[1]})

            if updates:
                for start in range(0, len(updates), _UPDATE_CHUNK_SIZE):
                    chunk = updates[start : start + _UPDATE_CHUNK_SIZE]
                    await session.execute(_UPDATE_COST, chunk)
                    await refresh_task_browse_rollups_for_trials(
                        session, [str(update["id"]) for update in chunk]
                    )

    if not total_rows:
        print("No unsettled ($0 / NULL) trials with token usage found.")
        return

    print(f"Unsettled ($0 / NULL) trials with token usage: {total_rows}")
    for model, total in dollars.most_common():
        print(f"  {model}: {trials[model]} trials -> ${total:.2f}")
    for model, count in unpriced.most_common():
        print(f"  {model}: {count} trials -> unpriced (bills the flat quota rate)")
    print(f"Total: {total_rows} trials, ${sum(dollars.values()):.2f} estimated spend")
    if apply:
        print("\nBackfill applied.")
    else:
        print("\nDry run complete. Re-run with --apply to execute updates.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-price trials stored with cost_usd=0 or NULL despite token usage."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute updates. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(apply=args.apply))


if __name__ == "__main__":
    main()
