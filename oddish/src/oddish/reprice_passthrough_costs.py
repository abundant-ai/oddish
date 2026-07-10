from __future__ import annotations

import argparse
import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import DateTime, Integer, String, bindparam, text

from oddish.config import settings
from oddish.db import get_session
from oddish.model_pricing import (
    is_native_cost_trusted,
    settle_cost_usd,
)

_SELECT_CANDIDATES = text(
    """
    SELECT t.id, t.agent, t.provider, t.model, t.cost_usd,
           t.input_tokens, t.output_tokens, t.cache_tokens,
           t.cache_write_tokens
    FROM trials AS t
    WHERE t.deleted_at IS NULL
      AND t.finished_at >= :since
      AND LOWER(t.agent) LIKE '%claude-code%'
      AND t.origin::text = 'oddish'
      AND (
        t.idempotency_key IS NULL
        OR t.idempotency_key NOT LIKE 'combine:%'
      )
      AND (t.billed_user_id IS NOT NULL OR t.is_probe IS FALSE)
      AND (
        COALESCE(t.input_tokens, 0) > 0
        OR COALESCE(t.output_tokens, 0) > 0
        OR COALESCE(t.cache_write_tokens, 0) > 0
      )
      AND (:model IS NULL OR t.model = :model)
      AND t.id > :after
    ORDER BY t.id
    LIMIT :page
    """
).bindparams(
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("model", type_=String()),
    bindparam("after", type_=String()),
    bindparam("page", type_=Integer()),
)

_UPDATE_COST = text(
    """
    UPDATE trials
    SET cost_usd = :new_cost
    WHERE id = :id
      AND cost_usd IS NOT DISTINCT FROM :old_cost
      AND deleted_at IS NULL
    """
)

_PAGE_SIZE = 5000
_UPDATE_CHUNK_SIZE = 500

SkipReason = Literal["trusted-native-cost", "unpriceable", "unchanged"]


@dataclass(frozen=True)
class RepricePlan:
    estimated_cost: float | None
    skip_reason: SkipReason | None
    clear_to_null: bool = False

    @property
    def should_update(self) -> bool:
        return self.skip_reason is None


@dataclass
class GroupSummary:
    row_count: int = 0
    old_recorded_total: float = 0.0
    new_effective_total: float = 0.0
    unpriceable_count: int = 0
    skipped_count: int = 0
    planned_update_count: int = 0


def parse_since(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise ValueError("since must not be empty")
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _plan_reprice(
    *,
    agent: str | None,
    provider: str | None,
    model: str | None,
    existing_cost: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_tokens: int | None,
    cache_write_tokens: int | None,
) -> RepricePlan:
    if is_native_cost_trusted(agent=agent, provider=provider):
        return RepricePlan(None, "trusted-native-cost")

    estimated_cost = settle_cost_usd(
        existing_cost,
        native_cost_trusted=False,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    if estimated_cost is None:
        if existing_cost == 0.0:
            # Preserve backfill_zero_cost semantics: a harness's fake $0 must
            # not masquerade as authoritative. NULL keeps the row on the
            # explicit unpriced path until a rate becomes available.
            return RepricePlan(None, None, clear_to_null=True)
        return RepricePlan(None, "unpriceable")
    if existing_cost == estimated_cost:
        return RepricePlan(estimated_cost, "unchanged")
    return RepricePlan(estimated_cost, None)


def _finite_cost_or_zero(value: float | None) -> float:
    return float(value) if value is not None and math.isfinite(value) else 0.0


async def run_reprice(
    *,
    since: str,
    apply: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    since_datetime = parse_since(since)
    model = model.strip() if model and model.strip() else None
    provider = provider.strip().lower() if provider and provider.strip() else None

    summaries: dict[tuple[str, str], GroupSummary] = {}
    after = ""
    matched_rows = 0
    attempted_updates = 0
    applied_updates = 0
    applied_count_known = True

    while True:
        async with get_session() as session:
            rows = (
                await session.execute(
                    _SELECT_CANDIDATES,
                    {
                        "since": since_datetime,
                        "model": model,
                        "after": after,
                        "page": _PAGE_SIZE,
                    },
                )
            ).fetchall()
            if not rows:
                break
            after = rows[-1].id

            updates: list[dict[str, object]] = []
            for row in rows:
                resolved_provider = settings.get_provider_for_trial(
                    row.agent or "", row.model
                )
                if provider and resolved_provider.lower() != provider:
                    continue
                plan = _plan_reprice(
                    agent=row.agent,
                    provider=resolved_provider,
                    model=row.model,
                    existing_cost=row.cost_usd,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cache_tokens=row.cache_tokens,
                    cache_write_tokens=row.cache_write_tokens,
                )
                if plan.skip_reason == "trusted-native-cost":
                    continue

                matched_rows += 1
                group_key = (
                    resolved_provider.strip().lower() or "unknown",
                    row.model or "unknown",
                )
                summary = summaries.setdefault(group_key, GroupSummary())
                old_cost = _finite_cost_or_zero(row.cost_usd)
                summary.row_count += 1
                summary.old_recorded_total += old_cost

                if plan.clear_to_null:
                    summary.unpriceable_count += 1
                    summary.planned_update_count += 1
                    updates.append(
                        {
                            "id": row.id,
                            "old_cost": row.cost_usd,
                            "new_cost": None,
                        }
                    )
                    continue

                if plan.skip_reason == "unpriceable":
                    summary.unpriceable_count += 1
                    summary.skipped_count += 1
                    summary.new_effective_total += old_cost
                    continue

                estimated_cost = float(plan.estimated_cost)
                summary.new_effective_total += estimated_cost
                if plan.skip_reason == "unchanged":
                    summary.skipped_count += 1
                    continue

                summary.planned_update_count += 1
                updates.append(
                    {
                        "id": row.id,
                        "old_cost": row.cost_usd,
                        "new_cost": estimated_cost,
                    }
                )

            attempted_updates += len(updates)
            if apply:
                for start in range(0, len(updates), _UPDATE_CHUNK_SIZE):
                    result = await session.execute(
                        _UPDATE_COST, updates[start : start + _UPDATE_CHUNK_SIZE]
                    )
                    rowcount = getattr(result, "rowcount", None)
                    if rowcount is None or rowcount < 0:
                        applied_count_known = False
                    else:
                        applied_updates += rowcount

    if not matched_rows:
        print("No first-party Claude-Code passthrough trials with token usage found.")
        return

    mode = "APPLY" if apply else "DRY RUN"
    print(f"Claude-Code passthrough historical reprice ({mode})")
    print(f"Window: finished_at >= {since_datetime.isoformat()}")
    if provider:
        print(f"Provider filter: {provider}")
    if model:
        print(f"Model filter: {model}")
    print()
    for (group_provider, group_model), summary in sorted(summaries.items()):
        delta = summary.new_effective_total - summary.old_recorded_total
        print(
            f"  {group_provider} / {group_model}: "
            f"rows={summary.row_count:,}, "
            f"old=${summary.old_recorded_total:,.2f}, "
            f"new=${summary.new_effective_total:,.2f}, "
            f"delta=${delta:,.2f}, "
            f"unpriceable={summary.unpriceable_count:,}, "
            f"skipped={summary.skipped_count:,}, "
            f"planned_updates={summary.planned_update_count:,}"
        )

    old_total = sum(summary.old_recorded_total for summary in summaries.values())
    new_total = sum(summary.new_effective_total for summary in summaries.values())
    unpriceable_total = sum(summary.unpriceable_count for summary in summaries.values())
    skipped_total = sum(summary.skipped_count for summary in summaries.values())
    print()
    print(
        f"Total: rows={matched_rows:,}, old=${old_total:,.2f}, "
        f"new=${new_total:,.2f}, delta=${new_total - old_total:,.2f}, "
        f"unpriceable={unpriceable_total:,}, skipped={skipped_total:,}, "
        f"planned_updates={attempted_updates:,}"
    )
    if apply:
        if applied_count_known:
            concurrency_skips = attempted_updates - applied_updates
            print(
                f"Applied updates: {applied_updates:,}; "
                f"concurrency-guard skips: {concurrency_skips:,}."
            )
        else:
            print(
                "Applied update count unavailable from the database driver; "
                "all writes used the old-cost concurrency guard."
            )
    else:
        print("Dry run complete. Re-run with --apply to execute guarded updates.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-price historical first-party Claude-Code passthrough trial costs."
        )
    )
    parser.add_argument(
        "--since",
        required=True,
        help="Earliest finished_at date/timestamp (ISO 8601; naive values are UTC).",
    )
    parser.add_argument(
        "--model",
        help="Only re-price this exact stored model id.",
    )
    parser.add_argument(
        "--provider",
        help="Only re-price this provider (case-insensitive).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute updates. Without this flag, runs in read-only dry-run mode.",
    )
    args = parser.parse_args()
    asyncio.run(
        run_reprice(
            since=args.since,
            apply=args.apply,
            model=args.model,
            provider=args.provider,
        )
    )


if __name__ == "__main__":
    main()
