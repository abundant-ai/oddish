from oddish.analyze.models import (
    ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier,
    ExploitationAssessment, Classification, TrialClassification,
)
from oddish.workers.queue.analysis_handler import classification_to_result_dict


def test_result_dict_includes_action_items_and_exploitation():
    item = ActionItem(
        source=ActionItemSource.POST_TRIAL, problem_type=ProblemType.MISMATCH,
        dimension=Dimension.VERIFIER, file="verifier.py", line_start=1, line_end=1,
        title="t", detail="d", recommendation="r", tier=ActionTier.MUST_FIX,
    )
    tc = TrialClassification(
        trial_name="t1", classification=Classification.BAD_SUCCESS, subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec", reward=1.0,
        action_items=[item], exploitation=[ExploitationAssessment(links_to="x", exploited=True)],
    )
    d = classification_to_result_dict(tc)
    assert d["action_items"][0]["file"] == "verifier.py"
    assert d["exploitation"][0]["exploited"] is True
    assert d["classification"] == "BAD_SUCCESS"
