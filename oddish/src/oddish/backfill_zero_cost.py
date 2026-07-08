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
      AND (COALESCE(input_tokens, 0) > 0 OR COALESCE(output_tokens, 0) > 0)
    """
)

_UPDATE_COST = text("UPDATE trials SET cost_usd = :cost WHERE id = :id")


async def run_backfill(*, apply: bool) -> None:
    async with get_session() as session:
        rows = (await session.execute(_SELECT_ZERO_COST)).fetchall()
        if not rows:
            print("No zero-cost trials with token usage found.")
            return

        updates: list[dict] = []
        trials: Counter[str] = Counter()
        dollars: Counter[str] = Counter()
        skipped: Counter[str] = Counter()
        for row in rows:
            cost = settle_cost_usd(
                0.0,
                model=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_tokens=row.cache_tokens,
                cache_write_tokens=row.cache_write_tokens,
            )
            model = row.model or "unknown"
            if cost is None:
                skipped[model] += 1
                continue
            updates.append({"id": row.id, "cost": cost})
            trials[model] += 1
            dollars[model] += cost

        print(f"Zero-cost trials with token usage: {len(rows)}")
        for model, total in dollars.most_common():
            print(f"  {model}: {trials[model]} trials -> ${total:.2f}")
        for model, count in skipped.most_common():
            print(f"  {model}: {count} trials skipped (no pricing)")
        print(
            f"Total: {len(updates)} trials, "
            f"${sum(dollars.values()):.2f} estimated spend"
        )

        if not apply:
            print("\nDry run complete. Re-run with --apply to execute updates.")
            return

        if updates:
            await session.execute(_UPDATE_COST, updates)
        print(f"\nBackfill applied: {len(updates)} trials updated.")


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
