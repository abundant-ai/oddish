from oddish.analyze.models import (
    ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier,
    ExploitationAssessment, TrialClassification, TrialClassificationModel,
)


def test_model_defaults_are_empty():
    m = TrialClassificationModel(
        classification="BAD_SUCCESS", subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec",
    )
    assert m.action_items == []
    assert m.exploitation == []


def test_from_model_carries_new_fields():
    item = ActionItem(
        source=ActionItemSource.POST_TRIAL, problem_type=ProblemType.MISMATCH,
        dimension=Dimension.VERIFIER, file="verifier.py", line_start=1, line_end=1,
        title="t", detail="d", recommendation="r", tier=ActionTier.MUST_FIX,
    )
    assessment = ExploitationAssessment(links_to="abc123", exploited=True, exploit_evidence="step 4", causal=True)
    m = TrialClassificationModel(
        classification="BAD_SUCCESS", subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec",
        action_items=[item], exploitation=[assessment],
    )
    tc = TrialClassification.from_model(trial_name="t1", model=m, reward=1.0)
    assert tc.action_items[0].file == "verifier.py"
    assert tc.exploitation[0].exploited is True
