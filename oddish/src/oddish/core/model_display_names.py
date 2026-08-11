"""Operator model aliases, applied to published share responses.

``apply_model_display_names`` rewrites already-built ``TrialResponse``
objects, after ``build_trial_response`` resolved cost from the real model
id. Nothing may run it earlier or feed an alias into ``normalize_trial_model``
or a pricing lookup -- those key off ``trials.model`` and would mis-price.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import ModelDisplayNameModel
from oddish.db.pg_errors import is_missing_table
from oddish.schemas import TrialResponse

logger = logging.getLogger(__name__)


def _lookup_keys(model: str | None) -> list[str]:
    if not model:
        return []
    keys = (model.strip().lower(), normalize_model_id(model))
    return list(dict.fromkeys(key for key in keys if key))


def canonical_model_key(model: str) -> str:
    """The single spelling an alias is stored under.

    Writers canonicalize through this so the live UNIQUE index collapses case
    and whitespace variants into one row; ``_lookup_keys`` covers the same
    ground from the read side, so the two always meet.
    """
    keys = _lookup_keys(model)
    return keys[-1] if keys else ""


async def load_model_display_names(session: AsyncSession) -> dict[str, str]:
    try:
        # Savepoint so a missing table leaves the caller's transaction usable;
        # public share reads run several more queries after this one.
        async with session.begin_nested():
            rows = list(await session.scalars(select(ModelDisplayNameModel)))
    except ProgrammingError as exc:
        # Only a MISSING table degrades -- any other SQL fault must surface.
        # Share pages predate this feature, so during the deploy-before-migrate
        # window they render real model ids instead of 500ing.
        if not is_missing_table(exc):
            raise
        logger.warning(
            "model display names unavailable (schema not migrated yet); "
            "published pages show real model ids",
            exc_info=True,
        )
        return {}
    names: dict[str, str] = {}
    for row in rows:
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
