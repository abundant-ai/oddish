"""Analysis-trial artifacts upload under a self-labeling segment.

Trial ids repeat across environments that share a bucket, and analysis
trials co-locate with their subject task's storage by design — so without
the label, an analysis agent's session under a colliding prefix reads as a
subject trial's own execution.
"""

import pytest

from oddish.workers.queue.trial_handler import _artifact_subprefix


class _Storage:
    def __init__(self, objects, listed=()):
        self.objects = objects
        self.listed = list(listed)
        self.downloaded = []
        self.list_trial_roots = []

    async def object_exists(self, key):
        return key in self.objects

    async def download_text(self, key):
        self.downloaded.append(key)
        return self.objects[key]

    async def download_bytes(self, key):
        self.downloaded.append(key)
        return self.objects[key].encode()

    async def list_keys(self, prefix):
        return [key for key in self.listed if key.startswith(prefix)]

    async def list_trial_files(self, **kwargs):
        self.list_trial_roots.append(kwargs["root_prefix"])
        return {"files": []}


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


@pytest.mark.asyncio
async def test_listing_and_content_share_the_manifest_selected_root():
    """A listed relative path must resolve at the content endpoint without
    doubling the analysis segment: both sides root at the selected child."""
    import json
    from types import SimpleNamespace

    from oddish.core.trial_artifacts import resolve_trial_artifact_layout

    nested = "tasks/t-1/trials/t-1-90/analysis-audit/attempt-3/"
    trial = SimpleNamespace(id="t-1-90", trial_s3_key=nested)

    class Storage:
        async def object_exists(self, key):
            return key == f"{nested}result.json"

        async def download_text(self, key):
            assert key == f"{nested}result.json"
            return json.dumps({"trial_results": [{"trial_name": "audit-run"}]})

    layout = await resolve_trial_artifact_layout(trial, Storage())
    root = layout.artifact_prefix
    assert root == f"{nested}audit-run/"
    listed_relative = "agent/claude-code.txt"
    assert f"{root}{listed_relative}".count("analysis-audit/") == 1


@pytest.mark.asyncio
async def test_analysis_import_reads_only_the_manifest_selected_artifact(monkeypatch):
    import json
    from types import SimpleNamespace

    from oddish.workers import analysis_trials

    prefix = "tasks/t-1/trials/t-1-90/analysis-qa/attempt-2/"
    current_key = f"{prefix}current-run/verifier/qa_result.json"
    stale_key = f"{prefix}old-run/verifier/qa_result.json"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            current_key: '{"selected": "current"}',
            stale_key: '{"selected": "stale"}',
        },
        listed=[stale_key, current_key],
    )
    monkeypatch.setattr(analysis_trials, "get_storage_client", lambda: storage)

    artifact = await analysis_trials.read_analysis_artifact(
        SimpleNamespace(id="t-1-90", trial_s3_key=prefix),
        "qa_result.json",
    )

    assert artifact == {"selected": "current"}
    assert stale_key not in storage.downloaded


@pytest.mark.asyncio
async def test_analysis_import_with_no_pointer_rejects_attempt_siblings(monkeypatch):
    from types import SimpleNamespace

    from oddish.db.storage import StorageClient
    from oddish.workers import analysis_trials

    trial_id = "t-1-90"
    root = StorageClient._trial_prefix(trial_id)
    stale_key = f"{root}analysis-qa/attempt-1/old/verifier/qa_result.json"
    storage = _Storage(
        {stale_key: '{"selected": "stale"}'},
        listed=[stale_key],
    )
    monkeypatch.setattr(analysis_trials, "get_storage_client", lambda: storage)

    artifact = await analysis_trials.read_analysis_artifact(
        SimpleNamespace(id=trial_id, trial_s3_key=None),
        "qa_result.json",
    )

    assert artifact is None
    assert stale_key not in storage.downloaded


@pytest.mark.asyncio
async def test_public_file_listing_and_content_use_the_selected_child(monkeypatch):
    import json
    from types import SimpleNamespace

    from oddish.core.sharing import helpers

    prefix = "tasks/t-1/trials/t-1-90/attempt-2/"
    selected = f"{prefix}current-run/"
    file_key = f"{selected}agent/claude-code.txt"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            file_key: "CURRENT",
            f"{prefix}old-run/agent/claude-code.txt": "STALE",
        }
    )
    monkeypatch.setattr(helpers, "get_storage_client", lambda: storage)
    trial = SimpleNamespace(id="t-1-90", trial_s3_key=prefix)

    await helpers.list_trial_files_s3(trial)
    content, media_type = await helpers.get_trial_file_content_s3(
        trial, "agent/claude-code.txt"
    )

    assert storage.list_trial_roots == [selected]
    assert content == b"CURRENT"
    assert media_type == "text/plain"


def test_attempt_number_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        _artifact_subprefix("agent", 0)
