"""Seed experiments + tasks + trials from abundant job dirs.

Pulls real job results from ``~/Developer/abundant/jobs/<job>/<trial>/result.json``,
creates one ExperimentModel per abundant job, one TaskModel per unique
``task_name`` in the job, and one TrialModel per trial dir. Also symlinks
the trial dirs into ``ODDISH_CC_CHAT_LOCAL_JOBS_DIR`` (``/tmp/cc-chat-jobs``
by default) so the cc-chat orchestrator's LocalFileStore can read them.

Usage::

    ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:odd@localhost:5432/oddish \\
      uv run python scripts/seed_experiments.py [--jobs DIR ...] [--org-id ORG]

Idempotent on (org_id, experiment name): re-running updates existing rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, select, text

from oddish.db import (
    ExperimentModel,
    JobStatus,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialOrigin,
    get_session,
    task_experiments,
    utcnow,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = Path.home() / "Developer" / "abundant" / "jobs"

# Pick a small, diverse set by default rather than the entire abundant tree.
DEFAULT_JOB_NAMES = [
    "2026-04-28__11-56-48",  # 22 trials, mix of eslint/mermaid/prettier/turborepo
    "2026-04-27__17-44-12",  # 20 trials, different mix
]

DEFAULT_CC_CHAT_DIR = Path(
    os.environ.get("ODDISH_CC_CHAT_LOCAL_JOBS_DIR", "/tmp/cc-chat-jobs")
)


def short_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(4)}"[:8]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def trial_status_from_result(result: dict[str, Any]) -> tuple[JobStatus, float | None, str | None]:
    """Map a Harbor result.json to (status, reward, error)."""
    exception = result.get("exception_info")
    if exception:
        msg = exception.get("message") if isinstance(exception, dict) else str(exception)
        return JobStatus.FAILED, None, msg

    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward")
    if reward is None and "score" in rewards:
        reward = rewards["score"]

    if reward is None:
        return JobStatus.FAILED, None, "no reward emitted by verifier"
    return JobStatus.SUCCESS, float(reward), None


def discover_trial_dirs(job_dir: Path) -> list[Path]:
    """Return immediate subdirs that look like trial dirs (have result.json)."""
    return sorted(
        p for p in job_dir.iterdir()
        if p.is_dir() and (p / "result.json").exists() and not p.name.startswith("analysis")
    )


def load_trial(trial_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((trial_dir / "result.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [skip] failed to load {trial_dir.name}: {e}")
        return None


async def seed_one_job(
    job_dir: Path,
    org_id: str | None,
    cc_chat_dir: Path,
) -> tuple[str, int, int]:
    """Returns (experiment_id, task_count, trial_count)."""
    job_name = job_dir.name
    experiment_name = f"abundant-{job_name}"

    trial_dirs = discover_trial_dirs(job_dir)
    if not trial_dirs:
        print(f"  [skip] no trial dirs in {job_dir}")
        return ("", 0, 0)

    trials_loaded: list[tuple[Path, dict[str, Any]]] = []
    for td in trial_dirs:
        data = load_trial(td)
        if data is not None:
            trials_loaded.append((td, data))

    if not trials_loaded:
        print(f"  [skip] no parseable trials in {job_dir}")
        return ("", 0, 0)

    now = utcnow()

    async with get_session() as session:
        # ---- experiment (idempotent on name within org) ----
        org_match = (
            ExperimentModel.org_id.is_(None)
            if org_id is None
            else ExperimentModel.org_id == org_id
        )
        existing_exp = (
            await session.execute(
                select(ExperimentModel).where(
                    and_(org_match, ExperimentModel.name == experiment_name)
                )
            )
        ).scalar_one_or_none()

        if existing_exp is None:
            experiment = ExperimentModel(
                id=short_id("e"),
                name=experiment_name,
                org_id=org_id,
            )
            session.add(experiment)
            await session.flush()
            exp_action = "insert"
        else:
            experiment = existing_exp
            # Wipe existing trials for clean re-seed.
            await session.execute(
                delete(TrialModel).where(TrialModel.experiment_id == experiment.id)
            )
            exp_action = "update"

        exp_id = experiment.id
        print(f"  [{exp_action}] experiment {exp_id} = {experiment_name}")

        # ---- group trials by task_name ----
        by_task: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        for td, data in trials_loaded:
            task_name = data.get("task_name") or td.name.split("__")[0]
            by_task.setdefault(task_name, []).append((td, data))

        task_count = 0
        trial_count = 0

        for task_name, members in sorted(by_task.items()):
            # First trial in the group provides task metadata.
            first_data = members[0][1]
            task_path = first_data.get("config", {}).get("task", {}).get("path") or task_name

            # Idempotent on (org, name).
            org_match_t = (
                TaskModel.org_id.is_(None)
                if org_id is None
                else TaskModel.org_id == org_id
            )
            existing_task = (
                await session.execute(
                    select(TaskModel).where(
                        and_(org_match_t, TaskModel.name == task_name)
                    )
                )
            ).scalar_one_or_none()

            if existing_task is None:
                task = TaskModel(
                    id=short_id("t"),
                    name=task_name,
                    org_id=org_id,
                    user="seed-experiments",
                    priority=Priority.LOW,
                    status=TaskStatus.COMPLETED,
                    task_path=task_path,
                    tags={"seed_source": str(job_dir)},
                    run_analysis=False,
                    started_at=now,
                    finished_at=now,
                )
                session.add(task)
                await session.flush()
            else:
                task = existing_task
                task.status = TaskStatus.COMPLETED
                task.finished_at = now

            # Link task <-> experiment (m2m).
            link_exists = (
                await session.execute(
                    select(task_experiments).where(
                        and_(
                            task_experiments.c.task_id == task.id,
                            task_experiments.c.experiment_id == exp_id,
                        )
                    )
                )
            ).first()
            if link_exists is None:
                await session.execute(
                    task_experiments.insert().values(
                        task_id=task.id, experiment_id=exp_id
                    )
                )

            task_count += 1

            # ---- trials ----
            for td, data in members:
                status, reward, err = trial_status_from_result(data)
                started = parse_iso(data.get("started_at")) or now
                finished = parse_iso(data.get("finished_at")) or now

                agent_cfg = data.get("config", {}).get("agent", {}) or {}
                env_cfg = data.get("config", {}).get("environment", {}) or {}
                agent_result = data.get("agent_result") or {}

                trial_id = data.get("id") or td.name
                trial_id = trial_id[:128]

                trial = TrialModel(
                    id=trial_id,
                    name=td.name,
                    task_id=task.id,
                    experiment_id=exp_id,
                    org_id=org_id,
                    agent=(agent_cfg.get("name") or "claude-code")[:64],
                    provider="anthropic",
                    queue_key=f"{(agent_cfg.get('name') or 'claude-code')}:{org_id or 'default'}",
                    model=(agent_cfg.get("model_name") or "anthropic/claude-opus-4-7")[:128],
                    environment=(env_cfg.get("type") or "daytona")[:32],
                    status=status,
                    origin=TrialOrigin.IMPORTED,
                    attempts=1,
                    max_attempts=1,
                    started_at=started,
                    finished_at=finished,
                    reward=reward,
                    error_message=err,
                    harbor_result_path=str(td.resolve()),
                    result={
                        "verifier_result": data.get("verifier_result"),
                        "agent_result": agent_result,
                    },
                    input_tokens=agent_result.get("n_input_tokens"),
                    cache_tokens=agent_result.get("n_cache_tokens"),
                    output_tokens=agent_result.get("n_output_tokens"),
                    cost_usd=agent_result.get("cost_usd"),
                )
                session.add(trial)
                trial_count += 1

                # Symlink trial dir into cc-chat local file_store.
                link_path = cc_chat_dir / exp_id / trial_id
                link_path.parent.mkdir(parents=True, exist_ok=True)
                if link_path.is_symlink() or link_path.exists():
                    if link_path.is_symlink():
                        link_path.unlink()
                try:
                    link_path.symlink_to(td.resolve(), target_is_directory=True)
                except OSError as e:
                    print(f"    [warn] symlink failed for {trial_id}: {e}")

            print(
                f"    {task_name}: {len(members)} trials "
                f"({sum(1 for _, d in members if (d.get('verifier_result') or {}).get('rewards', {}).get('reward', 0) >= 1)} pass)"
            )

        await session.commit()
        return (exp_id, task_count, trial_count)


async def resolve_org_id(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    async with get_session() as session:
        row = (
            await session.execute(text("SELECT id FROM organizations LIMIT 1"))
        ).first()
        if row:
            return row[0]
    # Match the existing seed_tasks.py behavior — local stack uses 'org_local'.
    return "org_local"


async def run(args: argparse.Namespace) -> None:
    jobs_root: Path = args.jobs_root
    if not jobs_root.is_dir():
        sys.exit(f"jobs root not found: {jobs_root}")

    if args.jobs:
        chosen: list[Path] = []
        for name in args.jobs:
            p = jobs_root / name
            if p.is_dir():
                chosen.append(p)
            else:
                print(f"  [warn] not found: {p}")
    else:
        chosen = []
        for name in DEFAULT_JOB_NAMES:
            p = jobs_root / name
            if p.is_dir():
                chosen.append(p)
            else:
                print(f"  [warn] default job not found, skipping: {p}")

    if not chosen:
        sys.exit("no jobs to seed")

    cc_chat_dir = args.cc_chat_dir
    cc_chat_dir.mkdir(parents=True, exist_ok=True)

    org_id = await resolve_org_id(args.org_id)
    print(f"Seeding {len(chosen)} job(s) into org_id={org_id!r}")
    print(f"cc-chat local file store: {cc_chat_dir}")
    print()

    summary: list[tuple[str, str, int, int]] = []
    for job_dir in chosen:
        print(f"== {job_dir.name} ==")
        exp_id, n_tasks, n_trials = await seed_one_job(job_dir, org_id, cc_chat_dir)
        if exp_id:
            summary.append((job_dir.name, exp_id, n_tasks, n_trials))
        print()

    print("Done.")
    for job_name, exp_id, n_tasks, n_trials in summary:
        print(
            f"  http://localhost:3001/experiments/{exp_id}  "
            f"({n_tasks} tasks, {n_trials} trials)  [{job_name}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        default=None,
        help="Stamp this org_id on seeded rows. Defaults to first org in DB or 'org_local'.",
    )
    parser.add_argument(
        "--jobs-root",
        type=Path,
        default=DEFAULT_JOBS_ROOT,
        help=f"Root directory containing job subdirs. Default: {DEFAULT_JOBS_ROOT}",
    )
    parser.add_argument(
        "--jobs",
        nargs="*",
        help="Specific job dir names to seed (under --jobs-root). Default: a small built-in list.",
    )
    parser.add_argument(
        "--cc-chat-dir",
        type=Path,
        default=DEFAULT_CC_CHAT_DIR,
        help=f"Directory for cc-chat LocalFileStore symlinks. Default: {DEFAULT_CC_CHAT_DIR}",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
