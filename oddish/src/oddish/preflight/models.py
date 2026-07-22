from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from harbor.models.task.config import TaskConfig


class Severity(StrEnum):
    """How much a finding matters.

    ERROR blocks a run; WARN is printed and ignored. The split exists so
    "you have no .dockerignore" is not shouted at the same volume as
    "your image contains the answer".
    """

    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True)
class Finding:
    check_id: str
    severity: Severity
    task_dir: Path
    message: str
    path: Path | None = None
    line: int | None = None
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "severity": self.severity.value,
            "task_dir": str(self.task_dir),
            "path": str(self.path) if self.path is not None else None,
            "line": self.line,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


# A check never prints, never exits, and never touches the network: it maps a
# task directory and its parsed config to findings. That purity is what lets
# the same bodies serve the CLI, `oddish run`, and external CI.
CheckFn = Callable[[Path, TaskConfig], list["Finding"]]


@dataclass(frozen=True)
class Check:
    id: str
    description: str
    fn: CheckFn
