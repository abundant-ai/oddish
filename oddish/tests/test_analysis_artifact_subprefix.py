"""Analysis-trial artifacts upload under a self-labeling segment.

Trial ids repeat across environments that share a bucket, and analysis
trials co-locate with their subject task's storage by design — so without
the label, an analysis agent's session under a colliding prefix reads as a
subject trial's own execution.
"""

from oddish.workers.queue.trial_handler import _artifact_subprefix


def test_each_analysis_kind_gets_its_label():
    assert _artifact_subprefix({"mode": "qa"}) == "analysis-qa"
    assert _artifact_subprefix({"mode": "audit"}) == "analysis-audit"
    assert _artifact_subprefix({"mode": "summarize"}) == "analysis-summarize"


def test_agent_trials_upload_at_the_bare_prefix():
    assert _artifact_subprefix({"variant_id": "gke", "resolved_sha": "x"}) is None
    assert _artifact_subprefix({"mode": "probe"}) is None
    assert _artifact_subprefix({}) is None
    assert _artifact_subprefix(None) is None


def test_the_uploader_nests_the_segment_under_the_trial_prefix():
    from oddish.db.storage import StorageClient

    base = StorageClient._trial_prefix("task-1-7")
    nested = f"{base.rstrip('/')}/analysis-audit/"
    assert nested.startswith(base.rstrip("/"))
    assert nested.endswith("/analysis-audit/")
