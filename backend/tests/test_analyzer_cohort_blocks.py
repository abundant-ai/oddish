"""Integration test: parallel MAP blocks and a REDUCE block each own their
sandbox client and exchange findings through declared block artifacts.

The fake runtime plants deterministic findings/reduce FILES into the fake
sandbox (the real source of truth); the stream markers are secondary, so the
assertions are on the file-based result.
"""

import json

import pytest

from api.services.cc_chat.analyzer_block_runner import run_analyzer_blocks
from api.services.cc_chat.analyzer_prompt import REDUCE_PATH, findings_path
from api.services.cc_chat.daytona_client import FakeDaytonaClient
from api.services.blocks.analyzer.sandbox_llm_client import SandboxAnalyzerLLMClient
from oddish.blocks.analyzer.analyzer_llm_client import register_sandbox_client_factory
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
        json_schema=None,
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
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock

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

    async def factory(*, model, api_key, sandbox_config):
        sandbox = await client.create_sandbox(
            env_vars={
                "ANTHROPIC_API_KEY": api_key or "",
                "ANTHROPIC_MODEL": model or "",
                "ODDISH_API_KEY": sandbox_config.oddish_api_key or "",
                "ODDISH_API_BASE_URL": sandbox_config.oddish_api_base_url or "",
            },
            auto_stop_minutes=15,
            auto_delete_minutes=30,
            labels=sandbox_config.labels,
        )
        await runtime.install(client, sandbox)
        for path, content in sandbox_config.files_to_upload.items():
            fs[path] = content
            await client.upload_file(sandbox, dest_path=path, content=content)
        return SandboxAnalyzerLLMClient(
            sandbox=sandbox,
            daytona_client=client,
            runtime=runtime,
            daytona_session_id=sandbox_config.session_id,
        )

    register_sandbox_client_factory(factory)

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

    findings, sections, _by_model = await run_analyzer_blocks(
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
        parallelism=2,
        models_by_task=None,
    )
    # SECTION_KEYS_BY_BUCKET["bad"] == ("bad_failure_content",), so the reduce
    # file's key is the section key -- not the literal "bad".
    assert sections["bad_failure_content"] == "## bad\nmd"
    assert [f.trial_id for f in findings] == ["b1"]

    assert len(client.deleted) == 2
