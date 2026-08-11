from api.services.cohort_comparison import validate_evidence

SUCCESS = [
    {
        "trial_id": "t1",
        "components": [
            {
                "trajectory_component": "testing_public",
                "step_ids": [34, 35],
                "summary": "Ran mvn test for a baseline.",
            }
        ],
    }
]
FAILING = [
    {
        "trial_id": "t2",
        "components": [
            {
                "trajectory_component": "implementing",
                "step_ids": [12, 13],
                "summary": "Wrote a Spring Boot pom.",
            }
        ],
    }
]


def _out(evidence, side="successful"):
    other = "failing" if side == "successful" else "successful"
    return {
        "schema_version": 1,
        "cohort_success": ["t1"],
        "cohort_failure": ["t2"],
        "categories": [
            {
                "category": "testing_verification",
                "label": None,
                side: [{"behavior_description": "did a thing", "evidence": evidence}],
                other: [],
            }
        ],
    }


def _good():
    return {
        "trial_id": "t1",
        "trajectory_component": "testing_public",
        "step_ids": [34, 35],
        "quote": "Ran mvn test for a baseline.",
    }


def test_valid_evidence_survives():
    filtered, drops = validate_evidence(_out([_good()]), SUCCESS, FAILING)
    assert len(filtered["categories"]) == 1
    assert drops["evidence"] == 0


def test_fabricated_trial_id_is_dropped():
    # An analyzer in this repo previously invented 200 of 200 trial ids.
    bad = {**_good(), "trial_id": "does-not-exist"}
    filtered, drops = validate_evidence(_out([bad]), SUCCESS, FAILING)
    assert filtered["categories"] == []
    assert drops["evidence"] == 1
    assert drops["observations"] == 1


def test_wrong_step_ids_are_dropped():
    bad = {**_good(), "step_ids": [99]}
    filtered, drops = validate_evidence(_out([bad]), SUCCESS, FAILING)
    assert filtered["categories"] == []


def test_altered_quote_is_dropped():
    bad = {**_good(), "quote": "Ran the tests and everything was fine."}
    filtered, drops = validate_evidence(_out([bad]), SUCCESS, FAILING)
    assert filtered["categories"] == []


def test_evidence_cited_on_the_wrong_side_is_dropped():
    # t2 is a failing trial; citing it under `successful` is a cohort error.
    bad = {
        "trial_id": "t2",
        "trajectory_component": "implementing",
        "step_ids": [12, 13],
        "quote": "Wrote a Spring Boot pom.",
    }
    filtered, drops = validate_evidence(_out([bad]), SUCCESS, FAILING)
    assert filtered["categories"] == []
