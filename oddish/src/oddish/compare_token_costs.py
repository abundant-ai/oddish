"""Read-only comparison of recorded trial cost versus a token-rate estimate."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from oddish.db import get_session


_COST_COMPARISON = text(
    """
    SELECT
        COUNT(*) AS trial_count,
        COUNT(*) FILTER (WHERE t.cost_usd IS NOT NULL) AS recorded_cost_trials,
        COUNT(*) FILTER (WHERE t.cost_usd = 0) AS zero_cost_trials,
        COUNT(*) FILTER (WHERE t.cost_usd IS NULL) AS null_cost_trials,
        COALESCE(SUM(t.cost_usd), 0) AS recorded_cost_usd,
        COALESCE(SUM(GREATEST(COALESCE(t.input_tokens, 0), 0)), 0)
            AS input_tokens,
        COALESCE(SUM(GREATEST(COALESCE(t.cache_tokens, 0), 0)), 0)
            AS cache_tokens,
        COALESCE(SUM(GREATEST(COALESCE(t.cache_write_tokens, 0), 0)), 0)
            AS cache_write_tokens,
        COALESCE(SUM(GREATEST(COALESCE(t.output_tokens, 0), 0)), 0)
            AS output_tokens,
        COALESCE(
            SUM(
                GREATEST(
                    GREATEST(COALESCE(t.input_tokens, 0), 0)
                    - GREATEST(COALESCE(t.cache_tokens, 0), 0)
                    - GREATEST(COALESCE(t.cache_write_tokens, 0), 0),
                    0
                )
            ),
            0
        ) AS uncached_input_tokens
    FROM trials AS t
    JOIN experiments AS e ON e.id = t.experiment_id
    WHERE t.model = :model
      AND t.finished_at >= :since
      AND t.finished_at < :until
      AND t.finished_at IS NOT NULL
      AND t.deleted_at IS NULL
      AND e.deleted_at IS NULL
      AND (
          t.billed_user_id IS NOT NULL
          OR (
              t.origin::text = 'oddish'
              AND t.is_probe IS FALSE
              AND (
                  t.idempotency_key IS NULL
                  OR t.idempotency_key NOT LIKE 'combine:%'
              )
          )
      )
    """
)


@dataclass(frozen=True)
class TokenRates:
    """USD per million tokens."""

    input: float
    cache_read: float
    output: float
    cache_write: float


@dataclass(frozen=True)
class TokenCostEstimate:
    uncached_input_usd: float
    cache_read_usd: float
    cache_write_usd: float
    output_usd: float

    @property
    def total_usd(self) -> float:
        return (
            self.uncached_input_usd
            + self.cache_read_usd
            + self.cache_write_usd
            + self.output_usd
        )


def parse_utc_datetime(value: str) -> datetime:
    """Parse an ISO date/timestamp, treating a missing timezone as UTC."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("since must not be empty")
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def estimate_token_cost(
    *,
    uncached_input_tokens: int,
    cache_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    rates: TokenRates,
) -> TokenCostEstimate:
    """Price aggregate token classes using a caller-supplied rate card."""
    per_million = 1_000_000
    return TokenCostEstimate(
        uncached_input_usd=max(uncached_input_tokens, 0) * rates.input / per_million,
        cache_read_usd=max(cache_tokens, 0) * rates.cache_read / per_million,
        cache_write_usd=max(cache_write_tokens, 0) * rates.cache_write / per_million,
        output_usd=max(output_tokens, 0) * rates.output / per_million,
    )


def _format_int(value: int) -> str:
    return f"{value:,}"


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


async def run_comparison(
    *,
    since: str,
    model: str,
    rates: TokenRates,
) -> None:
    """Query production-shaped data and print a read-only reconciliation."""
    since_dt = parse_utc_datetime(since)
    until_dt = datetime.now(timezone.utc)
    if since_dt >= until_dt:
        raise ValueError("since must be earlier than the current time")

    async with get_session() as session:
        row = (
            await session.execute(
                _COST_COMPARISON,
                {"model": model, "since": since_dt, "until": until_dt},
            )
        ).one()

    trial_count = int(row.trial_count or 0)
    input_tokens = int(row.input_tokens or 0)
    cache_tokens = int(row.cache_tokens or 0)
    cache_write_tokens = int(row.cache_write_tokens or 0)
    output_tokens = int(row.output_tokens or 0)
    uncached_input_tokens = int(row.uncached_input_tokens or 0)
    recorded_cost = float(row.recorded_cost_usd or 0.0)
    estimate = estimate_token_cost(
        uncached_input_tokens=uncached_input_tokens,
        cache_tokens=cache_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        rates=rates,
    )

    print("Token cost comparison (read-only)")
    print(f"Model: {model}")
    print(f"Window: {since_dt.isoformat()} <= finished_at < {until_dt.isoformat()}")
    print(
        "Rates ($/1M): "
        f"input={rates.input:g}, cache_read={rates.cache_read:g}, "
        f"cache_write={rates.cache_write:g}, output={rates.output:g}"
    )
    print()
    print("Trials")
    print(f"  total: {_format_int(trial_count)}")
    print(
        "  non-NULL / zero / NULL cost: "
        f"{_format_int(int(row.recorded_cost_trials or 0))} / "
        f"{_format_int(int(row.zero_cost_trials or 0))} / "
        f"{_format_int(int(row.null_cost_trials or 0))}"
    )
    print()
    print("Tokens")
    print(f"  input total: {_format_int(input_tokens)}")
    print(f"  uncached input: {_format_int(uncached_input_tokens)}")
    print(f"  cache read: {_format_int(cache_tokens)}")
    print(f"  cache write: {_format_int(cache_write_tokens)}")
    print(f"  output: {_format_int(output_tokens)}")
    print()
    print("Cost")
    print(f"  recorded cost_usd: {_format_usd(recorded_cost)}")
    print(f"  estimated uncached input: {_format_usd(estimate.uncached_input_usd)}")
    print(f"  estimated cache read: {_format_usd(estimate.cache_read_usd)}")
    print(f"  estimated cache write: {_format_usd(estimate.cache_write_usd)}")
    print(f"  estimated output: {_format_usd(estimate.output_usd)}")
    print(f"  estimated total: {_format_usd(estimate.total_usd)}")
    print(f"  recorded - estimated: {_format_usd(recorded_cost - estimate.total_usd)}")
    ratio = recorded_cost / estimate.total_usd if estimate.total_usd else None
    print(
        f"  recorded / estimated: {ratio:.3f}x"
        if ratio is not None
        else "  recorded / estimated: n/a"
    )


def run(**kwargs) -> None:
    """Synchronous entrypoint for wrappers."""
    asyncio.run(run_comparison(**kwargs))
