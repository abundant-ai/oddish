"""Offline tests for the crackbench harness.

Everything here runs with the fake LLM and mock solver — no API key, no network,
no oddish install — so the suite is a real end-to-end exercise of the loop.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from crackbench.config import HarnessConfig
from crackbench.harness import run, run_iteration, default_llm_factory
from crackbench.llm import FakeLLM, extract_json
from crackbench.materialize import write_task_dir
from crackbench.models import Checkpoint, GeneratedTask
from crackbench.solver import (
    MockSolver,
    OddishSolver,
    SolveResult,
    classify_long_horizon,
)
from crackbench.subagents import CheckpointGuidance, generate_tasks


def _cfg(tmp_path: Path, **kw) -> HarnessConfig:
    base = dict(
        design_dir=Path(__file__).resolve().parent.parent / "design",
        out_dir=tmp_path / "runs",
        max_iterations=3,
        seed=0,
    )
    base.update(kw)
    return HarnessConfig(**base)


# --- JSON extraction --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"tasks": []}',
        'here you go:\n```json\n{"tasks": []}\n```\nthanks',
        'prefix {"tasks": []} suffix',
    ],
)
def test_extract_json_variants(text):
    assert extract_json(text) == {"tasks": []}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here")


# --- fake generator + parsing ----------------------------------------------


def test_fake_llm_generates_requested_count():
    llm = FakeLLM(seed=1)
    guidance = CheckpointGuidance(text="- rule", sources=["x"])
    tasks = generate_tasks(
        llm, guidance, n=5, minutes=30, model="claude-fable-5", max_tokens=8000
    )
    assert len(tasks) == 5
    assert all(isinstance(t, GeneratedTask) for t in tasks)
    assert len({t.slug for t in tasks}) == 5  # no dup slugs in a batch
    for t in tasks:
        assert t.is_valid, t.validation_errors()
        assert t.checkpoints
        # dependency ids all resolve
        assert not any(
            dep not in {c.id for c in t.checkpoints}
            for c in t.checkpoints
            for dep in c.depends_on
        )


def test_generated_task_roundtrips():
    llm = FakeLLM(seed=3)
    tasks = generate_tasks(
        llm,
        CheckpointGuidance(text="", sources=[]),
        n=2,
        minutes=30,
        model="m",
        max_tokens=100,
    )
    for t in tasks:
        again = GeneratedTask.from_dict(t.to_dict())
        assert again.to_dict() == t.to_dict()


def test_validation_flags_bad_task():
    bad = GeneratedTask(
        slug="x",
        title="",
        category="pwn",
        difficulty="hard",
        summary="",
        instruction="",
        checkpoints=[Checkpoint(id="a", title="a", description="", verify_cmd="")],
    )
    errs = bad.validation_errors()
    assert "missing title" in errs
    assert "missing instruction body" in errs
    assert any("verify_cmd" in e for e in errs)


# --- solver + classifier ----------------------------------------------------


def test_classify_long_horizon():
    lh, _ = classify_long_horizon(
        SolveResult("brock", solved=False, minutes=5, reward=0.2),
        minutes_threshold=30,
    )
    assert lh  # failure => long-horizon regardless of time
    lh, _ = classify_long_horizon(
        SolveResult("brock", solved=True, minutes=45, reward=1.0),
        minutes_threshold=30,
    )
    assert lh  # solved but slow
    lh, _ = classify_long_horizon(
        SolveResult("brock", solved=True, minutes=10, reward=1.0),
        minutes_threshold=30,
    )
    assert not lh  # solved fast


def test_mock_solver_is_deterministic():
    llm = FakeLLM(seed=2)
    tasks = generate_tasks(
        llm, CheckpointGuidance("", []), n=5, minutes=30, model="m", max_tokens=100
    )
    s1 = MockSolver(seed=7)
    s2 = MockSolver(seed=7)
    for i, t in enumerate(tasks):
        r1 = s1.solve(t, iteration=1, index=i)
        r2 = s2.solve(t, iteration=1, index=i)
        assert (r1.solved, r1.minutes, r1.reward) == (r2.solved, r2.minutes, r2.reward)


def test_mock_solver_expected_minutes_orders_difficulty():
    s = MockSolver(seed=0)
    easy = GeneratedTask(
        slug="e", title="e", category="rev", difficulty="medium", summary="",
        instruction="i", checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    hard = GeneratedTask(
        slug="h", title="h", category="pwn", difficulty="expert", summary="",
        instruction="i",
        checkpoints=[Checkpoint(f"c{i}", "c", "", "true") for i in range(9)],
        techniques=["custom-vm", "rop", "aslr"],
    )
    assert s.expected_minutes(hard) > s.expected_minutes(easy)


def test_oddish_solver_errors_without_binary():
    solver = OddishSolver(oddish_bin="definitely-not-a-real-binary-xyz")
    task = GeneratedTask(
        slug="t", title="t", category="pwn", difficulty="hard", summary="",
        instruction="i", checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    with pytest.raises(RuntimeError, match="not found on PATH"):
        solver.solve(task, iteration=1, index=0)


# --- materialization + the generated verifier actually runs -----------------


def test_write_task_dir_layout(tmp_path):
    task = GeneratedTask(
        slug="demo", title="Demo", category="reverse-engineering", difficulty="hard",
        summary="s", instruction="do the thing",
        environment={"base_image": "ubuntu:24.04", "packages": ["gdb", "python3"]},
        checkpoints=[Checkpoint("cp-a", "A", "first", "true", 1.0)],
    )
    dest = write_task_dir(task, tmp_path / "demo", long_horizon_minutes=30)
    for rel in ("task.toml", "instruction.md", "environment/Dockerfile",
                "solution/README.md", "tests/test.sh", "tests/checkpoints.json"):
        assert (dest / rel).exists(), rel
    toml = (dest / "task.toml").read_text()
    assert 'name = "crackbench/demo"' in toml
    assert "max_timeout_sec" in toml
    # solution must not leak into the instruction
    assert "do the thing" in (dest / "instruction.md").read_text()


def test_generated_verifier_computes_dense_reward(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("present")  # cp-a will pass; cp-b will fail

    task = GeneratedTask(
        slug="v", title="V", category="pwn", difficulty="hard", summary="s",
        instruction="i",
        checkpoints=[
            Checkpoint("cp-a", "A", "", f'test -f "{ws}/a"', weight=1.0),
            Checkpoint("cp-b", "B", "", f'test -f "{ws}/b"', weight=3.0, depends_on=["cp-a"]),
        ],
    )
    dest = write_task_dir(task, tmp_path / "task", long_horizon_minutes=30)
    out = tmp_path / "verifier-out"
    proc = subprocess.run(
        ["bash", str(dest / "tests" / "test.sh")],
        capture_output=True, text=True,
        env={"CRACKBENCH_VERIFIER_OUT": str(out), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    reward = (out / "reward.txt").read_text().strip()
    assert reward == "0.25"  # 1 of (1+3) weight
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["checkpoints_passed"] == 1
    assert metrics["checkpoints_total"] == 2
    ctrf = json.loads((out / "ctrf.json").read_text())
    assert ctrf["results"]["summary"]["passed"] == 1
    assert ctrf["results"]["summary"]["failed"] == 1


# --- full loop --------------------------------------------------------------


def test_run_end_to_end_offline(tmp_path):
    cfg = _cfg(tmp_path, max_iterations=3)
    logs: list[str] = []
    result = run(cfg, log=logs.append)

    assert len(result.iterations) == 3
    for it in result.iterations:
        assert len(it.evaluations) == cfg.tasks_per_iteration
    # artifacts written
    assert (cfg.out_dir / "summary.json").exists()
    assert (cfg.out_dir / "summary.md").exists()
    assert (cfg.out_dir / "iteration-01.json").exists()
    # every accepted task is long-horizon and was materialized
    assert len(result.accepted) == sum(it.long_horizon_count for it in result.iterations)
    accepted_dir = cfg.out_dir / "accepted"
    if result.accepted:
        assert accepted_dir.exists()
        assert any(accepted_dir.iterdir())
    # context is cleared each iteration
    assert any("cleared subagent context" in m for m in logs)


def test_context_reset_makes_iterations_independent(tmp_path):
    """Fresh subagents per iteration => different batches (not a repeat of iter 1)."""
    cfg = _cfg(tmp_path, max_iterations=3)
    factory = default_llm_factory(cfg)
    solver = MockSolver(seed=cfg.seed, long_horizon_minutes=cfg.long_horizon_minutes)
    slugs_per_iter = []
    for i in (1, 2, 3):
        it, _ = run_iteration(
            cfg, i, llm_factory=factory, solver=solver, log=lambda *_: None
        )
        slugs_per_iter.append(tuple(e.task.slug for e in it.evaluations))
    assert len(set(slugs_per_iter)) == 3  # all three batches differ


def test_run_is_reproducible(tmp_path):
    r1 = run(_cfg(tmp_path / "a"), log=lambda *_: None)
    r2 = run(_cfg(tmp_path / "b"), log=lambda *_: None)
    assert [t.slug for t in r1.accepted] == [t.slug for t in r2.accepted]


def test_target_stops_early(tmp_path):
    cfg = _cfg(tmp_path, max_iterations=10, target_long_horizon=2)
    result = run(cfg, log=lambda *_: None)
    assert len(result.accepted) >= 2
    # should not have run all 10 iterations to collect just 2
    assert len(result.iterations) < 10
