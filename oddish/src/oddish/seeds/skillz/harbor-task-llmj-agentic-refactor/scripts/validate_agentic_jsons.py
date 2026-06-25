#!/usr/bin/env python3
"""Validate Harbor agentic_judge.json and agentic_rubric.json files for LLMJ migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ALLOWED_MODES = {"visual_artifact_comparison"}
ALLOWED_SCORING_MODES = {"rubric", "single_score"}
PREDICTED_PREFIXES = ("/app/", "/repo/", "/workspace/", "/workdir/")
REFERENCE_PREFIX = "/tests/"
AGENTIC_RUBRIC_FILENAME = "agentic_rubric.json"
LEGACY_AGENTIC_RUBRIC_FILENAME = "rubric.json"
MCP_RUBRIC_FILENAME = "mcp_rubric.json"


class Reporter:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def emit(self) -> None:
        for message in self.errors:
            print(f"ERROR: {message}", file=sys.stderr)
        for message in self.warnings:
            print(f"WARNING: {message}", file=sys.stderr)


def load_json(path: Path, reporter: Reporter, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        reporter.error(f"{label} not found: {path}")
    except json.JSONDecodeError as exc:
        reporter.error(f"{label} is invalid JSON: {path}:{exc.lineno}:{exc.colno}: {exc.msg}")
    except OSError as exc:
        reporter.error(f"{label} could not be read: {path}: {exc}")
    return None


def has_glob(path: str) -> bool:
    return any(char in path for char in "*?[]")


def local_tests_path(task_dir: Path, container_path: str) -> Path | None:
    if not container_path.startswith(REFERENCE_PREFIX):
        return None
    return task_dir / "tests" / container_path.removeprefix(REFERENCE_PREFIX)


def validate_artifact_paths(task_dir: Path, artifact: dict[str, Any], index: int, reporter: Reporter) -> None:
    prefix = f"artifacts[{index}]"
    predicted_path = artifact.get("predicted_path")
    reference_path = artifact.get("reference_path")

    if isinstance(predicted_path, str):
        if not predicted_path.startswith(PREDICTED_PREFIXES):
            reporter.error(
                f"{prefix}.predicted_path must start with one of {', '.join(PREDICTED_PREFIXES)}: {predicted_path}"
            )
        if has_glob(predicted_path):
            reporter.error(f"{prefix}.predicted_path must be explicit, not a glob: {predicted_path}")

    if reference_path is None:
        return
    if not isinstance(reference_path, str) or not reference_path:
        reporter.error(f"{prefix}.reference_path must be a non-empty string when present")
        return
    if not reference_path.startswith(REFERENCE_PREFIX):
        reporter.error(f"{prefix}.reference_path must point under /tests/: {reference_path}")
    if has_glob(reference_path):
        reporter.error(f"{prefix}.reference_path must be explicit, not a glob: {reference_path}")

    local_path = local_tests_path(task_dir, reference_path)
    if local_path is not None and not local_path.exists():
        reporter.error(f"{prefix}.reference_path does not exist in task checkout: {reference_path}")


def validate_agentic_judge(task_dir: Path, data: Any, reporter: Reporter) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        reporter.error("agentic_judge.json must be a JSON object")
        return []

    if data.get("version") != 1:
        reporter.error("agentic_judge.json version must be 1")

    mode = data.get("mode")
    if mode not in ALLOWED_MODES:
        reporter.error(f"agentic_judge.json mode must be one of {sorted(ALLOWED_MODES)}")

    scoring_mode = data.get("scoring_mode")
    if scoring_mode not in ALLOWED_SCORING_MODES:
        reporter.error(f"agentic_judge.json scoring_mode must be one of {sorted(ALLOWED_SCORING_MODES)}")

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        reporter.error("agentic_judge.json artifacts must be a non-empty list")
        return []

    seen_names: set[str] = set()
    validated: list[dict[str, Any]] = []
    required_fields = ("name", "predicted_path", "rubric_key", "media_type")
    for index, artifact in enumerate(artifacts):
        prefix = f"artifacts[{index}]"
        if not isinstance(artifact, dict):
            reporter.error(f"{prefix} must be an object")
            continue
        for field in required_fields:
            value = artifact.get(field)
            if not isinstance(value, str) or not value:
                reporter.error(f"{prefix}.{field} must be a non-empty string")

        name = artifact.get("name")
        if isinstance(name, str) and name:
            if name in seen_names:
                reporter.error(f"{prefix}.name is duplicated: {name}")
            seen_names.add(name)

        required = artifact.get("required")
        if required is not None and not isinstance(required, bool):
            reporter.error(f"{prefix}.required must be boolean when present")

        description = artifact.get("description")
        if description is not None and not isinstance(description, str):
            reporter.error(f"{prefix}.description must be a string when present")

        validate_artifact_paths(task_dir, artifact, index, reporter)
        validated.append(artifact)

    return validated


def validate_criterion_value(key: str, value: Any, reporter: Reporter) -> None:
    def validate_text(text: Any, path: str) -> None:
        if not isinstance(text, str) or not text.strip():
            reporter.error(f"{path} must be a non-empty criterion string")
            return
        if any(marker in text for marker in ("/app/", "/tests/", "predicted_path", "reference_path")):
            reporter.warn(f"{path} appears to contain path wiring; keep paths in agentic_judge.json")

    def validate_weight(weight: Any, path: str) -> None:
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            reporter.error(f"{path} weight must be a positive number")

    if isinstance(value, list):
        if not value:
            reporter.error(f"rubric key {key!r} must have at least one criterion")
            return
        for index, item in enumerate(value):
            path = f"rubric[{key!r}][{index}]"
            if isinstance(item, str):
                validate_text(item, path)
            elif isinstance(item, dict):
                validate_text(item.get("criterion"), f"{path}.criterion")
                if "weight" in item:
                    validate_weight(item.get("weight"), f"{path}.weight")
            else:
                reporter.error(f"{path} must be a string or object with criterion")
        return

    if isinstance(value, dict):
        validate_text(value.get("description") or value.get("criterion"), f"rubric[{key!r}]")
        if "weight" in value:
            validate_weight(value.get("weight"), f"rubric[{key!r}].weight")
        return

    reporter.error(f"rubric key {key!r} must map to a list or object")


def validate_rubric(data: Any, artifacts: list[dict[str, Any]], reporter: Reporter, rubric_label: str) -> None:
    if not isinstance(data, dict) or not data:
        reporter.error(f"{rubric_label} must be a non-empty JSON object")
        return

    for key, value in data.items():
        if not isinstance(key, str) or not key:
            reporter.error(f"{rubric_label} keys must be non-empty strings")
            continue
        validate_criterion_value(key, value, reporter)

    artifact_keys = {
        artifact.get("rubric_key")
        for artifact in artifacts
        if isinstance(artifact.get("rubric_key"), str) and artifact.get("rubric_key")
    }
    rubric_keys = set(data.keys())

    for key in sorted(artifact_keys - rubric_keys):
        reporter.error(f"artifact rubric_key has no matching rubric entry: {key}")

    for key in sorted(rubric_keys - artifact_keys):
        reporter.warn(f"rubric key is not referenced by any artifact: {key}")


def validate_task(task_dir: Path, reporter: Reporter) -> None:
    agentic_path = task_dir / "tests" / "agentic_judge.json"
    preferred_rubric_path = task_dir / "tests" / AGENTIC_RUBRIC_FILENAME
    legacy_rubric_path = task_dir / "tests" / LEGACY_AGENTIC_RUBRIC_FILENAME
    mcp_rubric_path = task_dir / "tests" / MCP_RUBRIC_FILENAME

    if preferred_rubric_path.exists():
        rubric_path = preferred_rubric_path
        if legacy_rubric_path.exists():
            reporter.warn(
                "tests/rubric.json is ignored by AgenticGrader when tests/agentic_rubric.json exists; "
                "use tests/mcp_rubric.json for deterministic MCP-only rubric data"
            )
    else:
        rubric_path = legacy_rubric_path
        if legacy_rubric_path.exists():
            reporter.warn("using compatibility tests/rubric.json fallback; prefer tests/agentic_rubric.json for new tasks")

    agentic = load_json(agentic_path, reporter, "agentic_judge.json")
    rubric = load_json(rubric_path, reporter, rubric_path.name)
    if agentic is None or rubric is None:
        return

    artifacts = validate_agentic_judge(task_dir, agentic, reporter)
    validate_rubric(rubric, artifacts, reporter, rubric_path.name)

    if mcp_rubric_path.exists():
        mcp_rubric = load_json(mcp_rubric_path, reporter, MCP_RUBRIC_FILENAME)
        if mcp_rubric is None:
            return
        if not isinstance(mcp_rubric, dict):
            reporter.warn("mcp_rubric.json is present but is not a JSON object; confirm the deterministic verifier expects this shape")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, help="Path to a Harbor task directory")
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    reporter = Reporter()
    validate_task(task_dir, reporter)
    reporter.emit()

    if reporter.errors:
        return 1
    print("agentic_judge.json and agentic rubric files are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
