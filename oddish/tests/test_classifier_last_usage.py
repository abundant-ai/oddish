from oddish.analyze import TrialClassifier
from oddish.analyze.analysis_cost import AnalysisUsage


def test_last_usage_defaults_to_none() -> None:
    clf = TrialClassifier(model="anthropic/claude-sonnet-4", verbose=False, timeout=1)
    assert clf.last_usage is None


def test_stash_from_payload_sets_last_usage() -> None:
    clf = TrialClassifier(model="anthropic/claude-sonnet-4", verbose=False, timeout=1)
    clf._stash_usage(
        {"total_cost_usd": 0.01, "usage": {"input_tokens": 3}},
        "anthropic/claude-sonnet-4",
    )
    assert isinstance(clf.last_usage, AnalysisUsage)
    assert clf.last_usage.cost_usd == 0.01
    assert clf.last_usage.input_tokens == 3
