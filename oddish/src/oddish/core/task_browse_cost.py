from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from oddish.config import settings
from oddish.model_pricing import estimate_cost_usd


def resolve_browse_trial_cost(row: Mapping[str, Any]) -> tuple[float | None, bool]:
    """Return the card cost and whether it was token-estimated."""
    if row["cost_usd"] is not None:
        return float(row["cost_usd"]), False
    if (
        row["input_tokens"] is None
        and row["output_tokens"] is None
        and not row.get("cache_write_tokens")
    ):
        return None, False
    model = settings.normalize_trial_model(row["agent"], row["model"], strict=False)
    estimated = estimate_cost_usd(
        model or row["model"],
        row["input_tokens"],
        row["output_tokens"],
        row["cache_tokens"],
        row.get("cache_write_tokens"),
    )
    return (estimated, True) if estimated is not None else (None, False)
