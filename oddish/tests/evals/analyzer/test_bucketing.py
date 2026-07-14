from oddish.evals.primitives import SubAnalysis
from oddish.evals.analyzer.bucketing import (
    BUCKET_OF, SUBCATEGORY_OF, bucket_subanalyses,
)


def _sa(cls_, subtype):
    return SubAnalysis(
        trial_id="t", trajectory_link="/x", classification=cls_, subtype=subtype,
        evidence="e", root_cause="rc", recommendation="rec",
    )


def test_bucket_of_rules():
    assert BUCKET_OF["BAD_SUCCESS"] == "bad"
    assert BUCKET_OF["BAD_FAILURE"] == "bad"
    assert BUCKET_OF["GOOD_FAILURE"] == "good"
    assert BUCKET_OF["GOOD_SUCCESS"] == "other"
    assert BUCKET_OF["HARNESS_ERROR"] == "other"


def test_subcategory_seed_mapping():
    assert SUBCATEGORY_OF["Underspecified Instruction"] == "1a"
    assert SUBCATEGORY_OF["Hardcoding"] == "1b"
    assert SUBCATEGORY_OF["Wrong Approach"] == "3a"
    assert SUBCATEGORY_OF["Implementation Bugs"] == "3b"


def test_bucket_subanalyses_counts_and_breakdown():
    bad, good, breakdown = bucket_subanalyses([
        _sa("BAD_SUCCESS", "Hardcoding"),        # bad, 1b
        _sa("BAD_FAILURE", "Underspecified Instruction"),  # bad, 1a
        _sa("GOOD_FAILURE", "Wrong Approach"),   # good, 3a
        _sa("GOOD_FAILURE", "Some New Thing"),   # good, emergent
        _sa("GOOD_SUCCESS", "Correct Solution"), # other, ignored
    ])
    assert len(bad) == 2 and len(good) == 2
    assert breakdown["1b"] == 1
    assert breakdown["1a"] == 1
    assert breakdown["3a"] == 1
    assert breakdown["emergent"] == 1
