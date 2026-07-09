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


def trial_is_expensive(
    row: Mapping[str, Any],
    minimum_usd: float,
    average_multiplier: float,
) -> bool:
    spend_usd = float(row.get("cost_usd") or 0)
    average_usd = row.get("other_trial_average_usd")
    if spend_usd <= minimum_usd:
        return False
    return average_usd is None or spend_usd > float(average_usd) * average_multiplier


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


def expensive_trial_alert(
    row: Mapping[str, Any],
    minimum_usd: float,
    average_multiplier: float,
    dashboard_url: str,
) -> tuple[str, str]:
    trial_id = str(row["id"])
    task_id = str(row["task_id"])
    owner = _escape(str(row.get("spender_label") or row.get("spender") or "Unknown"))
    spend_usd = float(row.get("cost_usd") or 0)
    average_usd = row.get("other_trial_average_usd")
    if average_usd is None:
        comparison = "first priced trial"
    elif float(average_usd) == 0:
        comparison = "the experiment's other trials average $0"
    else:
        comparison = (
            f"{spend_usd / float(average_usd):.1f}× the experiment's "
            f"${float(average_usd):,.2f} average"
        )
    url = f"{dashboard_url.rstrip('/')}/tasks/{quote(task_id, safe='')}"
    key = f"expensive_trial:{trial_id}:{minimum_usd:g}:{average_multiplier:g}"
    text = (
        f":warning: *Expensive trial:* `{_escape(trial_id)}` cost *${spend_usd:,.2f}* — "
        f"{comparison} · owner: *{owner}* · <{url}|open task>"
    )
    return key, text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
