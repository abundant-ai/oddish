from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import text

from oddish.db import get_session
from oddish.model_pricing import settle_cost_usd

_SELECT_ZERO_COST = text(
    """
    SELECT id, model, input_tokens, output_tokens, cache_tokens, cache_write_tokens
    FROM trials
    WHERE cost_usd = 0
      AND cost_is_estimated IS NOT TRUE
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

_UPDATE_COST = text(
    """
    UPDATE trials
    SET cost_usd = :cost, cost_is_estimated = :est
    WHERE id = :id AND cost_usd = 0
    """
)

_PAGE_SIZE = 5000
_UPDATE_CHUNK_SIZE = 500


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
                    _SELECT_ZERO_COST, {"after": after, "page": _PAGE_SIZE}
                )
            ).fetchall()
            if not rows:
                break
            after = rows[-1].id
            total_rows += len(rows)

            updates: list[dict] = []
            for row in rows:
                cost, est = settle_cost_usd(
                    0.0,
                    model=row.model,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cache_tokens=row.cache_tokens,
                    cache_write_tokens=row.cache_write_tokens,
                )
                model = row.model or "unknown"
                if apply:
                    updates.append({"id": row.id, "cost": cost, "est": est})
                if cost is None:
                    unpriced[model] += 1
                else:
                    trials[model] += 1
                    dollars[model] += cost

            if updates:
                for start in range(0, len(updates), _UPDATE_CHUNK_SIZE):
                    await session.execute(
                        _UPDATE_COST, updates[start : start + _UPDATE_CHUNK_SIZE]
                    )

    if not total_rows:
        print("No zero-cost trials with token usage found.")
        return

    print(f"Zero-cost trials with token usage: {total_rows}")
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
        description="Re-price trials stored with cost_usd=0 despite token usage."
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
