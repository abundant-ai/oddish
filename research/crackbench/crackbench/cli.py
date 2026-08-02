"""Command-line entry point: ``python -m crackbench run [options]``.

Offline by default (fake LLM + mock solver), so it runs end-to-end with zero
setup. ``--live`` uses the real Anthropic API (needs ANTHROPIC_API_KEY and the
``anthropic`` SDK); ``--solver oddish`` measures real solve time via the oddish CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import (
    DEFAULT_DESIGN_DIR,
    DEFAULT_FABLE_MODEL,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OUT_DIR,
    HarnessConfig,
)
from .harness import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crackbench",
        description="Auto-research harness for long-horizon CrackMeBench-style tasks.",
    )
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="run the auto-research loop")

    run_p.add_argument("--iterations", type=int, default=5, help="max iterations")
    run_p.add_argument(
        "--per-iteration", type=int, default=5, help="tasks generated per iteration"
    )
    run_p.add_argument(
        "--min-long-horizon",
        type=int,
        default=2,
        help="gate: min long-horizon tasks per iteration",
    )
    run_p.add_argument(
        "--minutes",
        type=float,
        default=30.0,
        help="long-horizon solve-time threshold (minutes)",
    )
    run_p.add_argument(
        "--target",
        type=int,
        default=None,
        help="stop once this many long-horizon tasks are collected",
    )
    run_p.add_argument("--design-dir", type=Path, default=DEFAULT_DESIGN_DIR)
    run_p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    run_p.add_argument("--seed", type=int, default=0)
    run_p.add_argument(
        "--cache-checkpoints",
        action="store_true",
        help="read design docs once instead of every iteration",
    )

    run_p.add_argument(
        "--live",
        action="store_true",
        help="use the real Anthropic API instead of the offline fake",
    )
    run_p.add_argument("--fable-model", default=DEFAULT_FABLE_MODEL)
    run_p.add_argument("--haiku-model", default=DEFAULT_HAIKU_MODEL)

    run_p.add_argument("--solver", choices=("mock", "oddish"), default="mock")
    run_p.add_argument("--solver-agent", default="claude-code")
    run_p.add_argument("--solver-model", default="anthropic/claude-sonnet-4-5")
    return parser


def config_from_args(args: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig(
        fable_model=args.fable_model,
        haiku_model=args.haiku_model,
        live=args.live,
        tasks_per_iteration=args.per_iteration,
        min_long_horizon=args.min_long_horizon,
        long_horizon_minutes=args.minutes,
        max_iterations=args.iterations,
        target_long_horizon=args.target,
        cache_checkpoints=args.cache_checkpoints,
        solver=args.solver,
        solver_agent=args.solver_agent,
        solver_model=args.solver_model,
        design_dir=args.design_dir,
        out_dir=args.out,
        seed=args.seed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 1

    config = config_from_args(args)
    result = run(config)

    passed = result.iterations_passed
    total = len(result.iterations)
    print(
        f"\ndone: {passed}/{total} iteration(s) passed the gate; "
        f"{len(result.accepted)} long-horizon task(s) accepted -> {config.out_dir}"
    )
    return _exit_code(result)


def _exit_code(result) -> int:
    """0 if the run produced any long-horizon tasks, else 2.

    Acceptance is per-task, not gated on a whole iteration clearing the
    N-of-M bar, so a run can write a useful corpus without any iteration
    "passing". Automation keying off the exit code must see that as success.
    """
    return 0 if result.accepted else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
