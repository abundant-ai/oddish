"""The baseline solver ("Brock") and the long-horizon classifier.

A task is *long-horizon* when the baseline solver either fails it or takes at
least ``long_horizon_minutes`` (default 30). Two solvers implement the same
protocol:

- ``MockSolver`` — a seeded, offline difficulty heuristic. It never runs code, so
  the whole harness is runnable and reproducible with no infra. Use it for
  development, tests, and dry runs.
- ``OddishSolver`` — materializes each task and actually runs it through
  ``oddish run`` with a real agent, reading back solve time and reward. This is the
  authoritative measurement; it needs oddish credentials and is best-effort until
  validated against a live deployment.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from .models import GeneratedTask, SolveResult

_DIFFICULTY_BASE = {"easy": 10.0, "medium": 18.0, "hard": 38.0, "expert": 70.0}
_TECHNIQUE_BONUS = {
    "packing": 6.0,
    "anti-debug": 8.0,
    "custom-vm": 16.0,
    "bytecode": 8.0,
    "opaque-predicates": 6.0,
    "stripped-symbols": 4.0,
    "rop": 10.0,
    "aslr": 6.0,
    "tcache": 6.0,
    "heap-overflow": 8.0,
    "keystream-reuse": 4.0,
    "custom-prng": 6.0,
    "parser-differential": 6.0,
    "memory-carving": 4.0,
    "staged-payload": 4.0,
}


class Solver(Protocol):
    name: str

    def solve(
        self, task: GeneratedTask, *, iteration: int, index: int
    ) -> SolveResult: ...


def classify_long_horizon(
    result: SolveResult, *, minutes_threshold: float
) -> tuple[bool, str]:
    """Long-horizon iff the solver failed or spent >= the threshold."""
    if not result.solved:
        return True, f"solver '{result.solver}' failed the task"
    if result.minutes >= minutes_threshold:
        return (
            True,
            f"solved but took {result.minutes:.1f} min "
            f">= {minutes_threshold:.0f} min threshold",
        )
    return (
        False,
        f"solved in {result.minutes:.1f} min "
        f"< {minutes_threshold:.0f} min threshold",
    )


def _rng(*parts: Any) -> "random.Random":  # noqa: F821 - see import below
    import random

    key = "|".join(str(p) for p in parts).encode("utf-8")
    seed = struct.unpack("<Q", hashlib.blake2b(key, digest_size=8).digest())[0]
    return random.Random(seed)


class MockSolver:
    """Offline, seeded difficulty heuristic. Deterministic given (seed, iter, idx)."""

    name = "brock-mock"

    def __init__(self, *, seed: int = 0, long_horizon_minutes: float = 30.0) -> None:
        self._seed = seed
        self._threshold = long_horizon_minutes

    def expected_minutes(self, task: GeneratedTask) -> float:
        base = _DIFFICULTY_BASE.get(task.difficulty.lower(), 30.0)
        n_cp = len(task.checkpoints)
        depth_bonus = 3.0 * max(0, n_cp - 3)
        tech_bonus = sum(_TECHNIQUE_BONUS.get(t.lower(), 2.0) for t in task.techniques)
        heuristic = base + depth_bonus + tech_bonus
        if task.estimated_solve_minutes is not None:
            # Trust the generator's estimate only partially; it is self-reported.
            heuristic = 0.6 * heuristic + 0.4 * float(task.estimated_solve_minutes)
        return max(2.0, heuristic)

    def solve(
        self, task: GeneratedTask, *, iteration: int, index: int
    ) -> SolveResult:
        rng = _rng(self._seed, iteration, index, task.slug)
        expected = self.expected_minutes(task)
        # Solve probability falls as expected effort rises (logistic).
        p_solve = 1.0 / (1.0 + math.exp((expected - 34.0) / 16.0))
        solved = rng.random() < p_solve
        if solved:
            # Actual time scatters around the expectation, floored at a few minutes.
            minutes = max(2.0, rng.gauss(expected * 0.85, expected * 0.25))
            reward = 1.0
            detail = f"solved (p_solve={p_solve:.2f}, expected~{expected:.0f}m)"
        else:
            # A failed run burns roughly the full budget and earns partial credit.
            minutes = max(self._threshold, rng.gauss(expected, expected * 0.15))
            reached = rng.randint(0, max(0, len(task.checkpoints) - 1))
            total_w = sum(c.weight for c in task.checkpoints) or 1.0
            reward = round(
                sum(c.weight for c in task.checkpoints[:reached]) / total_w, 3
            )
            detail = f"failed (p_solve={p_solve:.2f}, expected~{expected:.0f}m)"
        return SolveResult(
            solver=self.name,
            solved=solved,
            minutes=round(minutes, 1),
            reward=reward,
            detail=detail,
        )


class OddishSolver:
    """Authoritative solver: run each task through ``oddish run`` and read it back.

    Requires the ``oddish`` CLI on PATH and valid credentials (ODDISH_API_KEY).
    Best-effort JSON parsing until validated against a live deployment.
    """

    name = "brock-oddish"

    def __init__(
        self,
        *,
        agent: str = "claude-code",
        model: str = "anthropic/claude-sonnet-4-5",
        long_horizon_minutes: float = 30.0,
        workdir: Path | None = None,
        poll_interval_sec: float = 30.0,
        solve_reward_threshold: float = 1.0,
        oddish_bin: str = "oddish",
    ) -> None:
        self._agent = agent
        self._model = model
        self._threshold = long_horizon_minutes
        self._workdir = Path(workdir) if workdir else None
        self._poll = poll_interval_sec
        self._solve_reward = solve_reward_threshold
        self._bin = oddish_bin

    def _run_json(self, args: list[str], *, timeout: float) -> Any:
        proc = subprocess.run(
            [self._bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`{self._bin} {' '.join(args)}` failed ({proc.returncode}): "
                f"{proc.stderr.strip()[:400]}"
            )
        return json.loads(proc.stdout)

    def solve(
        self, task: GeneratedTask, *, iteration: int, index: int
    ) -> SolveResult:
        if shutil.which(self._bin) is None:
            raise RuntimeError(
                f"{self._bin!r} not found on PATH; use --solver mock or install oddish"
            )
        # Import here to avoid a hard dependency when only MockSolver is used.
        from .materialize import write_task_dir

        base = self._workdir or Path.cwd() / ".crackbench-solve"
        dest = base / f"iter{iteration}" / f"{index:02d}-{task.slug}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_task_dir(task, dest, long_horizon_minutes=self._threshold)

        submit = self._run_json(
            [
                "run",
                str(dest),
                "-a",
                self._agent,
                "-m",
                self._model,
                "--n-trials",
                "1",
                "--json",
            ],
            timeout=300,
        )
        task_id = _deep_find(submit, ("task_id", "id"))
        if not task_id:
            raise RuntimeError(f"could not find task id in oddish run output: {submit}")

        deadline = time.monotonic() + (self._threshold + 20) * 60
        snapshot: Any = None
        while time.monotonic() < deadline:
            snapshot = self._run_json(
                ["status", str(task_id), "--json"], timeout=120
            )
            if _is_terminal(snapshot):
                break
            time.sleep(self._poll)

        reward = _to_float(_deep_find(snapshot, ("reward",)))
        seconds = _to_float(
            _deep_find(snapshot, ("trajectory_duration_seconds", "duration_seconds"))
        )
        minutes = (seconds / 60.0) if seconds is not None else (self._threshold + 20)
        reward = reward if reward is not None else 0.0
        solved = reward >= self._solve_reward
        return SolveResult(
            solver=self.name,
            solved=solved,
            minutes=round(minutes, 1),
            reward=round(reward, 3),
            detail=f"task_id={task_id}",
        )


def _deep_find(obj: Any, keys: tuple[str, ...]) -> Any:
    """Depth-first search for the first matching key in nested dicts/lists."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in keys:
                if k in cur and cur[k] is not None:
                    return cur[k]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _is_terminal(snapshot: Any) -> bool:
    status = _deep_find(snapshot, ("status", "state", "harbor_stage"))
    if not isinstance(status, str):
        return False
    return status.lower() in {
        "completed",
        "complete",
        "success",
        "succeeded",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "terminal",
        "done",
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
