from oddish.analyze import Classification, TrialClassification, build_verdict_prompt


def _classification(name: str = "t-1") -> TrialClassification:
    return TrialClassification(
        trial_name=name,
        classification=Classification.BAD_SUCCESS,
        subtype="Hardcoding",
        evidence="hardcoded the expected value",
        root_cause="tests assert a literal",
        recommendation="randomize the fixture",
        reward=1.0,
    )


def test_prompt_includes_each_trial():
    prompt = build_verdict_prompt([_classification("t-1"), _classification("t-2")])
    assert "t-1" in prompt
    assert "t-2" in prompt
    assert "Hardcoding" in prompt


def test_prompt_reports_baseline_not_run_when_absent():
    prompt = build_verdict_prompt([_classification()])
    assert "Not run" in prompt


def test_prompt_is_deterministic():
    a = build_verdict_prompt([_classification()])
    b = build_verdict_prompt([_classification()])
    assert a == b
