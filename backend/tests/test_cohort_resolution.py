from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.agent_capabilities import (
    FAILURE_CLASS,
    MIN_COHORT,
    SUCCESS_CLASS,
    _cohort_for_trial,
    cohort_hash,
    resolve_cohort_membership,
    resolve_cohorts,
)


def test_one_trajectory_is_enough():
    assert MIN_COHORT == 1


def test_every_qa_classification_remains_analyzable():
    assert _cohort_for_trial("GOOD_SUCCESS", 0) == SUCCESS_CLASS
    assert _cohort_for_trial("BAD_SUCCESS", 0) == SUCCESS_CLASS
    assert _cohort_for_trial("GOOD_FAILURE", 1) == FAILURE_CLASS
    assert _cohort_for_trial("BAD_FAILURE", 1) == FAILURE_CLASS
    assert _cohort_for_trial("HARNESS_ERROR", 1) == FAILURE_CLASS


def test_verifier_reward_supplies_outcome_without_qa():
    assert _cohort_for_trial(None, 1) == SUCCESS_CLASS
    assert _cohort_for_trial(None, 0) == FAILURE_CLASS
    assert _cohort_for_trial(None, None) == FAILURE_CLASS


def test_cohort_hash_is_order_independent():
    assert cohort_hash(["b", "a"], ["d", "c"]) == cohort_hash(["a", "b"], ["c", "d"])


def test_cohort_hash_separates_the_two_sides():
    # Moving a trial from one cohort to the other must change the hash.
    assert cohort_hash(["a", "b"], ["c"]) != cohort_hash(["a"], ["b", "c"])


def test_cohort_hash_changes_when_a_trial_is_added():
    assert cohort_hash(["a"], ["b"]) != cohort_hash(["a", "z"], ["b"])


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", [resolve_cohorts, resolve_cohort_membership])
async def test_public_cohort_queries_are_scoped_to_the_shared_experiment(resolver):
    result = MagicMock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    await resolver(session, "version-1", "experiment-1")

    statement = session.execute.await_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "experiment-1" in compiled
    assert "experiment_trials" in compiled
