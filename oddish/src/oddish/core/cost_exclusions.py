from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import CostExcludedExperimentModel, CostExcludedModelModel, TrialModel
from oddish.db.pg_errors import is_missing_table

logger = logging.getLogger(__name__)

REASON_MODEL = "model"
REASON_EXPERIMENT = "experiment"


def canonical_excluded_model(model: str | None) -> str:
    return normalize_model_id(model) or ""


def _excluded_model_spend():
    return (
        select(CostExcludedModelModel.id)
        .where(
            CostExcludedModelModel.model_name == TrialModel.model,
            CostExcludedModelModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_model_filter():
    return ~_excluded_model_spend()


def _excluded_experiment_spend():
    return (
        select(CostExcludedExperimentModel.id)
        .where(
            CostExcludedExperimentModel.experiment_id == TrialModel.experiment_id,
            CostExcludedExperimentModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_experiment_filter():
    return ~_excluded_experiment_spend()


def excluded_spend_filter():
    return _excluded_model_spend() | _excluded_experiment_spend()


@dataclass(frozen=True)
class CostExclusions:
    models: frozenset[str] = field(default_factory=frozenset)
    experiment_ids: frozenset[str] = field(default_factory=frozenset)

    def reason_for(
        self, *, model: str | None = None, experiment_id: str | None = None
    ) -> str | None:
        if model and model in self.models:
            return REASON_MODEL
        if experiment_id and experiment_id in self.experiment_ids:
            return REASON_EXPERIMENT
        return None

    def excludes(
        self, *, model: str | None = None, experiment_id: str | None = None
    ) -> bool:
        return self.reason_for(model=model, experiment_id=experiment_id) is not None


async def load_cost_exclusions(session: AsyncSession) -> CostExclusions:
    try:
        async with session.begin_nested():
            models = list(await session.scalars(select(CostExcludedModelModel)))
            experiments = list(
                await session.scalars(select(CostExcludedExperimentModel))
            )
    except ProgrammingError as exc:
        if not is_missing_table(exc):
            raise
        logger.warning(
            "cost exclusion lists unavailable (schema not migrated yet); "
            "spend is shown unlabelled",
            exc_info=True,
        )
        return CostExclusions()

    return CostExclusions(
        models=frozenset(row.model_name for row in models),
        experiment_ids=frozenset(row.experiment_id for row in experiments),
    )
