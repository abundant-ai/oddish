import pytest

from oddish.core.analyzer_inputs import build_analyzer_inputs, subanalysis_from_trial
from oddish.db.models import AnalysisStatus


class FakeTrial:
    def __init__(self, tid, task_id, cls_, subtype, agent="claude-code",
                 status=AnalysisStatus.SUCCESS):
        self.id = tid
        self.task_id = task_id
        self.agent = agent
        self.model = "opus"
        self.reward = 0.0
        self.analysis_status = status
        self.analysis = (
            None if cls_ is None else
            {"classification": cls_, "subtype": subtype, "evidence": "e",
             "root_cause": "rc", "recommendation": "rec", "reward": 0.0}
        )
        self.trajectory_summary = {"summary": "s"}


def test_subanalysis_from_trial_success_and_skip():
    t = FakeTrial("task-0", "task", "BAD_FAILURE", "Hardcoding")
    sa = subanalysis_from_trial(t, "tasks/task")
    assert sa.classification == "BAD_FAILURE"
    assert sa.trajectory_link == "/tasks/task/probe/task-0"

    unanalyzed = FakeTrial("task-x", "task", None, None, status=AnalysisStatus.RUNNING)
    assert subanalysis_from_trial(unanalyzed, "tasks/task") is None


@pytest.mark.asyncio
async def test_build_inputs_reads_trajectory_only_for_failures():
    reads = []

    async def fake_read_traj(trial):
        reads.append(trial.id)
        return [{"step": 1}]

    async def fake_read_logs(trial):
        return {"verifier_stdout": "FAIL"}

    rows = [
        (FakeTrial("task-0", "task", "BAD_FAILURE", "Hardcoding", agent="oracle"), "tasks/task"),
        (FakeTrial("task-1", "task", "GOOD_FAILURE", "Wrong Approach"), "tasks/task"),
        (FakeTrial("task-2", "task", "GOOD_SUCCESS", "Correct Solution"), "tasks/task"),
    ]
    inputs = await build_analyzer_inputs(
        rows, read_trajectory=fake_read_traj, read_logs=fake_read_logs
    )
    # one bundle per trial (counts), subanalyses per classified trial
    assert len(inputs.bundles) == 3
    assert len(inputs.subanalyses) == 3
    # trajectory only read for the two failure trials, not the success
    assert set(reads) == {"task-0", "task-1"}
    # oracle-agent bad trial carries oracle context
    bad = next(b for b in inputs.bundles if b.trial_id == "task-0")
    assert bad.oracle_context and "oracle" in bad.oracle_context.lower()
    good = next(b for b in inputs.bundles if b.trial_id == "task-2")
    assert good.trajectory == []  # stub, no read


def test_subanalysis_carries_the_trial_model():
    t = FakeTrial("task-0", "task", "BAD_FAILURE", "Hardcoding")
    t.model = "global.anthropic.claude-opus-4-8-v1:0"
    sa = subanalysis_from_trial(t, "tasks/task")
    # Stored verbatim: normalization is the aggregation layer's job, and the raw
    # value is what tells a Bedrock-routed trial from a direct-API one.
    assert sa.model == "global.anthropic.claude-opus-4-8-v1:0"


def test_subanalysis_model_none_when_trial_has_no_model():
    t = FakeTrial("task-0", "task", "BAD_FAILURE", "Hardcoding")
    t.model = None
    assert subanalysis_from_trial(t, "tasks/task").model is None
