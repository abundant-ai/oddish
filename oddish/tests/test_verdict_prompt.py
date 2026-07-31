from oddish.analyze import Classification, TrialClassification, build_verdict_prompt
from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    ActionTier,
    Dimension,
    ProblemType,
)


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


def test_prompt_lists_action_items_so_unused_leaks_reach_the_verdict():
    """An all-pass run on a leaky task must still show the hole: the leak can
    exist only as a must_fix item on GOOD_* trials, and the verdict's leak
    rule keys on exactly that line."""
    item = ActionItem(
        source=ActionItemSource.POST_TRIAL,
        problem_type=ProblemType.MISMATCH,
        dimension=Dimension.INFO_LEAKAGE,
        file="Dockerfile",
        line_start=3,
        line_end=3,
        title="The container copies tests/ into the workspace",
        detail="tests/ is readable by the agent",
        recommendation="Do not copy tests/ into the image",
        tier=ActionTier.MUST_FIX,
    )
    good = TrialClassification(
        trial_name="t-1",
        classification=Classification.GOOD_SUCCESS,
        subtype="legitimate_solution",
        evidence="all tests passed",
        root_cause="the agent solved it",
        recommendation="N/A",
        reward=1.0,
        action_items=[item],
    )
    prompt = build_verdict_prompt([good])
    assert (
        "[must_fix/info_leakage] The container copies tests/ into the workspace"
        in prompt
    )


def test_prompt_marks_empty_action_items():
    prompt = build_verdict_prompt([_classification()])
    assert "(none)" in prompt


def _pre_trial_leak(exploited: bool = False) -> ActionItem:
    return ActionItem(
        source=ActionItemSource.PRE_TRIAL,
        problem_type=ProblemType.MISMATCH,
        dimension=Dimension.INFO_LEAKAGE,
        file="Dockerfile",
        line_start=3,
        line_end=3,
        title="The image ships the reference solution",
        detail="solution/ is readable by the agent",
        recommendation="Do not copy solution/ into the image",
        tier=ActionTier.MUST_FIX,
        exploited=exploited,
    )


def test_prompt_lists_pre_trial_findings_so_unexploited_holes_reach_the_verdict():
    """The classify prompt forbids repeating a pre-trial item in trial action
    items, so a hole no trial used reaches the verdict only through the
    static-check section — and the verdict's leak rule keys on that line."""
    prompt = build_verdict_prompt(
        [_classification()], pre_trial_items=[_pre_trial_leak()]
    )
    assert (
        "[must_fix/info_leakage] The image ships the reference solution" in prompt
    )
    assert "(no trial used it)" in prompt


def test_prompt_marks_exploited_pre_trial_findings():
    prompt = build_verdict_prompt(
        [_classification()], pre_trial_items=[_pre_trial_leak(exploited=True)]
    )
    assert "(a trial exploited it)" in prompt


def test_prompt_without_pre_trial_items_keeps_the_pass_glyph():
    prompt = build_verdict_prompt([_classification()], pre_trial_items=[])
    assert "✓ Passed" in prompt
