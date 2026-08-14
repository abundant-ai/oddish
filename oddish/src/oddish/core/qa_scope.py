"""Canonical trial scope and evidence identities for task-level QA.

Queue admission, the QA worker, and task review must agree on which rows are
eligible.  Keeping the SQL predicates here prevents a paid worker and a
read-only review from silently judging different cohorts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import and_, func, not_

from oddish.core.baseline_gate import GATE_SKIP_PREFIX, baseline_agent_clause
from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE
from oddish.db import TrialModel, TrialStatus


def live_same_version_trial_scope(task_id: str, task_version_id: str | None):
    """Return the shared live-row predicate for one immutable task version.

    Baselines remain in this base scope so review can report their gate
    evidence.  Probe, migration, and agent-role exclusions belong to the
    classification scope below.
    """

    return and_(
        TrialModel.task_id == task_id,
        (
            TrialModel.task_version_id == task_version_id
            if task_version_id is not None
            else True
        ),
        TrialModel.superseded_by_trial_id.is_(None),
        func.coalesce(TrialModel.harbor_stage, "") != CANCELLED_HARBOR_STAGE,
        TrialModel.status != TrialStatus.SKIPPED,
        func.coalesce(TrialModel.error_message, "").notlike(f"{GATE_SKIP_PREFIX}%"),
    )


def qa_classification_scope(task_id: str, task_version_id: str | None):
    """Return the exact model-trial cohort eligible for task-level QA."""

    return and_(
        qa_review_scope(task_id, task_version_id),
        not_(baseline_agent_clause(TrialModel.agent)),
    )


def qa_review_scope(task_id: str, task_version_id: str | None):
    """Return model and baseline rows eligible for the canonical QA review.

    The paid classifier adds only the agent-role exclusion.  Keeping the rest
    here lets the review's model counts and baseline evidence share the same
    version, retry, import, probe, cancellation, and gate-skip boundary.
    """

    return and_(
        live_same_version_trial_scope(task_id, task_version_id),
        # Bulk Sauron migrations are intentionally excluded from paid QA.
        # Small ad-hoc imports have ``imported_at IS NULL`` and stay eligible.
        TrialModel.imported_at.is_(None),
        TrialModel.is_probe.is_(False),
    )


def analysis_fingerprint(payload: Any) -> str:
    """Hash one stored analysis payload using the canonical review encoding."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def input_set_sha256(fingerprints: dict[str, str]) -> str:
    """Hash sorted trial/fingerprint pairs for one frozen QA input set."""

    pairs = sorted(fingerprints.items())
    encoded = json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "analysis_fingerprint",
    "input_set_sha256",
    "live_same_version_trial_scope",
    "qa_classification_scope",
    "qa_review_scope",
]
