"""Tests for analysis trials.

Each test checks one rule. The rule is in the test name and the first line.
"""

import os
import uuid

import pytest

from oddish.analyze.models import TaskVerdictModel
from oddish.db.models import TrialModel
from oddish.filters.trial_predicates import EligibleTrialScope
from oddish.core.trial_facets import facet_rows_for_trial
from oddish.workers.analysis_trials import (
    _classification_from_analysis,
    build_audit_brief,
    build_qa_brief,
    is_analysis_kind,
)
from oddish.workers.analyzer_trials import _batches, _finding_from

URL = os.environ.get("ODDISH_DATABASE_URL")

GOOD_ANALYSIS = {
    "trial_name": "t-1",
    "classification": "BAD_FAILURE",
    "subtype": "Verifier bug",
    "evidence": "e",
    "root_cause": "r",
    "recommendation": "x",
    "reward": 0.0,
    "action_items": [],
    "exploitation": [],
}


def test_the_four_analysis_kinds_are_known():
    """qa, audit, and the two analyzer kinds are analysis kinds. agent is not."""
    for kind in ("qa", "audit", "analyzer_map", "analyzer_reduce"):
        assert is_analysis_kind(kind)
    assert not is_analysis_kind("agent")
    assert not is_analysis_kind(None)


def test_the_qa_brief_tells_the_agent_everything_it_needs():
    """The brief must name each trial, the output file, the labels, and the
    verdict fields. If one is missing, the QA agent cannot do its job."""
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1", "t-2"],
        pre_trial_items=[{"id": "a1", "description": "leaky test"}],
    )
    assert "- t-1" in brief
    assert "- t-2" in brief
    assert "qa_result.json" in brief
    assert "leaky test" in brief
    assert "GOOD_SUCCESS|BAD_SUCCESS" in brief
    for field in TaskVerdictModel.model_json_schema()["properties"]:
        assert field in brief


def test_the_audit_brief_names_its_output_file():
    """The audit agent must know where to write, and must not solve the task."""
    brief = build_audit_brief(task_name="demo")
    assert "audit_result.json" in brief
    assert "Do not solve the task" in brief


def test_the_overlay_replaces_the_verifier_with_the_artifact_check(tmp_path):
    """An analysis trial must not run the audited task's verifier. Its own
    verifier stages /logs/<artifact> under the collected verifier dir and
    fails when the file is missing, so trial retries re-run the agent."""
    from oddish.worker.probe_staging import apply_analysis_overlay

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "expensive_llm_judge.py").write_text("x")
    (tmp_path / "instruction.md").write_text("original")
    apply_analysis_overlay(tmp_path, brief="the brief", artifact="qa_result.json")

    assert (tmp_path / "instruction.md").read_text() == "the brief"
    assert not (tmp_path / "tests" / "expensive_llm_judge.py").exists()
    test_sh = (tmp_path / "tests" / "test.sh").read_text()
    assert "/logs/qa_result.json" in test_sh
    assert "exit 1" in test_sh
    assert 'cp "$SRC" "$OUT/qa_result.json"' in test_sh
    # An empty JSON object must not earn reward: the verifier requires the
    # keys the importer reads.
    assert 'KEYS="trials verdict"' in test_sh


def test_a_correct_analysis_is_accepted():
    """A well-formed analysis from the QA agent parses into a classification."""
    parsed = _classification_from_analysis(GOOD_ANALYSIS)
    assert parsed is not None
    assert parsed.classification.value == "BAD_FAILURE"


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"classification": "NOT_A_LABEL"},
        {"classification": "BAD_FAILURE", "action_items": [{"bogus": True}]},
    ],
)
def test_a_broken_analysis_is_rejected_not_stored(broken):
    """A malformed analysis must parse to None. It must never reach the DB."""
    assert _classification_from_analysis(broken) is None


def test_a_finding_for_an_unknown_trial_is_dropped():
    """The agent can only report on trials the host gave it. Anything else
    is dropped."""
    host = {
        "t-1": {
            "trajectory_link": "/tasks/x/probe/t-1",
            "model": "m",
            "classification": "BAD_FAILURE",
            "subtype": "s",
            "task_id": "x",
            "task_path": "p",
        }
    }
    assert _finding_from({"trial_id": "t-9"}, "bad", host) is None


def test_host_facts_overwrite_what_the_agent_wrote():
    """The link and the bucket come from the host, never from the agent.
    An agent cannot forge them."""
    host = {
        "t-1": {
            "trajectory_link": "/tasks/x/probe/t-1",
            "model": "m",
            "classification": "BAD_FAILURE",
            "subtype": "s",
            "task_id": "x",
            "task_path": "p",
        }
    }
    finding = _finding_from(
        {"trial_id": "t-1", "bucket": "good", "trajectory_link": "/fake"},
        "bad",
        host,
    )
    assert finding.bucket == "bad"
    assert finding.trajectory_link == "/tasks/x/probe/t-1"


def test_big_cohorts_split_into_batches_of_ten():
    """One map agent gets at most ten trials. A small cohort stays whole."""
    assert _batches(list(range(5))) == [[0, 1, 2, 3, 4]]
    assert [len(b) for b in _batches(list(range(25)))] == [10, 10, 5]


def test_trial_filters_hide_analysis_trials_by_default():
    """Every surface that uses the shared filter sees agent trials only,
    unless it opts in."""
    default = EligibleTrialScope(membership=[]).clauses()
    assert any("kind" in str(c) for c in default)
    opted_in = EligibleTrialScope(membership=[], include_non_agent_kinds=True)
    assert not any("kind" in str(c) for c in opted_in.clauses())


def test_browse_filters_never_learn_analysis_trial_values():
    """A QA trial must not add its agent or model to the browse dropdowns."""
    kwargs = dict(org_id="org", agent="claude-code", model="m")
    assert facet_rows_for_trial(**kwargs)
    assert facet_rows_for_trial(**kwargs, trial_kind="qa") == set()
    assert facet_rows_for_trial(**kwargs, trial_kind="analyzer_map") == set()


@pytest.mark.asyncio
async def test_a_task_gets_exactly_one_qa_trial():
    """Needs a database. Checks three rules in order:
    1. QA does not start while an agent trial still runs.
    2. When the last agent trial ends, exactly one QA trial appears,
       even if two workers race.
    3. The QA trial itself never triggers more QA."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import (
        TaskStatus,
        TrialStatus,
        VerdictStatus,
        get_session,
        init_db,
    )
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.queue import maybe_start_qa_stage
    from sqlalchemy import select, text

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-barrier-{run}"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW())"
            ),
            {"t": task_id, "e": experiment.id},
        )
        for i, status in enumerate(
            (TrialStatus.SUCCESS, TrialStatus.RUNNING), start=1
        ):
            session.add(
                TrialModel(
                    id=f"{task_id}-{i}",
                    name=f"{task_id}-{i}",
                    task_id=task_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    status=status,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    # Rule 1: one trial still runs, so QA does not start.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is False
        await session.commit()

    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-2")
        trial.status = TrialStatus.SUCCESS
        await session.commit()

    # Rule 2: all trials are done. The first caller starts QA. The second
    # caller sees QA already started and does nothing.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is True
        await session.commit()
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is False
        await session.commit()

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.VERDICT_PENDING
        assert task.verdict_status == VerdictStatus.QUEUED
        qa_trials = (
            (
                await session.execute(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qa_trials) == 1
        brief = qa_trials[0].harbor_config["extra_instructions"]
        assert f"{task_id}-1" in brief
        assert f"{task_id}-2" in brief

    # Rule 3: the QA trial is not an agent trial, so it triggers nothing.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, qa_trials[0].id) is False
