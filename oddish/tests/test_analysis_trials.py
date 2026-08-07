"""Analysis-trial invariants: briefs, importers' parsing, kind exclusions,
and (DB-gated) the QA barrier + single-enqueue CAS."""

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


def test_analysis_kinds():
    assert is_analysis_kind("qa")
    assert is_analysis_kind("analyzer_reduce")
    assert not is_analysis_kind("agent")
    assert not is_analysis_kind(None)


def test_qa_brief_carries_trials_prompts_and_schema():
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1", "t-2"],
        pre_trial_items=[{"id": "a1", "description": "leaky test"}],
    )
    assert "- t-1" in brief and "- t-2" in brief
    assert "qa_result.json" in brief
    assert "leaky test" in brief
    assert "GOOD_SUCCESS|BAD_SUCCESS" in brief
    for field in TaskVerdictModel.model_json_schema()["properties"]:
        assert field in brief


def test_audit_brief_names_output_file():
    brief = build_audit_brief(task_name="demo")
    assert "audit_result.json" in brief
    assert "Do not solve the task" in brief


def test_classification_parses_valid_analysis():
    parsed = _classification_from_analysis(
        {
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
    )
    assert parsed is not None
    assert parsed.classification.value == "BAD_FAILURE"


@pytest.mark.parametrize(
    "analysis",
    [
        {},
        {"classification": "NOT_A_LABEL"},
        {"classification": "BAD_FAILURE", "action_items": [{"bogus": True}]},
    ],
)
def test_classification_rejects_malformed_analysis(analysis):
    assert _classification_from_analysis(analysis) is None


def test_finding_drops_trials_outside_cohort_and_keeps_host_facts():
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
    kept = _finding_from(
        {"trial_id": "t-1", "bucket": "good", "subcategory": "c"}, "bad", host
    )
    assert kept is not None
    assert kept.bucket == "bad"
    assert kept.trajectory_link == "/tasks/x/probe/t-1"
    assert _finding_from({"trial_id": "t-9"}, "bad", host) is None


def test_map_batches_split_and_small_cohort_stays_whole():
    assert _batches(list(range(5))) == [[0, 1, 2, 3, 4]]
    split = _batches(list(range(25)))
    assert [len(b) for b in split] == [10, 10, 5]


def test_eligible_trial_scope_excludes_non_agent_kinds_by_default():
    clauses = EligibleTrialScope(membership=[]).clauses()
    assert any("kind" in str(c) for c in clauses)
    opted_in = EligibleTrialScope(membership=[], include_non_agent_kinds=True)
    assert not any("kind" in str(c) for c in opted_in.clauses())


def test_facet_rows_exclude_non_agent_kinds():
    kwargs = dict(org_id="org", agent="claude-code", model="m")
    assert facet_rows_for_trial(**kwargs)
    assert facet_rows_for_trial(**kwargs, trial_kind="qa") == set()
    assert facet_rows_for_trial(**kwargs, trial_kind="analyzer_map") == set()


# --- DB-gated: the barrier and the single-enqueue CAS ------------------------


@pytest.mark.asyncio
async def test_qa_barrier_and_single_enqueue():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import (
        TaskStatus,
        TrialStatus,
        VerdictStatus,
        get_session,
        init_db,
    )
    from oddish.db.models import TaskModel
    from oddish.queue import maybe_start_qa_stage
    from sqlalchemy import select

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-barrier-{run}"
    async with get_session() as session:
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
        for i, status in enumerate(
            (TrialStatus.SUCCESS, TrialStatus.RUNNING), start=1
        ):
            session.add(
                TrialModel(
                    id=f"{task_id}-{i}",
                    name=f"{task_id}-{i}",
                    task_id=task_id,
                    agent="claude-code",
                    provider="anthropic",
                    queue_key="q",
                    status=status,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    # A RETRYING/RUNNING agent trial holds the barrier closed.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is False
        await session.commit()

    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-2")
        trial.status = TrialStatus.SUCCESS
        await session.commit()

    # Barrier opens exactly once: the winner creates one QA trial, the loser
    # sees VERDICT_PENDING and no-ops.
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
        assert f"{task_id}-1" in brief and f"{task_id}-2" in brief

    # The QA trial itself never reopens the barrier or counts as pending.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, qa_trials[0].id) is False
