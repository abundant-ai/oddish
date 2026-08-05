"""Operator model aliases, applied to published share responses.

``apply_model_display_names`` rewrites already-built ``TrialResponse``
objects, after ``build_trial_response`` resolved cost from the real model
id. Nothing may run it earlier or feed an alias into ``normalize_trial_model``
or a pricing lookup -- those key off ``trials.model`` and would mis-price.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import ModelDisplayNameModel
from oddish.schemas import TrialResponse


def _lookup_keys(model: str | None) -> list[str]:
    if not model:
        return []
    keys = (model.strip().lower(), normalize_model_id(model))
    return list(dict.fromkeys(key for key in keys if key))


async def load_model_display_names(session: AsyncSession) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in await session.scalars(select(ModelDisplayNameModel)):
        for key in _lookup_keys(row.model_name):
            names.setdefault(key, row.display_name)
    return names


def apply_model_display_names(
    trials: Iterable[TrialResponse], names: dict[str, str]
) -> None:
    if not names:
        return
    for trial in trials:
        keys = _lookup_keys(trial.model)
        display = next((names[key] for key in keys if key in names), None)
        if display is None:
            continue
        queue_key = trial.queue_key or ""
        prefix, sep, last = queue_key.rpartition("/")
        if queue_key.strip().lower() in keys:
            trial.queue_key = display
        elif sep and last.strip().lower() in keys:
            trial.queue_key = f"{prefix}{sep}{display}"
        trial.model = display
