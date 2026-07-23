from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    Dimension,
    ProblemType,
    ActionTier,
    compute_action_item_id,
)


def _item(**over):
    base = dict(
        source=ActionItemSource.PRE_TRIAL,
        problem_type=ProblemType.INCOMPLETENESS,
        dimension=Dimension.VERIFIER,
        file="verifier.py",
        line_start=10,
        line_end=12,
        title="Verifier ignores stderr",
        detail="grade() only checks stdout",
        recommendation="Also assert on stderr",
        tier=ActionTier.MUST_FIX,
    )
    base.update(over)
    return ActionItem(**base)


def test_defaults_for_post_trial_linkage_fields():
    item = _item()
    assert item.links_to is None
    assert item.exploited is False
    assert item.exploit_evidence is None
    assert item.causal is False


def test_id_is_stable_for_equal_content():
    a = compute_action_item_id(_item())
    b = compute_action_item_id(_item())
    assert a == b
    assert len(a) == 12


def test_id_changes_when_location_changes():
    a = compute_action_item_id(_item(line_start=10))
    b = compute_action_item_id(_item(line_start=99))
    assert a != b


def test_enum_values_serialize_as_strings():
    item = _item()
    dumped = item.model_dump(mode="json")
    assert dumped["source"] == "pre_trial"
    assert dumped["dimension"] == "verifier"
    assert dumped["tier"] == "must_fix"
