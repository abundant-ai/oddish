"""Offline tests for the crackbench harness.

Everything here runs with the fake LLM and mock solver — no API key, no network,
no oddish install — so the suite is a real end-to-end exercise of the loop.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from crackbench.config import HarnessConfig
from crackbench.harness import run, run_iteration, default_llm_factory
from crackbench.llm import FakeLLM, extract_json
from crackbench.materialize import render_dockerfile, render_task_toml, write_task_dir
from crackbench.models import Checkpoint, GeneratedTask
from crackbench.solver import (
    MockSolver,
    OddishSolver,
    SolveResult,
    classify_long_horizon,
    classify_oddish_snapshot,
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


def test_extract_json_skips_leading_non_json_braces():
    # a non-JSON brace pair before the real payload must not abort the scan
    assert extract_json('reasoning {not: json} then\n{"tasks": [1]}') == {"tasks": [1]}
    assert extract_json("junk [not, json] then [1, 2, 3]") == [1, 2, 3]


def test_extract_json_returns_prose_wrapped_top_level_array():
    # a top-level array must be returned whole, not misread as its first object
    text = 'here are the tasks:\n[{"slug": "a"}, {"slug": "b"}]'
    assert extract_json(text) == [{"slug": "a"}, {"slug": "b"}]


def test_extract_json_ignores_earlier_non_json_fence():
    # a shell fence before the real JSON fence must not trap extraction
    text = (
        "first, a shell example:\n```bash\nls -la\n```\n"
        'then the answer:\n```json\n{"tasks": [1]}\n```'
    )
    assert extract_json(text) == {"tasks": [1]}


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


def test_generate_tasks_trims_to_requested_n():
    """A model that over-produces must not inflate the gate's denominator."""

    class OverProducer:
        def complete(self, *, system, prompt, model, max_tokens, temperature=1.0):
            tasks = [
                {"slug": f"t{i}", "title": f"T{i}", "instruction": "i",
                 "checkpoints": [{"id": "a", "verify_cmd": "true"}]}
                for i in range(7)
            ]
            return json.dumps({"tasks": tasks})

    got = generate_tasks(
        OverProducer(), CheckpointGuidance("", []), n=5, minutes=30,
        model="m", max_tokens=100,
    )
    assert len(got) == 5


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
    # oddish requires [agent].timeout_sec specifically (not max_timeout_sec).
    assert "[agent]\ntimeout_sec = " in toml
    # solution must not leak into the instruction
    assert "do the thing" in (dest / "instruction.md").read_text()


def test_task_toml_satisfies_oddish_timeout_contract(tmp_path):
    """Mirror oddish.task_timeouts.validate_task_timeout_config's required fields.

    oddish rejects a task at submit time unless task.toml declares positive
    [agent].timeout_sec, [environment].build_timeout_sec, and [verifier].timeout_sec.
    """
    task = GeneratedTask(
        slug="c", title="C", category="pwn", difficulty="expert", summary="s",
        instruction="i", checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    dest = write_task_dir(task, tmp_path / "c", long_horizon_minutes=45)
    cfg = tomllib.loads((dest / "task.toml").read_text())
    for section, field in (
        ("agent", "timeout_sec"),
        ("environment", "build_timeout_sec"),
        ("verifier", "timeout_sec"),
    ):
        assert field in cfg.get(section, {}), f"[{section}].{field} missing"
        value = cfg[section][field]
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        assert value > 0, f"[{section}].{field} must be positive"


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


# --- review-fix regressions -------------------------------------------------


def test_task_toml_declares_no_network(tmp_path):
    task = GeneratedTask(
        slug="n", title="N", category="pwn", difficulty="hard", summary="s",
        instruction="i", checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    dest = write_task_dir(task, tmp_path / "n", long_horizon_minutes=30)
    cfg = tomllib.loads((dest / "task.toml").read_text())
    # CrackMeBench discipline; oddish closed_internet preflight requires it.
    assert cfg["environment"]["network_mode"] == "no-network"


def test_verifier_fails_closed_on_empty_verify_cmd(tmp_path):
    task = GeneratedTask(
        slug="e", title="E", category="pwn", difficulty="hard", summary="s",
        instruction="i",
        checkpoints=[
            Checkpoint("cp-empty", "E", "", "", weight=1.0),
            Checkpoint("cp-ok", "O", "", "true", weight=1.0),
        ],
    )
    dest = write_task_dir(task, tmp_path / "e", long_horizon_minutes=30)
    out = tmp_path / "out"
    proc = subprocess.run(
        ["bash", str(dest / "tests" / "test.sh")],
        capture_output=True, text=True,
        env={"CRACKBENCH_VERIFIER_OUT": str(out), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    # empty verify_cmd must NOT pass (bash -c "" would exit 0): reward = 1 of 2
    assert (out / "reward.txt").read_text().strip() == "0.5"
    by_id = {c["id"]: c for c in json.loads((out / "metrics.json").read_text())["checkpoints"]}
    assert by_id["cp-empty"]["passed"] is False
    assert by_id["cp-ok"]["passed"] is True


class _MixedBatchLLM:
    """Reader role -> canned rules; generator role -> 4 valid + 1 invalid task."""

    def complete(self, *, system, prompt, model, max_tokens, temperature=1.0):
        from crackbench.llm import ROLE_GENERATOR

        if ROLE_GENERATOR not in system:
            return "- checkpoints settled by an executable oracle"
        tasks = [
            {"slug": f"ok{i}", "title": f"Ok{i}", "instruction": "do it",
             "checkpoints": [{"id": "a", "verify_cmd": "true"}]}
            for i in range(4)
        ]
        tasks.append({"slug": "bad", "title": "Bad", "instruction": "do it",
                      "checkpoints": []})  # invalid: no checkpoints
        return json.dumps({"tasks": tasks})


def test_oddish_snapshot_missing_duration_uses_wall_clock():
    # solved with no trajectory duration must use measured wall-clock, not a
    # constant that would force every such success over the threshold.
    solved, minutes, reward = classify_oddish_snapshot(
        {"reward": 1.0}, elapsed_min=3.0, solve_reward=1.0
    )
    assert solved and reward == 1.0 and minutes == 3.0
    lh, _ = classify_long_horizon(
        SolveResult("brock-oddish", solved, minutes, reward), minutes_threshold=30
    )
    assert not lh  # a fast success must not be classified long-horizon
    # a reported trajectory duration wins over wall-clock
    _, minutes2, _ = classify_oddish_snapshot(
        {"reward": 1.0, "trajectory_duration_seconds": 2400},
        elapsed_min=3.0, solve_reward=1.0,
    )
    assert minutes2 == 40.0


def test_fake_llm_reproducible_across_instances():
    def batch(seed):
        return generate_tasks(
            FakeLLM(seed=seed), CheckpointGuidance("", []), n=5, minutes=30,
            model="m", max_tokens=100,
        )

    assert [t.to_dict() for t in batch(5)] == [t.to_dict() for t in batch(5)]
    assert [t.slug for t in batch(5)] != [t.slug for t in batch(6)]


def test_dockerfile_bootstraps_pip_for_pwntools():
    task = GeneratedTask(
        slug="p", title="P", category="pwn", difficulty="expert", summary="s",
        instruction="i",
        environment={"base_image": "ubuntu:24.04",
                     "packages": ["gdb", "python3", "pwntools"]},
        checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    df = render_dockerfile(task)
    assert "python3-pip" in df  # interpreter+pip bootstrapped before pip install
    assert "--break-system-packages" in df  # ubuntu:24.04 is PEP 668 managed
    assert "pwntools" in df


def test_dockerfile_always_installs_python3():
    # the verifier runs a python3 heredoc, so python3 must be present even when
    # the task declares no python packages.
    task = GeneratedTask(
        slug="r", title="R", category="reverse-engineering", difficulty="hard",
        summary="s", instruction="i",
        environment={"base_image": "ubuntu:24.04", "packages": ["gdb"]},
        checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    assert "python3" in render_dockerfile(task)


def test_task_toml_escapes_llm_controlled_strings():
    # a category/difficulty carrying a quote or newline must still yield valid TOML
    task = GeneratedTask(
        slug="x", title="X", category='ret2libc "pwn"\ninjected = 1',
        difficulty="hard", summary="s", instruction="i",
        checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    parsed = tomllib.loads(render_task_toml(task, long_horizon_minutes=30))
    # Harbor reads a top-level [metadata] table, not [task.metadata].
    assert parsed["metadata"]["category"] == 'ret2libc "pwn"\ninjected = 1'
    assert "injected" not in parsed["metadata"]  # not smuggled as a key
    assert "metadata" not in parsed["task"]  # not nested under [task]


def test_exit_code_tracks_accepted_not_just_gate():
    from crackbench.cli import _exit_code

    # any long-horizon tasks produced => success, even if no iteration "passed"
    assert _exit_code(SimpleNamespace(accepted=["a"])) == 0
    assert _exit_code(SimpleNamespace(accepted=[])) == 2


def test_run_iteration_rejects_invalid_and_batch_gate(tmp_path):
    cfg = _cfg(tmp_path, tasks_per_iteration=5, min_long_horizon=2)
    it, _ = run_iteration(
        cfg, 1, llm_factory=lambda i: _MixedBatchLLM(),
        solver=MockSolver(seed=0), log=lambda *_: None,
    )
    assert len(it.evaluations) == 4  # the invalid task was excluded from scoring
    assert [r["slug"] for r in it.rejected] == ["bad"]
    # an under-full batch (4 valid < 5 requested) cannot clear the 2-of-5 gate,
    # regardless of how many of the 4 are long-horizon
    assert not it.passed_gate(cfg.min_long_horizon)


def test_from_dict_coerces_string_list_fields():
    # a live model may return a string where a list is expected; it must not be
    # split into characters
    t = GeneratedTask.from_dict({
        "slug": "s", "title": "T", "instruction": "i",
        "techniques": "custom-vm",
        "environment": {"base_image": "ubuntu:24.04", "packages": "gdb"},
        "checkpoints": [{"id": "a", "verify_cmd": "true", "depends_on": "cp-x"}],
    })
    assert t.techniques == ["custom-vm"]
    assert t.environment["packages"] == ["gdb"]
    assert t.checkpoints[0].depends_on == ["cp-x"]


def test_dockerfile_handles_string_packages():
    task = GeneratedTask(
        slug="s", title="T", category="reverse-engineering", difficulty="hard",
        summary="s", instruction="i",
        environment={"base_image": "ubuntu:24.04", "packages": "gdb"},  # bypasses from_dict
        checkpoints=[Checkpoint("a", "a", "", "true")],
    )
    df = render_dockerfile(task)
    assert "gdb" in df  # whole token; a char-split would leave only "b d g ..."


def test_run_iteration_skips_inconclusive_solver(tmp_path):
    cfg = _cfg(tmp_path, tasks_per_iteration=5, min_long_horizon=2)
    base = MockSolver(seed=0, long_horizon_minutes=cfg.long_horizon_minutes)

    class FlakySolver:  # first task is "inconclusive" (e.g. an oddish timeout)
        name = "flaky"

        def solve(self, task, *, iteration, index):
            if index == 0:
                raise RuntimeError("trial did not reach a terminal state; inconclusive")
            return base.solve(task, iteration=iteration, index=index)

    it, _ = run_iteration(
        cfg, 1, llm_factory=default_llm_factory(cfg), solver=FlakySolver(),
        log=lambda *_: None,
    )
    assert len(it.evaluations) == 4  # the inconclusive task is skipped, not scored
    assert any("solver error" in r["errors"][0] for r in it.rejected)
    assert not it.passed_gate(cfg.min_long_horizon)  # under-full batch can't pass
