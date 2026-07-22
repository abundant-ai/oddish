import logging

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerType,
    AnalyzerInput,
    AnalyzerOutput,
    block_key_prefix,
    block_logger,
)


def test_key_prefix_uses_enum_value():
    assert (
        block_key_prefix(AnalyzerType.HEADROOM_ANALYSIS) == "analyzer/headroom_analysis"
    )


def test_task_verdict_analyzer_type_value():
    assert AnalyzerType.TASK_VERDICT.value == "task_verdict"


def test_io_dataclasses_accept_any():
    assert AnalyzerInput(input={"a": 1}).input == {"a": 1}
    assert AnalyzerOutput(output="text").output == "text"


def test_block_logger_prepends_prefix(caplog):
    log = block_logger("analyzer/scaling_analysis")
    with caplog.at_level(logging.INFO):
        log.info("hello")
    assert "[analyzer/scaling_analysis] hello" in caplog.text


import pytest

from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.db.models import JobStatus, utcnow


def _make_block(**over):
    kw = dict(
        analyzer_type=AnalyzerType.HEADROOM_ANALYSIS,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"x": 1}),
        prompt="do the thing",
    )
    kw.update(over)
    return AnalyzerBlock(**kw)


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


import asyncio

from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient


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
            b._downloaded_files[p] = await b._client._download_file(p)
    assert calls["n"] == 1  # second pass fetched nothing new


def test_files_to_download_rejected_on_api_backend():
    with pytest.raises(ValueError, match="files_to_download"):
        _make_block(
            llm_client_type=LLMClientType.API,
            client=FakeAnalyzerLLMClient(),
            input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
        )


def test_files_to_download_requires_injected_client():
    with pytest.raises(ValueError, match="injected client"):
        _make_block(
            llm_client_type=LLMClientType.SANDBOX,
            client=None,
            input=AnalyzerInput(input={}, files_to_download=["out/reduce.json"]),
        )
