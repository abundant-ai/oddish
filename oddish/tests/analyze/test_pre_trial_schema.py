from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    Dimension,
    ProblemType,
    ActionTier,
    PreTrialActionItems,
)


def test_wrapper_holds_items():
    item = ActionItem(
        source=ActionItemSource.PRE_TRIAL,
        problem_type=ProblemType.INCOMPLETENESS,
        dimension=Dimension.VERIFIER,
        file="verifier.py",
        line_start=1,
        line_end=2,
        title="t",
        detail="d",
        recommendation="r",
        tier=ActionTier.MUST_FIX,
    )
    wrapper = PreTrialActionItems(items=[item])
    assert wrapper.items[0].dimension == Dimension.VERIFIER
    assert PreTrialActionItems().items == []
