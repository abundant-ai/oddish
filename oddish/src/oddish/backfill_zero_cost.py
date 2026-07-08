from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import get_session
from oddish.model_pricing import settle_cost_usd


async def _load_zero_cost_rows(session: AsyncSession) -> list:
    # No deleted_at filter: quota spend sums include soft-deleted trials
    # (``_sum_settled_cost_usd`` runs with include_deleted=True), so their
    # cost must be repaired too.
    rows = await session.execute(
        text(
            """
            SELECT id, model, input_tokens, output_tokens,
                   cache_tokens, cache_write_tokens
            FROM trials
            WHERE cost_usd = 0
              AND (COALESCE(input_tokens, 0) > 0 OR COALESCE(output_tokens, 0) > 0)
            """
        )
    )
    return rows.fetchall()


async def run_backfill(*, apply: bool) -> None:
    async with get_session() as session:
        rows = await _load_zero_cost_rows(session)
        if not rows:
            print("No zero-cost trials with token usage found.")
            return

        updates: list[dict] = []
        updated_by_model: Counter[str] = Counter()
        dollars_by_model: Counter[str] = Counter()
        unpriced_by_model: Counter[str] = Counter()
        for row in rows:
            settled = settle_cost_usd(
                0.0,
                model=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_tokens=row.cache_tokens,
                cache_write_tokens=row.cache_write_tokens,
            )
            model = row.model or "unknown"
            if settled is None:
                unpriced_by_model[model] += 1
                continue
            updates.append({"id": row.id, "cost": settled})
            updated_by_model[model] += 1
            dollars_by_model[model] += settled

        print(f"Zero-cost trials with token usage: {len(rows)}")
        for model, dollars in dollars_by_model.most_common():
            print(f"  {model}: {updated_by_model[model]} trials -> ${dollars:.2f}")
        for model, count in unpriced_by_model.most_common():
            print(f"  {model}: {count} trials skipped (no pricing)")
        print(
            f"Total: {len(updates)} trials, "
            f"${sum(dollars_by_model.values()):.2f} estimated spend"
        )

        if not apply:
            print("\nDry run complete. Re-run with --apply to execute updates.")
            return

        if updates:
            await session.execute(
                text("UPDATE trials SET cost_usd = :cost WHERE id = :id"),
                updates,
            )
        print(f"\nBackfill applied: {len(updates)} trials updated.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-price trials stored with cost_usd=0 despite token usage "
            "(Claude Code reports $0 for GLM/MiniMax/Kimi/Fireworks passthroughs)."
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
