"""Operator-managed model aliases, applied to published share responses.

The alias is a presentation concern and lives at the very end of the read
path: :func:`apply_model_display_names` rewrites already-built
``TrialResponse`` objects, after ``build_trial_response`` has resolved cost
from the real model id. Nothing here may run before that, and nothing may
feed an alias back into ``normalize_trial_model`` or a pricing lookup --
those key off ``trials.model`` and would silently mis-price.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import ModelDisplayNameModel
from oddish.schemas import TrialResponse


def _lookup_keys(model: str | None) -> list[str]:
    """Spellings a model may be stored or displayed under, most literal first.

    Admins type whatever they see, which is usually the normalized id the
    dashboard renders but may be the raw ``trials.model`` value.
    """
    if not model:
        return []
    keys = []
    raw = model.strip().lower()
    if raw:
        keys.append(raw)
    normalized = normalize_model_id(model)
    if normalized and normalized not in keys:
        keys.append(normalized)
    return keys


async def load_model_display_names(session: AsyncSession) -> dict[str, str]:
    # Whole-entity select so the soft-delete loader criteria applies and
    # removed aliases stop renaming anything.
    result = await session.execute(select(ModelDisplayNameModel))
    names: dict[str, str] = {}
    for row in result.scalars().all():
        for key in _lookup_keys(row.model_name):
            names.setdefault(key, row.display_name)
    return names


def apply_model_display_names(
    trials: Iterable[TrialResponse], names: dict[str, str]
) -> None:
    """Swap aliased model ids for their presentation names, in place."""
    if not names:
        return
    for trial in trials:
        keys = _lookup_keys(trial.model)
        display = next((names[key] for key in keys if key in names), None)
        if display is None:
            continue
        # Queue keys are model-derived and rendered publicly, so a stale one
        # would sit next to the alias and give the real id away. Only the
        # whole key or its trailing segment is rewritten -- anything looser
        # would corrupt keys that merely contain the model as a substring.
        queue_key = trial.queue_key or ""
        if queue_key.strip().lower() in keys:
            trial.queue_key = display
        else:
            prefix, sep, last = queue_key.rpartition("/")
            if sep and last.strip().lower() in keys:
                trial.queue_key = f"{prefix}{sep}{display}"
        trial.model = display
