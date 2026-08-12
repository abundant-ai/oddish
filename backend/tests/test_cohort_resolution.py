from api.services.cohort_comparison import MIN_COHORT, cohort_hash


def test_min_cohort_is_three():
    # Below three per side the comparison is anecdote.
    assert MIN_COHORT == 3


def test_cohort_hash_is_order_independent():
    assert cohort_hash(["b", "a"], ["d", "c"]) == cohort_hash(["a", "b"], ["c", "d"])


def test_cohort_hash_separates_the_two_sides():
    # Moving a trial from one cohort to the other must change the hash.
    assert cohort_hash(["a", "b"], ["c"]) != cohort_hash(["a"], ["b", "c"])


def test_cohort_hash_changes_when_a_trial_is_added():
    assert cohort_hash(["a"], ["b"]) != cohort_hash(["a", "z"], ["b"])
