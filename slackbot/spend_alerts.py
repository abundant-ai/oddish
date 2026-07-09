from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote


def environment_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def experiment_is_expensive(row: Mapping[str, Any], threshold_usd: float) -> bool:
    return float(row.get("cost_usd") or 0) >= threshold_usd


def user_is_expensive(
    row: Mapping[str, Any],
    minimum_usd: float,
    average_multiplier: float,
) -> bool:
    spend_usd = float(row.get("cost_usd") or 0)
    average_usd = float(row.get("average_cost_usd") or 0)
    return (
        average_usd > 0
        and spend_usd >= minimum_usd
        and spend_usd >= average_usd * average_multiplier
    )


def expensive_experiment_alert(
    row: Mapping[str, Any],
    threshold_usd: float,
    dashboard_url: str,
) -> tuple[str, str]:
    experiment_id = str(row["experiment_id"])
    name = _escape(str(row.get("name") or experiment_id))
    owner = _escape(str(row.get("owner_label") or row.get("owner_user_id") or "Unknown"))
    cost_usd = float(row.get("cost_usd") or 0)
    active_trials = int(row.get("active_trials") or 0)
    phase = f"{active_trials} trial{'s' if active_trials != 1 else ''} still running" if active_trials else "finished"
    url = f"{dashboard_url.rstrip('/')}/experiments/{quote(quote(experiment_id, safe=''), safe='')}"
    key = f"expensive_experiment:{experiment_id}:{threshold_usd:g}"
    text = (
        f":money_with_wings: *Expensive experiment:* *{name}* has spent "
        f"*${cost_usd:,.2f}* (alert at ${threshold_usd:,.0f}) — {phase} · "
        f"owner: *{owner}* · <{url}|open experiment>"
    )
    return key, text


def expensive_user_alert(
    row: Mapping[str, Any],
    minimum_usd: float,
    average_multiplier: float,
    dashboard_url: str,
) -> tuple[str, str]:
    org_id = str(row["org_id"])
    spender = str(row["spender"])
    label = _escape(str(row.get("spender_label") or spender))
    spend_usd = float(row.get("cost_usd") or 0)
    average_usd = float(row.get("average_cost_usd") or 0)
    multiple = spend_usd / average_usd if average_usd > 0 else 0
    url = f"{dashboard_url.rstrip('/')}/admin"
    key = f"expensive_user:{org_id}:{spender}:{minimum_usd:g}:{average_multiplier:g}"
    text = (
        f":moneybag: *High user spend:* *{label}* spent *${spend_usd:,.2f}* "
        f"in the trailing 7 days — *{multiple:.1f}×* the org spender average "
        f"(${average_usd:,.2f}); alerts require ${minimum_usd:,.0f}+ and "
        f"{average_multiplier:.1f}×+ average. · <{url}|open admin costs>"
    )
    return key, text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
