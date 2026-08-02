"""Harness configuration.

All the knobs live here so the CLI, the library entry point, and the tests share
one source of truth. Defaults encode the request faithfully: batches of 5, a
2-of-5 long-horizon gate, and a 30-minute threshold (which also matches oddish's
own ``PROBE_AGENT_TIMEOUT_SEC``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Current model ids. Fable generates tasks; Haiku reads the design docs.
DEFAULT_FABLE_MODEL = "claude-fable-5"
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DESIGN_DIR = _PKG_ROOT / "design"
DEFAULT_OUT_DIR = _PKG_ROOT / "runs"


@dataclass
class HarnessConfig:
    # --- subagent models ---
    fable_model: str = DEFAULT_FABLE_MODEL
    haiku_model: str = DEFAULT_HAIKU_MODEL
    live: bool = False  # False => offline FakeLLM; True => real Anthropic API

    # --- the loop's acceptance test ---
    tasks_per_iteration: int = 5
    min_long_horizon: int = 2
    long_horizon_minutes: float = 30.0

    # --- loop control ---
    max_iterations: int = 5
    # Stop early once this many long-horizon tasks are collected. None => run all
    # iterations regardless.
    target_long_horizon: int | None = None
    # Re-read the design docs with fresh Haiku subagents every iteration (True) to
    # honor "start from scratch". Set cache_checkpoints=True to read them once.
    cache_checkpoints: bool = False

    # --- the "Brock" baseline solver ---
    solver: str = "mock"  # "mock" (offline heuristic) | "oddish" (real solve time)
    solver_agent: str = "claude-code"
    solver_model: str = "anthropic/claude-sonnet-4-5"

    # --- io & reproducibility ---
    design_dir: Path = field(default_factory=lambda: DEFAULT_DESIGN_DIR)
    out_dir: Path = field(default_factory=lambda: DEFAULT_OUT_DIR)
    seed: int = 0

    # --- token budgets for the subagents ---
    max_gen_tokens: int = 8000
    max_reader_tokens: int = 1500

    def __post_init__(self) -> None:
        self.design_dir = Path(self.design_dir)
        self.out_dir = Path(self.out_dir)
        if self.tasks_per_iteration < 1:
            raise ValueError("tasks_per_iteration must be >= 1")
        if not 1 <= self.min_long_horizon <= self.tasks_per_iteration:
            raise ValueError(
                "min_long_horizon must be between 1 and tasks_per_iteration"
            )
        if self.long_horizon_minutes <= 0:
            raise ValueError("long_horizon_minutes must be > 0")
        if self.solver not in ("mock", "oddish"):
            raise ValueError(f"unknown solver {self.solver!r}")

    def summary(self) -> dict[str, Any]:
        """JSON-safe snapshot for embedding in run artifacts."""
        return {
            "fable_model": self.fable_model,
            "haiku_model": self.haiku_model,
            "live": self.live,
            "tasks_per_iteration": self.tasks_per_iteration,
            "min_long_horizon": self.min_long_horizon,
            "long_horizon_minutes": self.long_horizon_minutes,
            "max_iterations": self.max_iterations,
            "target_long_horizon": self.target_long_horizon,
            "cache_checkpoints": self.cache_checkpoints,
            "solver": self.solver,
            "solver_agent": self.solver_agent,
            "solver_model": self.solver_model,
            "design_dir": str(self.design_dir),
            "out_dir": str(self.out_dir),
            "seed": self.seed,
        }
