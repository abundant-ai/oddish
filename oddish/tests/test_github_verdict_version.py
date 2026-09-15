"""GitHub summaries obtain provenance from the same query as task pages."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
)
from oddish.integrations.github.notifier import _build_task_summary
from oddish.integrations.github.formatter import (
    format_task_comment,
    format_experiment_comment,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accept", "reject"])
@pytest.mark.parametrize("graded_version", [None, 1, 2])
@pytest.mark.parametrize("pinned", [True, False])
async def test_github_summary_checks_published_verdict_version(
    session, outcome, graded_version, pinned
):
    suffix = uuid4().hex[:12]
    task = TaskModel(
        id=f"gh-task-{suffix}",
        name="Task",
        user="test",
        task_path="/tmp/task",
        verdict_status=VerdictStatus.SUCCESS,
    )
    experiment = ExperimentModel(id=f"gh-exp-{suffix}", name="Experiment")
    session.add_all([task, experiment])
    await session.flush()
    versions = [
        TaskVersionModel(
            id=f"{task.id}-v{n}",
            task_id=task.id,
            version=n,
            task_path=f"/tmp/task/v{n}",
        )
        for n in [1, 2]
    ]
    session.add_all(versions)
    await session.flush()
    task.current_version_id = versions[1].id
    qa_id = f"{task.id}-qa"
    task.verdict = {
        "verdict": outcome,
        "is_good": outcome == "accept",
        "primary_issue": "Published issue",
        "recommendations": ["Published fix"],
    }
    if pinned:
        task.verdict = {**task.verdict, "_graded_by": qa_id}
    if graded_version is not None:
        session.add(
            TrialModel(
                id=qa_id,
                name="Verdict generation",
                task_id=task.id,
                task_version_id=versions[graded_version - 1].id,
                experiment_id=experiment.id,
                kind="qa",
                agent="codex",
                provider="openai",
                model="gpt-5.5",
                queue_key="openai/gpt-5.5",
                status=TrialStatus.SUCCESS,
                finished_at=datetime.now(timezone.utc),
            )
        )
    await session.flush()
    summary = await _build_task_summary(session, task, experiment_id=experiment.id)
    assert summary.review_version_matches is (graded_version == 2)
    for body in [
        format_task_comment(summary, "Experiment", "https://example.test/exp"),
        format_experiment_comment([summary], "Experiment", "https://example.test/exp"),
    ]:
        if graded_version == 2:
            assert ("Accepted" if outcome == "accept" else "Rejected") in body
        else:
            assert "No verdict" in body
            assert "Accepted" not in body and "Rejected" not in body
            assert "Published issue" not in body and "Published fix" not in body
