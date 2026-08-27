"""Analysis-trial artifacts upload under a self-labeling segment.

Trial ids repeat across environments that share a bucket, and analysis
trials co-locate with their subject task's storage by design — so without
the label, an analysis agent's session under a colliding prefix reads as a
subject trial's own execution.
"""

from oddish.workers.queue.trial_handler import _artifact_subprefix


def test_each_analysis_kind_gets_its_label():
    assert _artifact_subprefix("qa", 2) == "analysis-qa/attempt-2"
    assert _artifact_subprefix("qa_eval", 2) == "analysis-qa_eval/attempt-2"
    assert _artifact_subprefix("audit", 2) == "analysis-audit/attempt-2"
    assert _artifact_subprefix("summarize", 2) == "analysis-summarize/attempt-2"


def test_agent_and_probe_trials_get_attempt_only_prefixes():
    assert _artifact_subprefix("agent", 1) == "attempt-1"
    assert _artifact_subprefix("agent", 3) == "attempt-3"


def test_the_uploader_nests_the_segment_under_the_trial_prefix():
    from oddish.db.storage import StorageClient

    base = StorageClient._trial_prefix("task-1-7")
    nested = f"{base.rstrip('/')}/analysis-audit/attempt-3/"
    assert nested.startswith(base.rstrip("/"))
    assert nested.endswith("/analysis-audit/attempt-3/")


def test_listing_and_content_share_the_authoritative_root():
    """A listed relative path must resolve at the content endpoint without
    doubling the analysis segment: both sides root at trial_s3_key."""
    from types import SimpleNamespace

    from oddish.core.sharing.helpers import _get_trial_s3_prefix

    nested = "tasks/t-1/trials/t-1-90/analysis-audit/attempt-3/"
    trial = SimpleNamespace(id="t-1-90", trial_s3_key=nested)
    root = _get_trial_s3_prefix(trial)
    assert root == nested
    listed_relative = "agent/claude-code.txt"
    assert f"{root}{listed_relative}".count("analysis-audit/") == 1


def test_attempt_number_must_be_positive():
    import pytest

    with pytest.raises(ValueError, match="must be positive"):
        _artifact_subprefix("agent", 0)
