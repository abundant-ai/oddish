import asyncio
import json

import pytest

from api.services.cc_chat import analyzer_cohort as ac
from api.services.cc_chat.analyzer_parse import CohortParseError
from api.services.cc_chat.analyzer_prompt import FINDINGS_PATH, REDUCE_PATH
from api.services.cc_chat.daytona_client import FakeDaytonaClient
from oddish.evals.primitives import SubAnalysis

pytestmark = pytest.mark.asyncio

COHORT = [
    SubAnalysis(
        trial_id="bad-1", trajectory_link="/tasks/t1/probe/bad-1",
        classification="reward_hacking", subtype="1a", evidence="e",
        root_cause="rc", recommendation="r",
    )
]
ROSTER = [{"trial_id": "bad-1", "bucket": "bad", "subtype": "1a",
           "trajectory_link": "/tasks/t1/probe/bad-1"}]
COUNTS = {"trials": 1, "bad": 1, "good": 0}


class _FakeRuntime:
    """Stands in for ClaudeCodeRuntime: no install, canned stream, and it
    plants the agent's output files the way a real agent would."""

    def __init__(self, events, *, files=None):
        self._events = events
        self._files = files or {}
        self.installed = False

    async def install(self, client, sandbox):
        self.installed = True

    async def stream_chat(self, client, sandbox, *, content,
                          claude_session_id, daytona_session_id):
        for path, body in self._files.items():
            await client.upload_file(sandbox, dest_path=path, content=body)
        for evt in self._events:
            yield evt


class _SlowRuntime(_FakeRuntime):
    """A wedged agent: stream_chat never yields within the timeout window.
    Plants good output files anyway, so a non-firing timeout would let
    run_cohort return normally instead of raising for an unrelated reason."""

    def __init__(self, delay):
        super().__init__([], files=_good_files())
        self._delay = delay

    async def stream_chat(self, client, sandbox, *, content,
                          claude_session_id, daytona_session_id):
        for path, body in self._files.items():
            await client.upload_file(sandbox, dest_path=path, content=body)
        await asyncio.sleep(self._delay)
        if False:  # pragma: no cover - keeps this an async generator
            yield


def _kwargs(**over):
    base = dict(
        bucket="bad", cohort=COHORT, roster=ROSTER, counts=COUNTS,
        oracle_by_trial={"bad-1": "oracle"}, analyzer_id="a1",
        anthropic_key="sk-ant-test", api_base="https://api.example", api_key="k",
        cli_src=b"#!/usr/bin/env node",
    )
    base.update(over)
    return base


def _good_files():
    return {
        REDUCE_PATH: json.dumps({"bad_failure_content": "# Bad"}).encode(),
        FINDINGS_PATH: (json.dumps({
            "trial_id": "bad-1", "bucket": "bad", "subcategory": "1a",
            "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
            "headroom_signal": "h", "trajectory_link": "junk",
        }) + "\n").encode(),
    }


async def test_run_cohort_returns_findings_and_sections():
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([{"type": "result", "subtype": "success"}],
                           files=_good_files())
    findings, sections = await ac.run_cohort(client, runtime, **_kwargs())
    assert sections == {"bad_failure_content": "# Bad"}
    assert [f.trial_id for f in findings] == ["bad-1"]
    assert findings[0].trajectory_link == "/tasks/t1/probe/bad-1"


async def test_run_cohort_uploads_the_cli_and_forces_haiku():
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([], files=_good_files())
    await ac.run_cohort(client, runtime, **_kwargs())
    sbx = next(iter(client.sandboxes.values()))
    assert sbx["files"]["/home/daytona/workspace/oddish-query"] == b"#!/usr/bin/env node"
    assert sbx["env"]["ANTHROPIC_MODEL"] == ac.HAIKU_MODEL
    assert sbx["env"]["ODDISH_API_KEY"] == "k"


async def test_run_cohort_passes_the_agent_its_inference_key():
    """Without ANTHROPIC_API_KEY the agent can't reach inference, and that
    failure reports success with 0 tokens instead of raising."""
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([], files=_good_files())
    await ac.run_cohort(client, runtime, **_kwargs())
    sbx = next(iter(client.sandboxes.values()))
    assert sbx["env"]["ANTHROPIC_API_KEY"] == "sk-ant-test"


async def test_run_cohort_deletes_the_sandbox_on_success():
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([], files=_good_files())
    await ac.run_cohort(client, runtime, **_kwargs())
    assert len(client.deleted) == 1


async def test_run_cohort_deletes_the_sandbox_on_failure():
    """A leaked sandbox bills until auto-delete; teardown must be unconditional."""
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([])  # no files planted -> parse fails
    with pytest.raises(CohortParseError):
        await ac.run_cohort(client, runtime, **_kwargs())
    assert len(client.deleted) == 1


async def test_run_cohort_falls_back_to_stream_when_files_absent():
    client = FakeDaytonaClient()
    reduce_line = "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# Stream"})
    runtime = _FakeRuntime([
        {"type": "assistant", "message": {"content": [{"type": "text",
                                                       "text": reduce_line}]}},
    ])
    _, sections = await ac.run_cohort(client, runtime, **_kwargs())
    assert sections == {"bad_failure_content": "# Stream"}


async def test_run_cohort_raises_on_timeout_and_still_deletes_sandbox(monkeypatch):
    """The 30-min safety net: a wedged agent must not hold the job or the
    sandbox open forever."""
    monkeypatch.setattr(ac, "COHORT_TIMEOUT_SECONDS", 0.05)
    client = FakeDaytonaClient()
    runtime = _SlowRuntime(delay=0.2)
    with pytest.raises(RuntimeError, match="exceeded") as exc_info:
        await ac.run_cohort(client, runtime, **_kwargs())
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert len(client.deleted) == 1


async def test_parse_happens_after_teardown(monkeypatch):
    """A parse failure must not hold a sandbox open."""
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([], files=_good_files())
    seen = {}

    def spy(*a, **kw):
        seen["deleted_at_parse_time"] = len(client.deleted)
        return ([], {"bad_failure_content": "# B"})

    monkeypatch.setattr(ac, "parse_cohort_result", spy)
    await ac.run_cohort(client, runtime, **_kwargs())
    assert seen["deleted_at_parse_time"] == 1


async def test_run_cohort_logs_the_stream_with_bucket_prefix(caplog):
    client = FakeDaytonaClient()
    runtime = _FakeRuntime(
        [{"type": "system", "subtype": "init", "model": ac.HAIKU_MODEL}],
        files=_good_files(),
    )
    with caplog.at_level("INFO"):
        await ac.run_cohort(client, runtime, **_kwargs())
    assert any("[analyzer a1][bad]" in r.message for r in caplog.records)
