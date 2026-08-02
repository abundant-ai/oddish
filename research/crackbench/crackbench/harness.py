"""The auto-research loop.

Each iteration:
  1. builds a *fresh* subagent transport (no carried context — "start from scratch"),
  2. fans out Haiku design-reader subagents to derive deterministic checkpoints,
  3. runs one Fable generator subagent to propose a batch of tasks,
  4. scores every task with the baseline "Brock" solver,
  5. gates on: at least ``min_long_horizon`` of the batch are long-horizon.

Long-horizon tasks are accumulated into an accepted corpus and materialized to the
output directory. The loop stops at ``max_iterations`` or once ``target_long_horizon``
tasks are collected.

Clearing context every iteration is deliberate: it makes each batch an independent
sample of the generator, so the ``min_long_horizon``-of-``tasks_per_iteration`` gate
is an honest acceptance test of the prompt's base rate — not a figure inflated by a
generator that has learned to repeat one lucky task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import HarnessConfig
from .llm import LLM, AnthropicLLM, FakeLLM
from .materialize import write_task_dir
from .models import IterationResult, RunResult, TaskEvaluation
from .solver import MockSolver, OddishSolver, Solver, classify_long_horizon
from .subagents import CheckpointGuidance, generate_tasks, read_checkpoint_guidance

LLMFactory = Callable[[int], LLM]
Logger = Callable[[str], None]


def default_llm_factory(config: HarnessConfig) -> LLMFactory:
    """Return a factory that builds a fresh subagent transport per iteration.

    Offline, the fake is seeded per iteration so independent rounds differ (the way
    a real model samples differently each call) while staying reproducible.
    """

    def factory(iteration: int) -> LLM:
        if config.live:
            return AnthropicLLM()
        return FakeLLM(seed=config.seed * 1000 + iteration)

    return factory


def default_solver(config: HarnessConfig) -> Solver:
    if config.solver == "mock":
        return MockSolver(
            seed=config.seed, long_horizon_minutes=config.long_horizon_minutes
        )
    return OddishSolver(
        agent=config.solver_agent,
        model=config.solver_model,
        long_horizon_minutes=config.long_horizon_minutes,
        workdir=config.out_dir / "solve",
    )


def run_iteration(
    config: HarnessConfig,
    index: int,
    *,
    llm_factory: LLMFactory,
    solver: Solver,
    log: Logger,
    cached_guidance: CheckpointGuidance | None = None,
) -> tuple[IterationResult, CheckpointGuidance]:
    log(f"[iter {index}] cleared subagent context; starting from scratch")
    llm = llm_factory(index)

    if cached_guidance is not None:
        guidance = cached_guidance
        log(f"[iter {index}] reusing cached checkpoint guidance [{guidance.digest}]")
    else:
        guidance = read_checkpoint_guidance(
            llm,
            config.design_dir,
            model=config.haiku_model,
            max_tokens=config.max_reader_tokens,
        )
        log(
            f"[iter {index}] haiku readers distilled checkpoints from "
            f"{len(guidance.sources)} design doc(s) [{guidance.digest}]"
        )

    tasks = generate_tasks(
        llm,
        guidance,
        n=config.tasks_per_iteration,
        minutes=config.long_horizon_minutes,
        model=config.fable_model,
        max_tokens=config.max_gen_tokens,
    )
    log(f"[iter {index}] fable generated {len(tasks)} candidate task(s)")

    # Reject structurally-invalid candidates before scoring: an invalid task must
    # not be solved, counted as long-horizon, or written into the accepted corpus.
    rejected: list[dict] = []
    valid: list = []
    for task in tasks:
        errors = task.validation_errors()
        if errors:
            rejected.append({"slug": task.slug, "errors": errors})
            log(f"[iter {index}]   ! rejected {task.slug}: {errors}")
        else:
            valid.append(task)
    if len(valid) != config.tasks_per_iteration:
        log(
            f"[iter {index}] ! {len(valid)} valid task(s) != requested "
            f"{config.tasks_per_iteration}; the {config.min_long_horizon}-of-"
            f"{config.tasks_per_iteration} gate cannot pass on this batch"
        )

    evaluations: list[TaskEvaluation] = []
    for idx, task in enumerate(valid):
        result = solver.solve(task, iteration=index, index=idx)
        long_horizon, reason = classify_long_horizon(
            result, minutes_threshold=config.long_horizon_minutes
        )
        evaluations.append(
            TaskEvaluation(
                task=task, solve=result, long_horizon=long_horizon, reason=reason
            )
        )
        tag = "LONG-HORIZON" if long_horizon else "short"
        log(f"[iter {index}]   {task.slug}: {tag} — {reason}")

    iteration = IterationResult(
        index=index,
        guidance_digest=guidance.digest,
        guidance_sources=guidance.sources,
        evaluations=evaluations,
        expected_batch_size=config.tasks_per_iteration,
        rejected=rejected,
    )
    return iteration, guidance


def run(
    config: HarnessConfig,
    *,
    llm_factory: LLMFactory | None = None,
    solver: Solver | None = None,
    log: Logger = print,
) -> RunResult:
    config.out_dir.mkdir(parents=True, exist_ok=True)
    llm_factory = llm_factory or default_llm_factory(config)
    solver = solver or default_solver(config)

    log(
        f"crackbench: up to {config.max_iterations} iteration(s), "
        f"{config.tasks_per_iteration} tasks each, gate >= {config.min_long_horizon} "
        f"long-horizon (>= {config.long_horizon_minutes:g} min), "
        f"solver={solver.name}, live={config.live}"
    )

    iterations: list[IterationResult] = []
    accepted: list = []
    cached: CheckpointGuidance | None = None

    for index in range(1, config.max_iterations + 1):
        iteration, guidance = run_iteration(
            config,
            index,
            llm_factory=llm_factory,
            solver=solver,
            log=log,
            cached_guidance=cached,
        )
        if config.cache_checkpoints:
            cached = guidance
        iterations.append(iteration)

        passed = iteration.passed_gate(config.min_long_horizon)
        log(
            f"[iter {index}] gate {'PASS' if passed else 'FAIL'}: "
            f"{iteration.long_horizon_count}/{len(iteration.evaluations)} long-horizon "
            f"(need {config.min_long_horizon})"
        )

        _write_iteration_artifacts(config, iteration)
        for task in iteration.long_horizon_tasks:
            dest = config.out_dir / "accepted" / f"iter{index}-{task.slug}"
            write_task_dir(
                task, dest, long_horizon_minutes=config.long_horizon_minutes
            )
            accepted.append(task)

        if (
            config.target_long_horizon is not None
            and len(accepted) >= config.target_long_horizon
        ):
            log(
                f"reached target of {config.target_long_horizon} long-horizon "
                f"task(s) after {index} iteration(s); stopping early"
            )
            break

    result = RunResult(
        iterations=iterations, accepted=accepted, config=config.summary()
    )
    _write_run_summary(config, result, log=log)
    return result


def _write_iteration_artifacts(
    config: HarnessConfig, iteration: IterationResult
) -> None:
    path = config.out_dir / f"iteration-{iteration.index:02d}.json"
    path.write_text(
        json.dumps(iteration.to_dict(config.min_long_horizon), indent=2),
        encoding="utf-8",
    )


def _write_run_summary(
    config: HarnessConfig, result: RunResult, *, log: Logger
) -> None:
    (config.out_dir / "summary.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    lines = [
        "# crackbench run summary",
        "",
        f"- iterations run: {len(result.iterations)}",
        f"- iterations passing the {config.min_long_horizon}/"
        f"{config.tasks_per_iteration} gate: {result.iterations_passed}",
        f"- long-horizon tasks accepted: {len(result.accepted)}",
        f"- solver: {config.solver} | live: {config.live} | seed: {config.seed}",
        "",
        "## Accepted long-horizon tasks",
        "",
    ]
    if result.accepted:
        for task in result.accepted:
            lines.append(
                f"- `{task.slug}` ({task.category}/{task.difficulty}, "
                f"{len(task.checkpoints)} checkpoints) — {task.summary}"
            )
    else:
        lines.append("_none_")
    lines.append("")
    (config.out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    log(
        f"wrote artifacts to {config.out_dir} "
        f"(summary.json, summary.md, iteration-*.json, accepted/)"
    )
