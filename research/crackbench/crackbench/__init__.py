"""crackbench — an auto-research harness for generating long-horizon,
CrackMeBench-style cybersecurity tasks.

The harness iterates: each round spins up *fresh* subagents (no carried-over
context), has Haiku subagents read design docs to derive deterministic
checkpoints, has one Fable subagent generate a batch of tasks built around those
checkpoints, then scores each task with a baseline "Brock" solver. A round
"passes" when at least ``min_long_horizon`` of the batch are long-horizon — the
solver either fails them or spends >= ``long_horizon_minutes``.

See ``README.md`` for the full design and the interpretation of the original
request.
"""

from __future__ import annotations

from .config import HarnessConfig
from .models import (
    Checkpoint,
    GeneratedTask,
    IterationResult,
    RunResult,
    SolveResult,
    TaskEvaluation,
)

__all__ = [
    "HarnessConfig",
    "Checkpoint",
    "GeneratedTask",
    "SolveResult",
    "TaskEvaluation",
    "IterationResult",
    "RunResult",
]

__version__ = "0.1.0"
