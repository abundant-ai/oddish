from oddish.analyze import Classification, TrialClassification
from oddish.core.verdict_sync import build_verdict_payload


class _Verdict:
    is_good = False
    confidence = "high"
    primary_issue = "reward hacking"
    recommendations = ["randomize the fixture"]
    reasoning = "two trials hardcoded the answer"


def _c(
    classification: Classification, subtype: str = "Hardcoding"
) -> TrialClassification:
    return TrialClassification(
        trial_name="t",
        classification=classification,
        subtype=subtype,
        evidence="e",
        root_cause="r",
        recommendation="rec",
    )


def test_payload_shape_and_counts():
    classifications = [
        _c(Classification.BAD_SUCCESS),
        _c(Classification.BAD_FAILURE),
        _c(Classification.GOOD_FAILURE),
        _c(Classification.GOOD_SUCCESS),
        _c(Classification.HARNESS_ERROR),
    ]
    payload = build_verdict_payload(_Verdict(), classifications)

    assert payload == {
        "is_good": False,
        "confidence": "high",
        "primary_issue": "reward hacking",
        "reasoning": "two trials hardcoded the answer",
        "recommendations": ["randomize the fixture"],
        "task_problem_count": 2,  # BAD_SUCCESS + BAD_FAILURE
        "agent_problem_count": 1,  # GOOD_FAILURE
        "success_count": 2,  # GOOD_SUCCESS + BAD_SUCCESS
        "harness_error_count": 1,  # HARNESS_ERROR
    }


def test_harness_error_leak_counts_as_task_problem():
    """A hidden_file_leak voids the run, but the exposure is a task defect.
    It must appear in both counts, like BAD_SUCCESS does for successes."""
    classifications = [_c(Classification.HARNESS_ERROR, subtype="hidden_file_leak")]
    payload = build_verdict_payload(_Verdict(), classifications)
    assert payload["task_problem_count"] == 1
    assert payload["harness_error_count"] == 1


def test_counts_ignore_model_supplied_values():
    """Counts come from classifications only -- a model cannot inflate them."""

    class _Lying(_Verdict):
        task_problem_count = 99

    payload = build_verdict_payload(_Lying(), [_c(Classification.GOOD_SUCCESS)])
    assert payload["task_problem_count"] == 0
