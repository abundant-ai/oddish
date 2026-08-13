import pytest

from api.services.cohort_metrics import (
    CHART_CATEGORIES,
    cited_trials,
    delta,
)


def test_discovery_is_not_a_chart_category():
    assert "behavior_discovery" not in CHART_CATEGORIES
    assert len(CHART_CATEGORIES) == 6


def test_repeated_trial_id_counts_once():
    category = {
        "successful": [
            {"evidence": [{"trial_id": "t1"}, {"trial_id": "t1"}, {"trial_id": "t2"}]},
            {"evidence": [{"trial_id": "t1"}]},
        ],
        "failing": [],
    }
    assert cited_trials(category, "successful") == {"t1", "t2"}


def test_sides_are_read_independently():
    category = {
        "successful": [{"evidence": [{"trial_id": "s1"}]}],
        "failing": [{"evidence": [{"trial_id": "f1"}]}],
    }
    assert cited_trials(category, "failing") == {"f1"}


def test_missing_and_malformed_evidence_is_skipped():
    category = {"successful": [{"evidence": None}, {}, {"evidence": [{}]}]}
    assert cited_trials(category, "successful") == set()


def test_delta_is_relative_to_the_models_own_baseline():
    # 5 of 6 cited trials were successes, against a model that succeeds 80%
    assert delta(5, 1, 0.80) == pytest.approx(0.0333, abs=1e-4)
    # the same ratio from a model that succeeds 25% is a real strength
    assert delta(2, 1, 0.25) == pytest.approx(0.4167, abs=1e-4)


def test_no_evidence_is_none_not_zero():
    assert delta(0, 0, 0.5) is None


def test_a_one_sided_cohort_has_no_delta():
    """A model that never failed (or never succeeded) has nothing to compare.

    Its citations can only come from the one side it has, so the ratio equals
    the baseline exactly and every category returns 0.00 -- which the radar
    draws as a closed shape resting on the baseline ring, the picture of a
    model that is average at everything. Seen on real data: a gemini variant
    with a 0-success / 9-failure cohort scored exactly 0.0 on all five axes it
    had evidence for.
    """
    # 9 failures, no successes: baseline 0, and every cited trial is a failure.
    assert delta(0, 4, 0.0, comparable=False) is None
    # The mirror: 6 successes, no failures.
    assert delta(3, 0, 1.0, comparable=False) is None
    # Without the guard both of these are a confident zero.
    assert delta(0, 4, 0.0) == 0.0
    assert delta(3, 0, 1.0) == 0.0
