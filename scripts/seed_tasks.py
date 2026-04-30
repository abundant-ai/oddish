"""Seed TaskModel rows for local UI development from the ralph/long-horizon tasks.

Reads ``long-horizon/tasks/*/task.toml`` and inserts one TaskModel per task with
varied ``status`` / ``verdict_status`` / ``verdict`` JSON so the dashboard renders
the full range of UI states without running the cloud platform.

Usage:
    ODDISH_DATABASE_URL=postgresql+asyncpg://user:pw@localhost/oddish \\
        uv run python scripts/seed_tasks.py [--org-id ORG] [--long-horizon DIR]

Idempotent: re-running updates existing rows by (org_id, name).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select, text

from oddish.db import (
    Priority,
    TaskModel,
    TaskStatus,
    VerdictStatus,
    get_session,
    utcnow,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LONG_HORIZON_TASKS = REPO_ROOT / "long-horizon" / "tasks"
DEFAULT_SWE_GEN_JS_TASKS = Path.home() / "Developer" / "abundant" / "SWE-gen-JS-tasks"
DEFAULT_TASK_SOURCES = [DEFAULT_LONG_HORIZON_TASKS, DEFAULT_SWE_GEN_JS_TASKS]


# Per-task seed scenarios — keyed by task directory name.
# Covers all four UI states: completed-good, completed-bad, mid-pipeline, failed.
SCENARIOS: dict[str, dict[str, Any]] = {
    "biofabric-rust-rewrite": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.SUCCESS,
        "verdict": {
            "is_good": True,
            "confidence": "high",
            "primary_issue": None,
            "recommendations": [],
            "reasoning": (
                "All trials reached the verifier and split between solid passes "
                "and legitimate model failures. Tests are deterministic and the "
                "Java reference is well-defined."
            ),
        },
    },
    "find-network-alignments": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.SUCCESS,
        "verdict": {
            "is_good": False,
            "confidence": "medium",
            "primary_issue": "Tests Too Permissive",
            "recommendations": [
                "Tighten the S3 score floor — current threshold passes alignments produced by random seed-and-extend baselines.",
                "Add a per-pair runtime cap so submissions that brute-force overnight are not rewarded.",
                "Verify the held-out yeast NC threshold is not satisfied by trivially permuted oracle output.",
            ],
            "reasoning": (
                "Multiple GOOD_SUCCESS classifications correspond to alignments "
                "that beat the threshold without implementing simulated annealing."
            ),
        },
    },
    "rust-c-compiler": {
        "status": TaskStatus.ANALYZING,
        "verdict_status": VerdictStatus.RUNNING,
        "verdict": None,
    },
    "rust-java-lsp": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.FAILED,
        "verdict_error": (
            "Verdict LLM call timed out after 120s on the consolidated "
            "trial-level analyses. Retry from the dashboard."
        ),
        "verdict": None,
    },
    # SWE-gen-JS tasks (real GitHub issues turned into eval tasks)
    "mermaid-js__mermaid-5197": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.SUCCESS,
        "verdict": {
            "is_good": True,
            "confidence": "high",
            "primary_issue": None,
            "recommendations": [],
            "reasoning": (
                "TypeScript bugfix with deterministic Vitest assertions; "
                "trial outcomes split cleanly between pass and fail."
            ),
        },
    },
    "prettier__prettier-15487": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.SUCCESS,
        "verdict": {
            "is_good": False,
            "confidence": "medium",
            "primary_issue": "Underspecified Instruction",
            "recommendations": [
                "Clarify expected formatting behaviour for nested ternaries — current instruction defers to 'match prettier defaults' without naming the option.",
                "Pin the prettier-plugin version range so transitive snapshot diffs do not flip the verifier.",
                "Add a passing-state baseline run so reviewers can spot snapshot drift unrelated to the agent's change.",
            ],
            "reasoning": (
                "Multiple agents passed by guessing at the requested format and got "
                "credit despite landing on different defaults."
            ),
        },
    },
    "sindresorhus__got-547": {
        "status": TaskStatus.ANALYZING,
        "verdict_status": VerdictStatus.RUNNING,
        "verdict": None,
    },
    "vercel__turborepo-6279": {
        "status": TaskStatus.COMPLETED,
        "verdict_status": VerdictStatus.FAILED,
        "verdict_error": (
            "Verdict aggregation failed: not enough successful trials to form a "
            "consensus (5 attempts, 0 reached the verifier)."
        ),
        "verdict": None,
    },
}


def load_task_toml(task_dir: Path) -> dict[str, Any]:
    with (task_dir / "task.toml").open("rb") as f:
        return tomllib.load(f)


def build_tags(toml_data: dict[str, Any]) -> dict[str, Any]:
    metadata = toml_data.get("metadata", {})
    tags = {
        "category": metadata.get("category"),
        "difficulty": metadata.get("difficulty"),
        "harbor_tags": metadata.get("tags", []),
        "description": metadata.get("description"),
        "author_name": metadata.get("author_name"),
        "author_organization": metadata.get("author_organization"),
        "expert_time_estimate_hours": metadata.get("expert_time_estimate_hours"),
    }
    return {k: v for k, v in tags.items() if v is not None}


async def resolve_org_id(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    async with get_session() as session:
        row = (
            await session.execute(text("SELECT id FROM organizations LIMIT 1"))
        ).first()
        return row[0] if row else None


async def seed(task_sources: list[Path], org_id: str | None) -> None:
    found_any = False
    for src in task_sources:
        if src.is_dir():
            found_any = True
        else:
            print(f"  [warn] task source not found, skipping: {src}")
    if not found_any:
        sys.exit("None of the configured task source directories exist.")

    now = utcnow()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in task_sources:
        if not src.is_dir():
            continue
        for task_path in sorted(src.iterdir()):
            if not task_path.is_dir() or not (task_path / "task.toml").exists():
                continue
            if task_path.name in seen:
                print(f"  [skip] duplicate task name across sources: {task_path.name}")
                continue
            seen.add(task_path.name)

            scenario = SCENARIOS.get(task_path.name)
            if scenario is None:
                print(f"  [skip] no scenario configured for {task_path.name}")
                continue
            toml_data = load_task_toml(task_path)
            metadata = toml_data.get("metadata", {})
            author = metadata.get("author_name") or "seed"

            finished_at = now - timedelta(hours=2)
            started_at = finished_at - timedelta(hours=1)

            rows.append(
                {
                    "name": task_path.name,
                    "org_id": org_id,
                    "user": author,
                    "priority": Priority.LOW,
                    "status": scenario["status"],
                    "task_path": str(task_path),
                    "tags": build_tags(toml_data),
                    "run_analysis": True,
                    "started_at": started_at,
                    "finished_at": (
                        finished_at if scenario["status"] == TaskStatus.COMPLETED else None
                    ),
                    "verdict": scenario.get("verdict"),
                    "verdict_status": scenario.get("verdict_status"),
                    "verdict_error": scenario.get("verdict_error"),
                    "verdict_started_at": started_at + timedelta(minutes=55),
                    "verdict_finished_at": (
                        finished_at
                        if scenario.get("verdict_status") == VerdictStatus.SUCCESS
                        else None
                    ),
                }
            )

    if not rows:
        sys.exit("No tasks matched a configured scenario.")

    print(f"Seeding {len(rows)} task(s) into org_id={org_id!r}")

    async with get_session() as session:
        for row_data in rows:
            org_match = (
                TaskModel.org_id.is_(None)
                if org_id is None
                else TaskModel.org_id == org_id
            )
            existing = (
                await session.execute(
                    select(TaskModel).where(
                        and_(org_match, TaskModel.name == row_data["name"])
                    )
                )
            ).scalar_one_or_none()

            if existing is None:
                task = TaskModel(**row_data)
                session.add(task)
                action = "insert"
            else:
                for key, value in row_data.items():
                    setattr(existing, key, value)
                task = existing
                action = "update"

            print(
                f"  [{action}] {row_data['name']:<30} "
                f"status={row_data['status'].value:<12} "
                f"verdict={row_data['verdict_status'].value if row_data.get('verdict_status') else '-'}"
            )

        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        default=None,
        help="Stamp this org_id on the seeded tasks. Defaults to the first org in the DB, or NULL.",
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        action="append",
        dest="tasks_dirs",
        help=(
            "Directory containing task subdirectories (each with a task.toml). "
            f"Repeatable. Defaults to {' and '.join(str(p) for p in DEFAULT_TASK_SOURCES)}."
        ),
    )
    args = parser.parse_args()

    sources = [p.resolve() for p in (args.tasks_dirs or DEFAULT_TASK_SOURCES)]

    async def run() -> None:
        org_id = await resolve_org_id(args.org_id)
        await seed(sources, org_id)

    asyncio.run(run())


if __name__ == "__main__":
    main()
