import asyncio
import json

import pytest

from api.services.cc_chat import analyzer_cohort as ac
from api.services.cc_chat import analyzer_prompt as ap
from api.services.cc_chat.analyzer_parse import CohortParseError
from api.services.cc_chat.analyzer_prompt import (
    FINDINGS_GLOB,
    REDUCE_PATH,
    findings_path,
)
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
                          claude_session_id, daytona_session_id,
                          system_prompt=None):
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
                          claude_session_id, daytona_session_id,
                          system_prompt=None):
        for path, body in self._files.items():
            await client.upload_file(sandbox, dest_path=path, content=body)
        await asyncio.sleep(self._delay)
        if False:  # pragma: no cover - keeps this an async generator
            yield


HOSTS = {
    "bad-1": {
        "trajectory_link": "/tasks/t1/probe/bad-1", "model": "m",
        "classification": "BAD_FAILURE", "subtype": "1a",
        "task_id": "task-1", "task_path": "tasks/t1",
    }
}


def _kwargs(**over):
    base = dict(
        bucket="bad", cohort=COHORT, roster=ROSTER, counts=COUNTS,
        oracle_by_trial={"bad-1": "oracle"}, host_by_trial=HOSTS, analyzer_id="a1",
        anthropic_key="sk-ant-test", api_base="https://api.example", api_key="k",
        cli_src=b"#!/usr/bin/env node",
    )
    base.update(over)
    return base


def _good_files():
    return {
        REDUCE_PATH: json.dumps({"bad_failure_content": "# Bad"}).encode(),
        findings_path(1): (json.dumps({
            "trial_id": "bad-1", "bucket": "bad", "subcategory": "1a",
            "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
            "headroom_signal": "h", "trajectory_link": "junk",
        }) + "\n").encode(),
    }


async def test_run_cohort_returns_findings_and_sections():
    client = FakeDaytonaClient()
    runtime = _FakeRuntime([{"type": "result", "subtype": "success"}],
                           files=_good_files())
    findings, sections, _ = await ac.run_cohort(client, runtime, **_kwargs())
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
    _, sections, _ = await ac.run_cohort(client, runtime, **_kwargs())
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
        return ([], {"bad_failure_content": "# B"}, ([], ""))

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


# --- batching -----------------------------------------------------------
# A cohort used to be handed to ONE claude process, so context grew with trial
# count and a 97-trial cohort exhausted the window before REDUCE, leaving a 0B
# reduce.json. Proven against prod data: the same 'good' bucket succeeded at 8
# trials and failed at 97. These pin the batching that bounds it.


def _cohort_of(n):
    return [
        SubAnalysis(
            trial_id=f"t{i}", trajectory_link=f"/tasks/t/probe/t{i}",
            classification="reward_hacking", subtype="1a", evidence="e",
            root_cause="rc", recommendation="r",
        )
        for i in range(n)
    ]


async def test_batches_keeps_a_small_cohort_as_one_batch():
    # The shape that already works today must not change.
    cohort = _cohort_of(ac.MAP_BATCH_SIZE)
    assert ac.batches(cohort) == [cohort]


async def test_batches_splits_a_large_cohort_and_loses_no_trials():
    cohort = _cohort_of(97)
    plan = ac.batches(cohort)
    assert len(plan) == 10
    assert all(len(b) <= ac.MAP_BATCH_SIZE for b in plan)
    # Every trial appears exactly once, in order.
    assert [sa.trial_id for b in plan for sa in b] == [sa.trial_id for sa in cohort]


class _CountingRuntime(_FakeRuntime):
    """Records each turn's prompt so the map/reduce split can be asserted."""

    def __init__(self, events, *, files=None):
        super().__init__(events, files=files)
        self.prompts = []
        self.system_prompts = []

    async def stream_chat(self, client, sandbox, *, content,
                          claude_session_id, daytona_session_id,
                          system_prompt=None):
        self.prompts.append(content)
        self.system_prompts.append(system_prompt)
        assert claude_session_id is None, "a resumed session would chain contexts"
        for path, body in self._files.items():
            await client.upload_file(sandbox, dest_path=path, content=body)
        for evt in self._events:
            yield evt


async def test_run_cohort_runs_one_turn_per_batch_plus_a_reduce():
    client = FakeDaytonaClient()
    runtime = _CountingRuntime([], files=_good_files())
    cohort = _cohort_of(25)  # -> 3 map batches
    hosts = {sa.trial_id: {"trajectory_link": sa.trajectory_link, "model": "m",
                           "classification": "reward_hacking", "subtype": "1a",
                           "task_id": "t", "task_path": "tasks/t"}
             for sa in cohort}

    await ac.run_cohort(client, runtime, **_kwargs(cohort=cohort, host_by_trial=hosts,
                                                  oracle_by_trial={}))

    assert len(runtime.prompts) == 4  # 3 map + 1 reduce
    assert runtime.prompts[-1].count("REDUCE RESULT:") == 1
    # The reduce turn must read EVERY batch's findings off disk (the glob), not
    # carry trajectories and not read just one batch's file.
    assert FINDINGS_GLOB in runtime.prompts[-1]
    # A map turn must not also be asked to synthesize.
    assert "REDUCE RESULT:" not in runtime.prompts[0]


async def test_map_batches_only_name_their_own_trials():
    client = FakeDaytonaClient()
    runtime = _CountingRuntime([], files=_good_files())
    cohort = _cohort_of(25)
    hosts = {sa.trial_id: {"trajectory_link": sa.trajectory_link, "model": "m",
                           "classification": "reward_hacking", "subtype": "1a",
                           "task_id": "t", "task_path": "tasks/t"}
             for sa in cohort}

    await ac.run_cohort(client, runtime, **_kwargs(cohort=cohort, host_by_trial=hosts,
                                                   oracle_by_trial={}))

    first_map = runtime.prompts[0]
    # t0..t9 are batch 1's cohort block; t10 belongs to batch 2 and must not be
    # in batch 1's "trials to analyze now" list (the roster is separate).
    assert "- trial_id: t0" in first_map
    assert "- trial_id: t10" not in first_map


# --- trajectory tail budget + fetch-more system prompt --------------------
# The CLI returns only the tail of a trajectory, and a truncated trajectory
# still reads as coherent -- so an agent will analyze the fragment and never
# notice what it is missing. The system prompt is the counterweight, and it must
# ride EVERY turn because each batch is a fresh context with no memory.


async def test_every_map_turn_carries_the_fetch_more_system_prompt():
    client = FakeDaytonaClient()
    runtime = _CountingRuntime([], files=_good_files())
    cohort = _cohort_of(25)
    hosts = {sa.trial_id: {"trajectory_link": sa.trajectory_link, "model": "m",
                           "classification": "reward_hacking", "subtype": "1a",
                           "task_id": "t", "task_path": "tasks/t"}
             for sa in cohort}

    await ac.run_cohort(client, runtime, **_kwargs(cohort=cohort, host_by_trial=hosts,
                                                   oracle_by_trial={}))

    # 3 map turns + reduce. Every MAP turn needs it: context resets per batch, so
    # a batch that lost the prompt would under-fetch silently.
    assert len(runtime.system_prompts) == 4
    maps, reduce_sp = runtime.system_prompts[:3], runtime.system_prompts[3]
    assert all(sp and "--tail-bytes" in sp for sp in maps)
    # REDUCE must NOT be told to fetch: it would contradict its own prompt and
    # could refetch the whole cohort, recreating the context blowup.
    assert reduce_sp is None


async def test_system_prompt_advertises_the_budget_the_cli_will_honour():
    # The number the agent is told and the number the CLI enforces come from one
    # constant; a literal in either place could drift and the prompt would
    # advertise a budget that does not exist.
    sp = ap.build_system_prompt(ac.TRAJ_TAIL_BYTES)
    assert str(ac.TRAJ_TAIL_BYTES) in sp


async def test_sandbox_gets_the_tail_budget_as_env():
    client = FakeDaytonaClient()
    runtime = _CountingRuntime([], files=_good_files())
    await ac.run_cohort(client, runtime, **_kwargs())
    (sbx,) = client.sandboxes.values()
    assert sbx["env"]["ODDISH_QUERY_TRAJ_TAIL_BYTES"] == str(ac.TRAJ_TAIL_BYTES)


async def test_map_prompt_demands_raw_json_in_the_findings_file():
    # _findings_from_jsonl does json.loads(line), so a "MAP FINDING:"-prefixed
    # line is silently dropped. The n=8 prod run emitted exactly 8 "skipping
    # unparseable finding line" warnings for an 8-trial cohort -- the agent had
    # written the prefix into the file and every finding survived only via the
    # stream fallback. Batching makes that file load-bearing across turns, so
    # the two forms must be spelled out as distinct.
    p = ap.build_map_batch_prompt("bad", COHORT, ROSTER, {}, 1, 1, 8000)
    assert "NO `MAP FINDING:` prefix" in p
    # Batch 1 writes to its OWN file: a shared file made correctness depend on
    # every agent picking `>>` over `>`, and a real run showed they do not.
    assert findings_path(1) in p


async def test_map_prompt_describes_the_real_truncation():
    # The CLI is tail-only now; claiming "head/tail at ~4KB" would tell the agent
    # it has seen the start of a run it never saw.
    p = ap.build_map_batch_prompt("bad", COHORT, ROSTER, {}, 1, 1, 8000)
    assert "LAST 8000 bytes" in p
    assert "head/tail" not in p


# --- per-batch findings files -------------------------------------------
# Batches shared one findings.jsonl and appended to it, so correctness rested on
# every agent choosing `>>` over `>`. A real 97-trial run used a truncating `>`
# 32 times: the file went 10 -> 30 -> 10 lines as later batches wiped earlier
# ones, 80 findings were emitted and 47 survived -- and nothing failed, because
# reduce synthesized happily from what was left. Per-batch files make an agent
# able to clobber only its own work.


async def test_each_batch_writes_its_own_findings_file():
    plan_size = 3
    paths = {ap.findings_path(i) for i in range(1, plan_size + 1)}
    assert len(paths) == plan_size, "batches must not share a findings path"
    for i in range(1, plan_size + 1):
        p = ap.build_map_batch_prompt("bad", COHORT, ROSTER, {}, i, plan_size, 8000)
        assert ap.findings_path(i) in p
        # Batch i must not be told about anyone else's file.
        for j in range(1, plan_size + 1):
            if j != i:
                assert ap.findings_path(j) not in p


async def test_host_concatenates_every_batch_file():
    # The host merges; the sandbox is never trusted to have done it.
    client = FakeDaytonaClient()
    cohort = _cohort_of(25)  # -> 3 batches
    hosts = {sa.trial_id: {"trajectory_link": sa.trajectory_link, "model": "m",
                           "classification": "reward_hacking", "subtype": "1a",
                           "task_id": "t", "task_path": "tasks/t"}
             for sa in cohort}

    def _line(trial_id):
        return (json.dumps({
            "trial_id": trial_id, "bucket": "bad", "subcategory": "1a",
            "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
            "headroom_signal": "h",
        }) + "\n").encode()

    # One finding per batch, in three separate files.
    files = {REDUCE_PATH: json.dumps({"bad_failure_content": "# Bad"}).encode()}
    for i, tid in enumerate(["t0", "t10", "t20"], start=1):
        files[ap.findings_path(i)] = _line(tid)
    runtime = _CountingRuntime([], files=files)

    findings, _, _ = await ac.run_cohort(client, runtime, **_kwargs(
        cohort=cohort, host_by_trial=hosts, oracle_by_trial={}))

    # All three survive: a shared file would have kept only the last writer's.
    assert sorted(f.trial_id for f in findings) == ["t0", "t10", "t20"]


async def test_a_batch_that_wrote_nothing_costs_only_its_own_batch():
    client = FakeDaytonaClient()
    cohort = _cohort_of(25)
    hosts = {sa.trial_id: {"trajectory_link": sa.trajectory_link, "model": "m",
                           "classification": "reward_hacking", "subtype": "1a",
                           "task_id": "t", "task_path": "tasks/t"}
             for sa in cohort}
    files = {REDUCE_PATH: json.dumps({"bad_failure_content": "# Bad"}).encode()}
    # Batch 2 wrote nothing; 1 and 3 must still land.
    files[ap.findings_path(1)] = (json.dumps({
        "trial_id": "t0", "bucket": "bad", "subcategory": "1a",
        "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
        "headroom_signal": "h"}) + "\n").encode()
    files[ap.findings_path(3)] = (json.dumps({
        "trial_id": "t20", "bucket": "bad", "subcategory": "1a",
        "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
        "headroom_signal": "h"}) + "\n").encode()
    runtime = _CountingRuntime([], files=files)

    findings, _, _ = await ac.run_cohort(client, runtime, **_kwargs(
        cohort=cohort, host_by_trial=hosts, oracle_by_trial={}))
    assert sorted(f.trial_id for f in findings) == ["t0", "t20"]
