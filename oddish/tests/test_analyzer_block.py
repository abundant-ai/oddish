import asyncio
import logging
from types import SimpleNamespace

import pytest

from oddish.analyze.classifier import TrialClassifier
from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerType,
    AnalyzerInput,
    AnalyzerOutput,
    block_key_prefix,
    block_logger,
)
from oddish.blocks.analyzer.analyzer_llm_client import (
    FakeAnalyzerLLMClient,
    LLMClientType,
)
from oddish.db.models import JobStatus, utcnow


def test_key_prefix_uses_enum_value():
    assert (
        block_key_prefix(AnalyzerType.HEADROOM_ANALYSIS) == "analyzer/headroom_analysis"
    )


def test_task_verdict_analyzer_type_value():
    assert AnalyzerType.TASK_VERDICT.value == "task_verdict"
    assert AnalyzerType.POST_TRIAL.value == "post_trial"


@pytest.mark.asyncio
async def test_post_trial_classifier_runs_and_stamps_analyzer_block(
    monkeypatch, tmp_path
):
    from oddish.blocks.analyzer import analyzer_llm_client

    captured = {}
    classifier = TrialClassifier(model="anthropic/test")

    async def fake_cli(prompt, trial_dir, task_dir, *, extra_dirs=None):
        return {"classification": "GOOD_SUCCESS"}

    async def fake_s3(self, raw):
        captured["raw"] = raw

    async def fake_db(self):
        captured["block"] = self

    async def fake_cost(self):
        return None

    monkeypatch.setattr(classifier, "_run_claude_cli", fake_cli)
    monkeypatch.setattr(
        analyzer_llm_client,
        "sandbox_client_factory_registered",
        lambda: False,
    )
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", fake_s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", fake_db)
    monkeypatch.setattr(AnalyzerBlock, "record_cost", fake_cost)

    output = await classifier._run_in_analyzer_block(
        prompt="classify",
        trial_dir=tmp_path,
        task_dir=tmp_path,
        extra_dirs=None,
        context={
            "trial_id": "trial-1",
            "task_id": "task-1",
            "prompt_key": "QA_POST_TRIAL",
            "prompt_version": 7,
            "prompt_id": "prompt-1",
        },
    )

    block = captured["block"]
    assert output == {"classification": "GOOD_SUCCESS"}
    assert block.analyzer_type is AnalyzerType.POST_TRIAL
    assert block.analyzer_id == "trial-1"
    assert block.task_id == "task-1"
    assert block.llm_client_type is LLMClientType.CLAUDE_CLI
    assert block.block_metadata["prompt_key"] == "QA_POST_TRIAL"
    assert block.block_metadata["prompt_version"] == 7
    assert captured["raw"] == b'{"classification": "GOOD_SUCCESS"}'


@pytest.mark.asyncio
async def test_post_trial_sandbox_opt_in_uploads_and_rewrites_paths(
    monkeypatch, tmp_path
):
    """With the sandbox explicitly enabled, artifacts are shipped and the
    prompt's host paths are rewritten to their in-sandbox locations."""
    from oddish.blocks.analyzer import analyzer_llm_client
    from oddish.config import settings

    monkeypatch.setattr(settings, "post_trial_sandbox_enabled", True)

    task_dir = tmp_path / "task-local"
    trial_dir = tmp_path / "trial-local"
    task_dir.mkdir()
    trial_dir.mkdir()
    (task_dir / "README.md").write_text("task")
    (trial_dir / "result.json").write_text("{}")
    captured = {}

    monkeypatch.setattr(
        analyzer_llm_client,
        "sandbox_client_factory_registered",
        lambda: True,
    )

    async def fake_run(self):
        captured["block"] = self
        return SimpleNamespace(output={"classification": "GOOD_SUCCESS"})

    monkeypatch.setattr(AnalyzerBlock, "run", fake_run)
    classifier = TrialClassifier(model="anthropic/test")
    output = await classifier._run_in_analyzer_block(
        prompt=f"read {task_dir} and {trial_dir}",
        trial_dir=trial_dir,
        task_dir=task_dir,
        extra_dirs=None,
        context={"trial_id": "trial-1", "task_id": "task-1"},
    )

    block = captured["block"]
    assert output == {"classification": "GOOD_SUCCESS"}
    assert block.llm_client_type is LLMClientType.SANDBOX
    assert block._client_factory is None
    assert block._sandbox_config.files_to_upload
    assert str(task_dir) not in block.prompt
    assert str(trial_dir) not in block.prompt
    assert "/home/daytona/workspace/post-trial/task" in block.prompt
    assert "/home/daytona/workspace/post-trial/trial" in block.prompt


@pytest.mark.asyncio
async def test_post_trial_sandbox_rewrites_qa_context_refs(monkeypatch, tmp_path):
    """The prompt cites `.qa_context/*.json` by absolute path so the sandbox
    rewrite carries it; a relative ref would resolve against the sandbox cwd."""
    from oddish.analyze.classifier import _write_qa_context
    from oddish.blocks.analyzer import analyzer_llm_client

    task_dir = tmp_path / "task-local"
    trial_dir = tmp_path / "trial-local"
    task_dir.mkdir()
    trial_dir.mkdir()
    qa_dir, pre_trial_context, file_access_context = _write_qa_context(
        trial_dir, [{"id": "a1"}], [{"step": 0}]
    )
    assert str(qa_dir) in pre_trial_context
    assert str(qa_dir) in file_access_context

    captured = {}
    monkeypatch.setattr(
        analyzer_llm_client, "sandbox_client_factory_registered", lambda: True
    )

    async def fake_run(self):
        captured["block"] = self
        return SimpleNamespace(output={})

    monkeypatch.setattr(AnalyzerBlock, "run", fake_run)
    await TrialClassifier(model="anthropic/test")._run_in_analyzer_block(
        prompt=f"{pre_trial_context}\n{file_access_context}",
        trial_dir=trial_dir,
        task_dir=task_dir,
        extra_dirs=[qa_dir],
        context={"trial_id": "trial-1", "task_id": "task-1"},
    )

    prompt = captured["block"].prompt
    assert str(qa_dir) not in prompt
    assert "/home/daytona/workspace/post-trial/trial/.qa_context/pre_trial.json" in prompt
    assert (
        "/home/daytona/workspace/post-trial/trial/.qa_context/file_access.json" in prompt
    )


@pytest.mark.asyncio
async def test_post_trial_sandbox_constrains_output_to_schema(monkeypatch, tmp_path):
    """Without --json-schema the model follows the prompt's example object,
    which omits action_items/exploitation -- the post-trial linkage payload."""
    from oddish.blocks.analyzer import analyzer_llm_client

    captured = {}
    monkeypatch.setattr(
        analyzer_llm_client, "sandbox_client_factory_registered", lambda: True
    )

    async def fake_run(self):
        captured["block"] = self
        return SimpleNamespace(output={})

    monkeypatch.setattr(AnalyzerBlock, "run", fake_run)
    await TrialClassifier(model="anthropic/test")._run_in_analyzer_block(
        prompt="classify",
        trial_dir=tmp_path,
        task_dir=tmp_path,
        extra_dirs=None,
        context={"trial_id": "trial-1", "task_id": "task-1"},
    )

    schema = captured["block"]._sandbox_config.json_schema
    assert schema
    assert "action_items" in schema
    assert "exploitation" in schema


@pytest.mark.asyncio
async def test_post_trial_sandbox_run_is_bounded_by_classifier_timeout(
    monkeypatch, tmp_path
):
    """A wedged Daytona session must not hold the QA job open indefinitely."""
    from oddish.blocks.analyzer import analyzer_llm_client

    monkeypatch.setattr(
        analyzer_llm_client, "sandbox_client_factory_registered", lambda: True
    )

    async def never_returns(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(AnalyzerBlock, "run", never_returns)
    classifier = TrialClassifier(model="anthropic/test", timeout=0)
    with pytest.raises(TimeoutError):
        await classifier._run_in_analyzer_block(
            prompt="classify",
            trial_dir=tmp_path,
            task_dir=tmp_path,
            extra_dirs=None,
            context={"trial_id": "trial-1", "task_id": "task-1"},
        )


def test_post_trial_parses_sandbox_result_stream():
    raw = (
        '{"type":"assistant","message":{"content":[]}}'
        '{"type":"result","result":"```json\\n'
        '{\\"classification\\":\\"GOOD_SUCCESS\\"}\\n```"}'
    )
    assert TrialClassifier._parse_sandbox_block_output(raw) == {
        "classification": "GOOD_SUCCESS"
    }


def test_io_dataclasses_accept_any():
    assert AnalyzerInput(input={"a": 1}).input == {"a": 1}
    assert AnalyzerOutput(output="text").output == "text"


def test_block_logger_prepends_prefix(caplog):
    log = block_logger("analyzer/scaling_analysis")
    with caplog.at_level(logging.INFO):
        log.info("hello")
    assert "[analyzer/scaling_analysis] hello" in caplog.text

def _make_block(**over):
    client = over.pop("client", None)
    kw = dict(
        analyzer_type=AnalyzerType.HEADROOM_ANALYSIS,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"x": 1}),
        prompt="do the thing",
    )
    kw.update(over)
    block = AnalyzerBlock(**kw)
    if client is not None:
        async def _create_client():
            return client

        block._create_client = _create_client
    return block


def test_block_init_sets_prefix_and_ids():
    b = _make_block()
    assert b.key_prefix == "analyzer/headroom_analysis"
    assert b.id and isinstance(b.id, str)
    assert b.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_save_to_s3_uses_prefix_key(monkeypatch):
    calls = {}

    class _FakeStorage:
        async def upload_bytes(self, data, s3_key, *, content_type=None):
            calls["data"] = data
            calls["key"] = s3_key
            calls["ct"] = content_type

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_storage_client",
        lambda: _FakeStorage(),
    )
    b = _make_block()
    await b.save_to_s3(b"raw-bytes")
    assert calls["key"] == f"analyzer/headroom_analysis/{b.id}"
    assert calls["data"] == b"raw-bytes"
    assert calls["ct"] == "application/x-ndjson"


@pytest.mark.asyncio
async def test_save_to_s3_swallows_and_logs_errors(monkeypatch, caplog):
    class _BoomStorage:
        async def upload_bytes(self, *a, **k):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_storage_client",
        lambda: _BoomStorage(),
    )
    b = _make_block()
    await b.save_to_s3(b"x")  # must NOT raise
    assert "s3 down" in caplog.text or "save_to_s3" in caplog.text


@pytest.mark.asyncio
async def test_save_to_db_adds_row(monkeypatch):
    added = {}

    class _FakeSession:
        def add(self, obj):
            added["obj"] = obj

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_session",
        lambda: _FakeSession(),
    )
    b = _make_block(block_metadata={"k": "v"})
    b.status = JobStatus.SUCCESS
    b.output = AnalyzerOutput(output="result-text")
    b.job_started_at = utcnow()
    b.job_ended_at = b.job_started_at
    b.job_duration_seconds = 0.0
    await b.save_to_db()
    row = added["obj"]
    assert row.id == b.id
    assert row.type == "headroom_analysis"
    assert row.llm_client_type == "Api"
    assert row.key_prefix == "analyzer/headroom_analysis"
    assert row.prompt == "do the thing"
    assert row.input == {"x": 1}
    assert row.output == "result-text"
    assert row.status == JobStatus.SUCCESS
    assert row.block_metadata == {"k": "v"}

def _patch_persistence(monkeypatch):
    """Capture save_to_s3 raw + save_to_db without touching S3/DB."""
    saved = {"s3": None, "db": 0}

    async def fake_s3(self, raw):
        saved["s3"] = raw

    async def fake_db(self):
        saved["db"] += 1
        saved["status"] = self.status
        saved["output"] = self.output
        saved["error"] = self.error
        saved["duration"] = self.job_duration_seconds

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", fake_s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", fake_db)
    return saved


@pytest.mark.asyncio
async def test_run_success_persists_output(monkeypatch):
    saved = _patch_persistence(monkeypatch)
    b = _make_block(client=FakeAnalyzerLLMClient(chunks=["foo", "bar"]))
    out = await b.run()
    assert out.output == "foobar"
    assert saved["s3"] == b"foobar"
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.SUCCESS
    assert saved["error"] is None
    assert saved["duration"] is not None and saved["duration"] >= 0


@pytest.mark.asyncio
async def test_run_failure_persists_failed_and_reraises(monkeypatch):
    saved = _patch_persistence(monkeypatch)
    b = _make_block(
        client=FakeAnalyzerLLMClient(chunks=["partial"], exc=RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"):
        await b.run()
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.FAILED
    assert "boom" in saved["error"]
    # Partial stream still reaches S3.
    assert saved["s3"] == b"partial"


@pytest.mark.asyncio
async def test_run_cancellation_still_persists(monkeypatch):
    saved = _patch_persistence(monkeypatch)

    class _HangingClient:
        async def stream(self, prompt, *, system_prompt=None):
            yield "first"
            await asyncio.sleep(3600)

        async def aclose(self):
            return None

    b = _make_block(client=_HangingClient())
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.05)  # let it yield "first", then hang
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.FAILED
    assert saved["s3"] == b"first"


@pytest.mark.asyncio
async def test_run_persist_completes_when_cancelled_during_persist(monkeypatch):
    """Discriminating guard for the shielded-persist pattern: if run() is
    cancelled while the save is in flight, run() must not finish unwinding until
    the save completes. A bare `await asyncio.shield(...)` would unwind
    immediately and leave the DB write unfinished when the task raises.
    """
    saved = {"db": 0}
    entered_s3 = asyncio.Event()
    release = asyncio.Event()

    async def fake_s3(self, raw):
        entered_s3.set()
        await release.wait()  # hang the save mid-flight
        saved["s3"] = raw

    async def fake_db(self):
        saved["db"] += 1

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", fake_s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", fake_db)

    # Stream completes normally; the cancellation lands during persist.
    b = _make_block(client=FakeAnalyzerLLMClient(chunks=["first"]))
    task = asyncio.create_task(b.run())

    await entered_s3.wait()  # run() is now suspended inside the shielded persist
    task.cancel()

    # Correct impl re-awaits the still-blocked persist, so the task cannot finish
    # yet. A bare-shield impl would already be done here (raising CancelledError).
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    assert saved["db"] == 0  # save_to_db hasn't run: save_to_s3 still blocked

    release.set()  # let the save finish
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved["db"] == 1  # the DB write completed before run() unwound
    assert saved["s3"] == b"first"


@pytest.mark.asyncio
async def test_block_forwards_system_prompt(monkeypatch):
    _patch_persistence(monkeypatch)
    fake = FakeAnalyzerLLMClient(chunks=["ok"])
    b = _make_block(client=fake, system_prompt="MAP rules")
    await b.run()
    assert fake.last_system_prompt == "MAP rules"


def test_block_records_model_in_metadata():
    b = _make_block(model="claude-haiku-4-5-20251001", block_metadata={"k": "v"})
    assert b.block_metadata["model"] == "claude-haiku-4-5-20251001"
    assert b.block_metadata["k"] == "v"


def test_block_without_model_leaves_metadata_untouched():
    b = _make_block(block_metadata={"k": "v"})
    assert b.block_metadata == {"k": "v"}


@pytest.mark.asyncio
async def test_files_to_download_populates_output_map(monkeypatch):
    _patch_persistence(monkeypatch)
    fake = FakeAnalyzerLLMClient(
        chunks=["streamed"],
        files={"out/reduce.json": b'{"bad":"x"}', "out/findings-1.jsonl": b'{"t":1}\n'},
    )
    b = _make_block(
        llm_client_type=LLMClientType.SANDBOX,
        client=fake,
        input=AnalyzerInput(
            input={"x": 1},
            files_to_download=["out/reduce.json", "out/findings-1.jsonl"],
        ),
    )
    out = await b.run()
    assert out.output == {
        "out/reduce.json": '{"bad":"x"}',
        "out/findings-1.jsonl": '{"t":1}\n',
    }


@pytest.mark.asyncio
async def test_files_to_download_missing_file_is_empty_string(monkeypatch):
    _patch_persistence(monkeypatch)
    fake = FakeAnalyzerLLMClient(chunks=["s"], files={})  # nothing on disk
    b = _make_block(
        llm_client_type=LLMClientType.SANDBOX,
        client=fake,
        input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
    )
    out = await b.run()
    assert out.output == {"out/reduce.json": ""}


@pytest.mark.asyncio
async def test_files_to_download_idempotent(monkeypatch):
    _patch_persistence(monkeypatch)
    calls = {"n": 0}

    class _CountingFake(FakeAnalyzerLLMClient):
        async def _download_file(self, path):
            calls["n"] += 1
            return b"data"

    fake = _CountingFake(chunks=["s"])
    b = _make_block(
        llm_client_type=LLMClientType.SANDBOX,
        client=fake,
        input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
    )
    await b.run()
    b._chunks = []  # simulate a re-run of the download-bearing tail
    b.status = JobStatus.RUNNING
    for p in b.input.files_to_download:
        if p not in b._downloaded_files:
            b._downloaded_files[p] = await fake._download_file(p)
    assert calls["n"] == 1  # second pass fetched nothing new


def test_files_to_download_rejected_on_api_backend():
    with pytest.raises(ValueError, match="files_to_download"):
        _make_block(
            llm_client_type=LLMClientType.API,
            client=FakeAnalyzerLLMClient(),
            input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
        )


@pytest.mark.asyncio
async def test_self_provisioned_client_stays_open_through_file_download(
    monkeypatch,
):
    _patch_persistence(monkeypatch)

    class _Client(FakeAnalyzerLLMClient):
        def __init__(self):
            super().__init__(chunks=["stream"], files={"out/reduce.json": b"result"})
            self.closed = False

        async def _download_file(self, path):
            assert self.closed is False
            return await super()._download_file(path)

        async def aclose(self):
            self.closed = True

    client = _Client()

    async def create(*args, **kwargs):
        return client

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.create_llm_client", create
    )
    block = _make_block(
        llm_client_type=LLMClientType.SANDBOX,
        input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
    )

    output = await block.run()

    assert output.output == {"out/reduce.json": "result"}
    assert client.closed is True


@pytest.mark.asyncio
async def test_self_provisioning_honors_creation_timeout(monkeypatch):
    _patch_persistence(monkeypatch)

    async def hanging_create(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.create_llm_client", hanging_create
    )
    block = _make_block(
        llm_client_type=LLMClientType.SANDBOX,
        client_creation_timeout=0.01,
    )

    with pytest.raises(TimeoutError):
        await block.run()
