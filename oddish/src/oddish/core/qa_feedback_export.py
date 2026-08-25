"""Read-only selection of human-reviewed QA trials for offline benchmarks."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oddish.analyze.trajectory_taxonomy import SCHEMA_VERSION
from oddish.db import (
    AGENT_TRIAL_KIND,
    AnalysisStatus,
    FeedbackModel,
    TrialModel,
    TrialStatus,
)


class QaFeedbackExportItem(BaseModel):
    """One human label tied to the exact currently-published QA run."""

    trial_id: str
    grader_trial_id: str
    task_id: str
    task_version_id: str | None
    experiment_id: str
    classification: str
    human_vote: str
    review_note: str
    reviewed_at: datetime
    vote_count: int
    reward: float | None
    solver_agent: str
    solver_model: str | None
    judge_agent: str
    judge_model: str | None


class QaFeedbackExportResponse(BaseModel):
    requested_limit: int
    eligible_total: int
    returned_count: int
    items: list[QaFeedbackExportItem]


def build_qa_feedback_export_statement(*, org_id: str, limit: int):
    """Build the Postgres query used by the hosted operator export route.

    A feedback row does not snapshot the QA trial that produced the reviewed
    classification. The current trial analysis does carry ``_graded_by``. To
    avoid pairing a vote with a later replacement QA run, this query accepts a
    vote only when it was created after the current analysis finished and its
    ``target_key`` still matches the current classification.
    """

    grader = aliased(TrialModel, name="grader")
    grader_id = TrialModel.analysis["_graded_by"].astext
    classification = TrialModel.analysis["classification"].astext

    reviewed = (
        select(
            FeedbackModel.trial_id.label("trial_id"),
            FeedbackModel.experiment_id.label("experiment_id"),
            FeedbackModel.target_key.label("classification"),
            FeedbackModel.vote.label("human_vote"),
            FeedbackModel.body.label("review_note"),
            FeedbackModel.created_at.label("reviewed_at"),
            TrialModel.task_id.label("task_id"),
            TrialModel.task_version_id.label("task_version_id"),
            TrialModel.reward.label("reward"),
            TrialModel.agent.label("solver_agent"),
            TrialModel.model.label("solver_model"),
            grader_id.label("grader_trial_id"),
            func.row_number()
            .over(
                partition_by=FeedbackModel.trial_id,
                order_by=(FeedbackModel.created_at.desc(), FeedbackModel.id.desc()),
            )
            .label("review_rank"),
        )
        .join(TrialModel, TrialModel.id == FeedbackModel.trial_id)
        .where(
            FeedbackModel.org_id == org_id,
            FeedbackModel.target == "qa_verdict",
            FeedbackModel.created_at >= TrialModel.analysis_finished_at,
            FeedbackModel.target_key == classification,
            TrialModel.org_id == org_id,
            TrialModel.kind == AGENT_TRIAL_KIND,
            TrialModel.status == TrialStatus.SUCCESS,
            TrialModel.analysis_status == AnalysisStatus.SUCCESS,
            TrialModel.analysis.is_not(None),
            TrialModel.analysis_finished_at.is_not(None),
            TrialModel.trajectory_summary.is_not(None),
            TrialModel.trajectory_summary["schema_version"].astext == SCHEMA_VERSION,
            TrialModel.has_trajectory.is_(True),
            TrialModel.is_probe.is_(False),
            TrialModel.superseded_by_trial_id.is_(None),
            grader_id.is_not(None),
        )
        .cte("reviewed")
    )

    consensus = (
        select(
            reviewed.c.trial_id,
            func.count().label("vote_count"),
            func.count(func.distinct(reviewed.c.human_vote)).label("distinct_votes"),
        )
        .group_by(reviewed.c.trial_id)
        .cte("consensus")
    )

    eligible = (
        select(
            reviewed.c.trial_id,
            reviewed.c.grader_trial_id,
            reviewed.c.task_id,
            reviewed.c.task_version_id,
            reviewed.c.experiment_id,
            reviewed.c.classification,
            reviewed.c.human_vote,
            reviewed.c.review_note,
            reviewed.c.reviewed_at,
            consensus.c.vote_count,
            reviewed.c.reward,
            reviewed.c.solver_agent,
            reviewed.c.solver_model,
            grader.agent.label("judge_agent"),
            grader.model.label("judge_model"),
        )
        .join(consensus, consensus.c.trial_id == reviewed.c.trial_id)
        .join(grader, grader.id == reviewed.c.grader_trial_id)
        .where(
            reviewed.c.review_rank == 1,
            consensus.c.distinct_votes == 1,
            grader.org_id == org_id,
            grader.task_id == reviewed.c.task_id,
            grader.kind == "qa",
            grader.status == TrialStatus.SUCCESS,
            grader.has_trajectory.is_(True),
            grader.trajectory_summary.is_not(None),
            grader.trajectory_summary["schema_version"].astext == SCHEMA_VERSION,
            grader.superseded_by_trial_id.is_(None),
        )
        .subquery("eligible")
    )

    return (
        select(eligible, func.count().over().label("eligible_total"))
        .order_by(eligible.c.reviewed_at.desc(), eligible.c.trial_id)
        .limit(limit)
    )


async def export_qa_feedback_core(
    session: AsyncSession, *, org_id: str, limit: int
) -> QaFeedbackExportResponse:
    rows = (
        (
            await session.execute(
                build_qa_feedback_export_statement(org_id=org_id, limit=limit)
            )
        )
        .mappings()
        .all()
    )
    eligible_total = int(rows[0]["eligible_total"]) if rows else 0
    items = [
        QaFeedbackExportItem.model_validate(
            {key: value for key, value in row.items() if key != "eligible_total"}
        )
        for row in rows
    ]
    return QaFeedbackExportResponse(
        requested_limit=limit,
        eligible_total=eligible_total,
        returned_count=len(items),
        items=items,
    )


__all__ = [
    "QaFeedbackExportItem",
    "QaFeedbackExportResponse",
    "build_qa_feedback_export_statement",
    "export_qa_feedback_core",
]
