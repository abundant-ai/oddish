"""Integration test: run_cohort runs MAP + REDUCE as AnalyzerBlocks sharing one
injected SandboxAnalyzerLLMClient, and feeds the reduce block's downloaded files
into the unchanged parse_cohort_result.

The fake runtime plants deterministic findings/reduce FILES into the fake
sandbox (the real source of truth); the stream markers are secondary, so the
assertions are on the file-based result.
"""

import json

import pytest

from api.services.cc_chat.analyzer_cohort import run_cohort
from api.services.cc_chat.analyzer_prompt import REDUCE_PATH, findings_path
from api.services.cc_chat.daytona_client import FakeDaytonaClient
from oddish.evals.primitives import SubAnalysis


def _sub(trial_id: str, classification: str) -> SubAnalysis:
    # SubAnalysis requires `recommendation` (no default); `model` is optional.
    return SubAnalysis(
        trial_id=trial_id,
        trajectory_link=f"/tasks/t/probe/{trial_id}",
        model="m",
        classification=classification,
        subtype="s",
        evidence="e",
        root_cause="rc",
        recommendation="r",
    )


class _FakeRuntime:
    """Writes deterministic findings/reduce files into the fake sandbox, then
    streams an assistant event so the block accumulates a chunk too."""

    def __init__(self, fs: dict):
        self._fs = fs

    async def install(self, client, sandbox):
        return None

    async def stream_chat(
        self,
        client,
        sandbox,
        *,
        content,
        claude_session_id,
        daytona_session_id,
        system_prompt=None,
    ):
        if "REDUCE" in content:
            self._fs[REDUCE_PATH] = b'{"bad_failure_content": "## bad\\nmd"}'
            yield {"type": "assistant", "text": "REDUCE"}
        else:
            self._fs[findings_path(1)] = (
                json.dumps(
                    {
                        "trial_id": "b1",
                        "subcategory": "1a",
                        "evidence_quote": "q",
                        "step_ids": ["s1"],
                        "root_cause": "rc",
                        "headroom_signal": "n",
                    }
                )
                + "\n"
            ).encode()
            yield {"type": "assistant", "text": "MAP"}


async def _get(fs, src_path):
    return fs.get(src_path, b"")


@pytest.mark.asyncio
async def test_run_cohort_via_blocks(monkeypatch):
    # Each block persists its raw stream to S3 + DB (best-effort, failure-isolated
    # in production). Stub both so this stays a hermetic wiring test with no real
    # S3/DB and no leaked client session.
    from api.services.analyzer_block import AnalyzerBlock

    async def _noop(self, *a, **kw):
        return None

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", _noop)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", _noop)

    fs: dict[str, bytes] = {}
    client = FakeDaytonaClient()
    monkeypatch.setattr(
        client, "download_file", lambda sandbox, *, src_path: _get(fs, src_path)
    )
    runtime = _FakeRuntime(fs)

    cohort = [_sub("b1", "reward_hacking")]
    host_by_trial = {
        "b1": {
            "trajectory_link": "/tasks/t/probe/b1",
            "model": "m",
            "classification": "reward_hacking",
            "subtype": "s",
            "task_id": "t",
            "task_path": "p",
        }
    }

    findings, sections = await run_cohort(
        client,
        runtime,
        bucket="bad",
        cohort=cohort,
        roster=[],
        counts={"trials": 1},
        oracle_by_trial={},
        host_by_trial=host_by_trial,
        analyzer_id="an1",
        anthropic_key="k",
        api_base="http://api",
        api_key="ak",
        cli_src=b"cli",
        models_by_task=None,
    )
    # SECTION_KEYS_BY_BUCKET["bad"] == ("bad_failure_content",), so the reduce
    # file's key is the section key -- not the literal "bad".
    assert sections["bad_failure_content"] == "## bad\nmd"
    assert [f.trial_id for f in findings] == ["b1"]

    # The sandbox is created and torn down by run_cohort itself.
    assert len(client.deleted) == 1
