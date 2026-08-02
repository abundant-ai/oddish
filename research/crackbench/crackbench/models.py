"""Typed data structures for the harness, with plain-dict (JSON) round-tripping.

Kept dependency-free (stdlib dataclasses only) so the harness and its tests run
anywhere, with or without the ``anthropic`` SDK installed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")

VALID_DIFFICULTIES = ("medium", "hard", "expert")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "task"


@dataclass
class Checkpoint:
    """A single deterministically verifiable sub-goal of a task.

    ``verify_cmd`` is a shell command that must exit 0 iff the checkpoint is met.
    ``depends_on`` names prerequisite checkpoint ids; it is recorded for the
    future DAG but not yet enforced (deferred by design).
    """

    id: str
    title: str
    description: str
    verify_cmd: str
    weight: float = 1.0
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", data["id"])),
            description=str(data.get("description", "")),
            verify_cmd=str(data.get("verify_cmd", "false")),
            weight=float(data.get("weight", 1.0)),
            depends_on=[str(d) for d in data.get("depends_on", []) or []],
        )


@dataclass
class GeneratedTask:
    """A CrackMeBench-style task proposal emitted by the generator subagent."""

    slug: str
    title: str
    category: str
    difficulty: str
    summary: str
    instruction: str
    checkpoints: list[Checkpoint]
    environment: dict[str, Any] = field(default_factory=dict)
    techniques: list[str] = field(default_factory=list)
    solution_notes: str = ""
    estimated_solve_minutes: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checkpoints"] = [c.to_dict() for c in self.checkpoints]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeneratedTask":
        title = str(data.get("title") or data.get("slug") or "Untitled Task")
        checkpoints = [
            Checkpoint.from_dict(c) for c in data.get("checkpoints", []) or []
        ]
        est = data.get("estimated_solve_minutes")
        return cls(
            slug=slugify(str(data.get("slug") or title)),
            title=title,
            category=str(data.get("category", "reverse-engineering")),
            difficulty=str(data.get("difficulty", "hard")),
            summary=str(data.get("summary", "")),
            instruction=str(data.get("instruction", "")),
            checkpoints=checkpoints,
            environment=dict(data.get("environment", {}) or {}),
            techniques=[str(t) for t in data.get("techniques", []) or []],
            solution_notes=str(data.get("solution_notes", "")),
            estimated_solve_minutes=float(est) if est is not None else None,
        )

    def validation_errors(self) -> list[str]:
        """Structural problems that would make the task unusable (empty list = ok)."""
        errors: list[str] = []
        if not self.title.strip():
            errors.append("missing title")
        if not self.instruction.strip():
            errors.append("missing instruction body")
        if not self.checkpoints:
            errors.append("no checkpoints")
        for i, cp in enumerate(self.checkpoints):
            if not cp.verify_cmd.strip():
                errors.append(f"checkpoint[{i}] {cp.id!r} has empty verify_cmd")
            if cp.weight <= 0:
                errors.append(f"checkpoint[{i}] {cp.id!r} has non-positive weight")
        ids = [cp.id for cp in self.checkpoints]
        if len(set(ids)) != len(ids):
            errors.append("duplicate checkpoint ids")
        known = set(ids)
        for cp in self.checkpoints:
            for dep in cp.depends_on:
                if dep not in known:
                    errors.append(f"checkpoint {cp.id!r} depends on unknown {dep!r}")
        return errors

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors()


@dataclass
class SolveResult:
    """Outcome of running the baseline ("Brock") solver against a task."""

    solver: str
    solved: bool
    minutes: float
    reward: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskEvaluation:
    """A generated task plus its solver outcome and long-horizon verdict."""

    task: GeneratedTask
    solve: SolveResult
    long_horizon: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "solve": self.solve.to_dict(),
            "long_horizon": self.long_horizon,
            "reason": self.reason,
        }


@dataclass
class IterationResult:
    """Everything produced by one iteration of the loop."""

    index: int
    guidance_digest: str
    guidance_sources: list[str]
    evaluations: list[TaskEvaluation]

    @property
    def long_horizon_count(self) -> int:
        return sum(1 for e in self.evaluations if e.long_horizon)

    @property
    def long_horizon_tasks(self) -> list[GeneratedTask]:
        return [e.task for e in self.evaluations if e.long_horizon]

    def passed_gate(self, min_long_horizon: int) -> bool:
        return self.long_horizon_count >= min_long_horizon

    def to_dict(self, min_long_horizon: int) -> dict[str, Any]:
        return {
            "index": self.index,
            "guidance_digest": self.guidance_digest,
            "guidance_sources": self.guidance_sources,
            "generated": len(self.evaluations),
            "long_horizon_count": self.long_horizon_count,
            "passed_gate": self.passed_gate(min_long_horizon),
            "evaluations": [e.to_dict() for e in self.evaluations],
        }


@dataclass
class RunResult:
    """Aggregate result of a full harness run."""

    iterations: list[IterationResult]
    accepted: list[GeneratedTask]
    config: dict[str, Any]

    @property
    def iterations_passed(self) -> int:
        min_lh = int(self.config.get("min_long_horizon", 2))
        return sum(1 for it in self.iterations if it.passed_gate(min_lh))

    def to_dict(self) -> dict[str, Any]:
        min_lh = int(self.config.get("min_long_horizon", 2))
        return {
            "config": self.config,
            "iterations_run": len(self.iterations),
            "iterations_passed": self.iterations_passed,
            "accepted_long_horizon": len(self.accepted),
            "iterations": [it.to_dict(min_lh) for it in self.iterations],
            "accepted": [t.to_dict() for t in self.accepted],
        }
