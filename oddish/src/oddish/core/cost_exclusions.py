from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.cache import TTLCache
from oddish.config import model_family_key, normalize_model_id, settings
from oddish.db import (
    CostExcludedExperimentModel,
    CostExcludedLlmKeyModel,
    CostExcludedModelModel,
    TrialModel,
)
from oddish.db.optional_read import read_optional_table

logger = logging.getLogger(__name__)


REASON_MODEL = "model"
REASON_EXPERIMENT = "experiment"
REASON_KEY = "key"


def canonical_excluded_model(model: str | None) -> str:
    return normalize_model_id(model) or ""


def _excluded_model_spend(trial=TrialModel):
    trial_family = func.btrim(
        func.regexp_replace(
            func.regexp_replace(
                func.lower(func.btrim(func.regexp_replace(trial.model, "^.*/", ""))),
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
        .correlate(trial)
        .exists()
    )


def not_excluded_model_filter():
    return ~_excluded_model_spend()


def _excluded_llm_key_spend(trial=TrialModel):
    return (
        select(CostExcludedLlmKeyModel.id)
        .where(
            CostExcludedLlmKeyModel.key_hash == trial.llm_key_hash,
            CostExcludedLlmKeyModel.deleted_at.is_(None),
        )
        .correlate(trial)
        .exists()
    )


def not_excluded_llm_key_filter():
    return ~_excluded_llm_key_spend()


def _excluded_experiment_spend(trial=TrialModel):
    return (
        select(CostExcludedExperimentModel.id)
        .where(
            CostExcludedExperimentModel.experiment_id == trial.experiment_id,
            CostExcludedExperimentModel.deleted_at.is_(None),
        )
        .correlate(trial)
        .exists()
    )


def not_excluded_experiment_filter():
    return ~_excluded_experiment_spend()


def excluded_spend_filter(trial=TrialModel):
    """Spend on an admin-excluded key, model, or experiment. ``trial`` is the
    ``TrialModel`` entity (or alias) the caller selects from."""
    return (
        _excluded_llm_key_spend(trial)
        | _excluded_model_spend(trial)
        | _excluded_experiment_spend(trial)
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


# The three lists change a few times a month from the admin page and are read
# on every task, trial and experiment fetch. One entry per container, refreshed
# at most once per ``settings.cost_exclusions_cache_seconds``, keeps the
# combined statement (plus the SAVEPOINT around it) off the request path. The
# admin routers call ``invalidate_cost_exclusions`` after each edit so the
# container that served the edit answers fresh; the TTL bounds the lag on
# every other container.
_cache: TTLCache[str, CostExclusions] = TTLCache(0.0, max_size=1)
_CACHE_KEY = "all"


def invalidate_cost_exclusions() -> None:
    _cache.clear()


async def _read_cost_exclusions(session: AsyncSession) -> CostExclusions:
    # UNION ALL appends the lists without multiplying rows across categories.
    # Explicit live predicates also cover compound-select execution paths.
    rows = await session.execute(
        union_all(
            select(literal(REASON_KEY), CostExcludedLlmKeyModel.key_hash).where(
                CostExcludedLlmKeyModel.deleted_at.is_(None)
            ),
            select(literal(REASON_MODEL), CostExcludedModelModel.model_name).where(
                CostExcludedModelModel.deleted_at.is_(None)
            ),
            select(
                literal(REASON_EXPERIMENT), CostExcludedExperimentModel.experiment_id
            ).where(CostExcludedExperimentModel.deleted_at.is_(None)),
        )
    )
    values: dict[str, set[str]] = {
        REASON_KEY: set(),
        REASON_MODEL: set(),
        REASON_EXPERIMENT: set(),
    }
    for reason, value in rows:
        values[reason].add(value)
    return CostExclusions(
        llm_key_hashes=frozenset(values[REASON_KEY]),
        models=frozenset(values[REASON_MODEL]),
        experiment_ids=frozenset(values[REASON_EXPERIMENT]),
    )


async def load_cost_exclusions(session: AsyncSession) -> CostExclusions:
    ttl = settings.cost_exclusions_cache_seconds
    if ttl > 0 and (cached := _cache.get(_CACHE_KEY)) is not None:
        return cached
    exclusions = await read_optional_table(
        session,
        lambda: _read_cost_exclusions(session),
        table="cost exclusion lists",
        degraded="spend is shown unlabelled",
    )
    if exclusions is None:
        # Not cached: the lists come back the moment the migration lands.
        return CostExclusions()
    _cache.set(_CACHE_KEY, exclusions, ttl_seconds=ttl)
    return exclusions
