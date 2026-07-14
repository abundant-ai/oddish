"""Platform-shared, serializable analysis primitives.

These are the reusable currency other features (reports, verdicts, probes,
future sandboxed evals) consume. They are deliberately report-agnostic and
JSON round-trippable so they can be persisted or mounted into a sandbox.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def trajectory_link(task_id: str, trial_id: str) -> str:
    """Canonical FE deep link to a trial's trajectory view."""
    return f"/tasks/{task_id}/probe/{trial_id}"


@dataclass
class TrajectoryBundle:
    """One trajectory + the context needed to analyze and cite it."""

    trial_id: str
    task_id: str
    task_path: str
    agent: str | None
    model: str | None
    reward: float | None
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    logs: dict[str, Any] = field(default_factory=dict)
    trajectory_summary: dict[str, Any] | None = None
    # Present for bad-bucket trials; None otherwise.
    oracle_context: str | None = None
    # Pre-built by the host so downstream LLM steps copy it verbatim.
    trajectory_link: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrajectoryBundle":
        return cls(**d)


@dataclass
class SubAnalysis:
    """The reusable per-trial subanalysis unit (the 'subcategory analysis').

    Wraps the existing TrialClassificationModel fields plus citation metadata.
    """

    trial_id: str
    trajectory_link: str
    classification: str  # one of Classification enum values
    subtype: str
    evidence: str
    root_cause: str
    recommendation: str
    trajectory_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubAnalysis":
        return cls(**d)
