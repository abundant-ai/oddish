import asyncio
import json

import pytest

from api.services.cc_chat import analyzer_trajectory as at

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Records uploads. Mirrors StorageClient.upload_bytes' signature only."""

    def __init__(self, exc=None):
        self.calls = []
        self._exc = exc

    async def upload_bytes(self, data, s3_key, *, content_type=None):
        self.calls.append({"data": data, "s3_key": s3_key,
                           "content_type": content_type})
        if self._exc:
            raise self._exc


def _install(monkeypatch, storage):
    monkeypatch.setattr(at, "get_storage_client", lambda: storage)


def test_trajectory_key_layout():
    assert at.trajectory_key("a1", "bad", "map-01") == \
        "analyzers/a1/bad/map-01.jsonl"


def test_map_slug_is_zero_padded():
    # Unpadded, map-10 would sort before map-2.
    assert at.map_slug(1) == "map-01"
    assert at.map_slug(2) == "map-02"
    assert at.map_slug(10) == "map-10"
    assert sorted([at.map_slug(2), at.map_slug(10)]) == ["map-02", "map-10"]


def test_reduce_slug():
    assert at.REDUCE_SLUG == "reduce"


async def test_persist_turn_uploads_jsonl(monkeypatch):
    storage = _FakeStorage()
    _install(monkeypatch, storage)
    events = [{"type": "system", "subtype": "init"}, {"type": "result"}]

    await at.persist_turn(analyzer_id="a1", bucket="good", slug="map-01",
                          events=events)

    assert len(storage.calls) == 1
    call = storage.calls[0]
    assert call["s3_key"] == "analyzers/a1/good/map-01.jsonl"
    assert call["content_type"] == "application/x-ndjson"
    lines = call["data"].decode().split("\n")
    assert [json.loads(x) for x in lines] == events


async def test_persist_turn_preserves_events_verbatim(monkeypatch):
    """The record must be raw, not rendered: render_event truncates tool
    inputs to 200 chars, which would hide whether --tail-bytes was widened."""
    storage = _FakeStorage()
    _install(monkeypatch, storage)
    command = "node /home/daytona/workspace/oddish-query trials logs t1 " \
              "--trajectory --tail-bytes 40000" + " # pad" * 100
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}]

    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                          events=events)

    line = json.loads(storage.calls[0]["data"].decode())
    assert line["message"]["content"][0]["input"]["command"] == command


async def test_persist_turn_skips_empty_events(monkeypatch):
    storage = _FakeStorage()
    _install(monkeypatch, storage)

    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="reduce",
                          events=[])

    assert storage.calls == []


async def test_persist_turn_swallows_upload_failure(monkeypatch):
    storage = _FakeStorage(exc=RuntimeError("s3 down"))
    _install(monkeypatch, storage)

    # Must not raise: the findings are the primary product.
    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                          events=[{"type": "result"}])


async def test_persist_turn_lets_cancellation_propagate(monkeypatch):
    """CancelledError is a BaseException; swallowing it would break
    run_cohort's asyncio.timeout."""
    storage = _FakeStorage(exc=asyncio.CancelledError())
    _install(monkeypatch, storage)

    with pytest.raises(asyncio.CancelledError):
        await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                              events=[{"type": "result"}])
