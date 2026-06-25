#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_tasks(root: Path) -> list[dict[str, Any]]:
    tasks = []
    for path in sorted((root / "tasks").glob("*/task.json")):
        data = load_json(path)
        if isinstance(data, dict):
            tasks.append(data)
    return tasks


def relative_paths(root: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def trial_files(root: Path, trial_id: str) -> dict[str, bool]:
    trial_root = root / "trials" / trial_id
    return {
        "logs": (trial_root / "logs.txt").exists(),
        "structured_logs": (trial_root / "logs_structured.json").exists(),
        "result": (trial_root / "result.json").exists(),
        "trajectory": (trial_root / "trajectory.json").exists(),
    }


def build_summary(root: Path) -> dict[str, Any]:
    manifest = load_json(root / "manifest.json")
    manifest_errors = []
    if isinstance(manifest, dict):
        raw_errors = manifest.get("pull_errors") or []
        if isinstance(raw_errors, list):
            manifest_errors = raw_errors
    pull_error_files = relative_paths(
        root, sorted(root.glob("_pull_errors/**/*.error.txt"))
    )
    tasks_summary = []
    for task in load_tasks(root):
        trials = task.get("trials") or []
        status_counts = collections.Counter(t.get("status") for t in trials)
        analysis_counts = collections.Counter(
            ((t.get("analysis") or {}).get("classification") or "NO_ANALYSIS")
            for t in trials
        )
        running = [
            t.get("id")
            for t in trials
            if t.get("status") not in ("success", "failed")
        ]
        harness_errors = [
            t.get("id")
            for t in trials
            if (t.get("analysis") or {}).get("classification") == "HARNESS_ERROR"
        ]
        tasks_summary.append(
            {
                "name": task.get("name"),
                "id": task.get("id"),
                "status": task.get("status"),
                "progress": task.get("progress"),
                "experiment_id": task.get("experiment_id"),
                "experiment_name": task.get("experiment_name"),
                "total": task.get("total"),
                "completed": task.get("completed"),
                "failed": task.get("failed"),
                "reward_success": task.get("reward_success"),
                "reward_total": task.get("reward_total"),
                "status_counts": dict(status_counts),
                "analysis_counts": dict(analysis_counts),
                "running_trials": running,
                "harness_error_trials": harness_errors,
                "trials": [
                    {
                        "id": t.get("id"),
                        "status": t.get("status"),
                        "reward": t.get("reward"),
                        "analysis_status": t.get("analysis_status"),
                        "classification": (t.get("analysis") or {}).get(
                            "classification"
                        ),
                        "subtype": (t.get("analysis") or {}).get("subtype"),
                        "finished_at": t.get("finished_at"),
                        "files": trial_files(root, str(t.get("id") or "")),
                    }
                    for t in trials
                ],
            }
        )
    return {
        "pull_root": str(root),
        "manifest_present": isinstance(manifest, dict),
        "pull_errors": manifest_errors,
        "pull_error_files": pull_error_files,
        "tasks": tasks_summary,
    }


def write_markdown(summary: dict[str, Any], output: Path) -> None:
    lines = [
        "# Oddish Pull Summary",
        "",
        f"- Local root: `{summary['pull_root']}`",
        f"- Manifest present: {summary['manifest_present']}",
        f"- Pull errors: {len(summary['pull_errors'])}",
        f"- Pull error files: {len(summary['pull_error_files'])}",
        f"- Tasks: {len(summary['tasks'])}",
        "",
    ]
    if summary["pull_errors"] or summary["pull_error_files"]:
        lines.extend(["## Pull Completeness", ""])
        for error in summary["pull_errors"][:20]:
            lines.append(f"- Manifest error: `{json.dumps(error, sort_keys=True)}`")
        for path in summary["pull_error_files"][:20]:
            lines.append(f"- Error file: `{path}`")
        if len(summary["pull_errors"]) > 20 or len(summary["pull_error_files"]) > 20:
            lines.append("- Additional pull errors omitted from markdown; see JSON summary.")
        lines.append("")
    for task in summary["tasks"]:
        lines.extend(
            [
                f"## {task['name']}",
                "",
                f"- Task ID: `{task['id']}`",
                f"- Status: `{task['status']}` ({task['progress']})",
                f"- Rewards: {task['reward_success']}/{task['reward_total']}",
                f"- Trial statuses: {task['status_counts']}",
                f"- Analysis classifications: {task['analysis_counts']}",
            ]
        )
        if task["running_trials"]:
            lines.append(
                "- Still running remotely: "
                + ", ".join(f"`{trial}`" for trial in task["running_trials"])
            )
        if task["harness_error_trials"]:
            lines.append(
                "- Harness error trials: "
                + ", ".join(f"`{trial}`" for trial in task["harness_error_trials"])
            )
        lines.extend(
            [
                "",
                "| Trial | Status | Reward | Analysis | Subtype | Files | Finished |",
                "|---|---:|---:|---|---|---|---|",
            ]
        )
        for trial in task["trials"]:
            files = ",".join(k for k, v in trial["files"].items() if v)
            lines.append(
                "| `{}` | {} | {} | {} | {} | {} | {} |".format(
                    trial["id"],
                    trial["status"],
                    trial["reward"],
                    trial["classification"] or "",
                    trial["subtype"] or "",
                    files,
                    trial["finished_at"] or "",
                )
            )
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize an Oddish pull directory.")
    parser.add_argument("pull_root", type=Path)
    args = parser.parse_args()
    root = args.pull_root.resolve()
    if not (root / "tasks").exists() or not (root / "trials").exists():
        raise SystemExit(f"Not an Oddish pull root: {root}")
    summary = build_summary(root)
    (root / "analysis-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    write_markdown(summary, root / "analysis-summary.md")
    print(root / "analysis-summary.md")
    print(root / "analysis-summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
