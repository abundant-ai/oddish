import logging

from api.services.analyzer_block import (
    AnalyzerType,
    AnalyzerInput,
    AnalyzerOutput,
    block_key_prefix,
    block_logger,
)


def test_key_prefix_uses_enum_value():
    assert block_key_prefix(AnalyzerType.HEADROOM_ANALYSIS) == "analyzer/headroom_analysis"


def test_io_dataclasses_accept_any():
    assert AnalyzerInput(input={"a": 1}).input == {"a": 1}
    assert AnalyzerOutput(output="text").output == "text"


def test_block_logger_prepends_prefix(caplog):
    log = block_logger("analyzer/scaling_analysis")
    with caplog.at_level(logging.INFO):
        log.info("hello")
    assert "[analyzer/scaling_analysis] hello" in caplog.text


import pytest

from api.services.analyzer_block import AnalyzerBlock
from api.services.analyzer_llm_client import LLMClientType
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
        "api.services.analyzer_block.get_storage_client", lambda: _FakeStorage()
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
        "api.services.analyzer_block.get_storage_client", lambda: _BoomStorage()
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
        "api.services.analyzer_block.get_session", lambda: _FakeSession()
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

from api.services.analyzer_llm_client import FakeAnalyzerLLMClient


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
    b = _make_block(client=FakeAnalyzerLLMClient(chunks=["partial"], exc=RuntimeError("boom")))
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
        async def stream(self, prompt):
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
