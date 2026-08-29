from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import model_family_key, normalize_model_id
from oddish.db import (
    CostExcludedExperimentModel,
    CostExcludedLlmKeyModel,
    CostExcludedModelModel,
    TrialModel,
)
from oddish.db.pg_errors import is_missing_table

logger = logging.getLogger(__name__)

REASON_MODEL = "model"
REASON_EXPERIMENT = "experiment"
REASON_KEY = "key"


def canonical_excluded_model(model: str | None) -> str:
    return normalize_model_id(model) or ""


def _excluded_model_spend():
    trial_family = func.btrim(
        func.regexp_replace(
            func.regexp_replace(
                func.lower(
                    func.btrim(func.regexp_replace(TrialModel.model, "^.*/", ""))
                ),
                r"\s+",
                "-",
                "g",
            ),
            r"-{2,}",
            "-",
            "g",
        ),
        "-",
    )
    return (
        select(CostExcludedModelModel.id)
        .where(
            CostExcludedModelModel.model_name == trial_family,
            CostExcludedModelModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_model_filter():
    return ~_excluded_model_spend()


def _excluded_llm_key_spend():
    return (
        select(CostExcludedLlmKeyModel.id)
        .where(
            CostExcludedLlmKeyModel.key_hash == TrialModel.llm_key_hash,
            CostExcludedLlmKeyModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_llm_key_filter():
    return ~_excluded_llm_key_spend()


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
    return (
        _excluded_llm_key_spend()
        | _excluded_model_spend()
        | _excluded_experiment_spend()
    )


@dataclass(frozen=True)
class CostExclusions:
    llm_key_hashes: frozenset[str] = field(default_factory=frozenset)
    models: frozenset[str] = field(default_factory=frozenset)
    experiment_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "models",
            frozenset(filter(None, (model_family_key(model) for model in self.models))),
        )

    def reason_for(
        self,
        *,
        llm_key_hash: str | None = None,
        model: str | None = None,
        experiment_id: str | None = None,
    ) -> str | None:
        if llm_key_hash and llm_key_hash in self.llm_key_hashes:
            return REASON_KEY
        if model and model_family_key(model) in self.models:
            return REASON_MODEL
        if experiment_id and experiment_id in self.experiment_ids:
            return REASON_EXPERIMENT
        return None

    def excludes(
        self,
        *,
        llm_key_hash: str | None = None,
        model: str | None = None,
        experiment_id: str | None = None,
    ) -> bool:
        return (
            self.reason_for(
                llm_key_hash=llm_key_hash,
                model=model,
                experiment_id=experiment_id,
            )
            is not None
        )


async def load_cost_exclusions(session: AsyncSession) -> CostExclusions:
    is_autocommit = session.info.get("oddish_read_autocommit") is True
    # A missing optional table must not abort a caller's transaction, so normal
    # sessions keep the savepoint. get_read_session marks driver autocommit on
    # the session it owns; each SELECT already owns its transaction and
    # PostgreSQL rejects SAVEPOINT there.
    transaction_guard = nullcontext() if is_autocommit else session.begin_nested()
    try:
        async with transaction_guard:
            llm_keys = list(await session.scalars(select(CostExcludedLlmKeyModel)))
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
        llm_key_hashes=frozenset(row.key_hash for row in llm_keys),
        models=frozenset(row.model_name for row in models),
        experiment_ids=frozenset(row.experiment_id for row in experiments),
    )
