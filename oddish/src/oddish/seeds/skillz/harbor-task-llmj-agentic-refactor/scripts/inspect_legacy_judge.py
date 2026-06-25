#!/usr/bin/env python3
"""Best-effort inspector for legacy Harbor LLM/VLM judge artifact wiring."""

from __future__ import annotations

import argparse
import ast
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any


JUDGE_NAMES = ("llm_judge.py", "visual_judge.py", "vlm_judge.py")
PRED_NAMES = {"pred_path", "prediction_path", "predicted_path", "plot_path", "image_path"}
REF_NAMES = {"gold_path", "reference_path", "ref_path", "expected_path"}
AGENTIC_RUBRIC_FILENAME = "agentic_rubric.json"
LEGACY_AGENTIC_RUBRIC_FILENAME = "rubric.json"


def media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    suffix = Path(path).suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix in {".txt", ".log"}:
        return "text/plain"
    return "application/octet-stream"


def as_abs(path: str, base: str | None = None) -> str:
    if path.startswith("/"):
        return path
    if base:
        return str(Path(base) / path)
    return path


class PathEvaluator:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def eval(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Call) and self._is_path_call(node):
            if node.args:
                return self.eval(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self.eval(node.left)
            right = self.eval(node.right)
            if left and right:
                return str(Path(left) / right)
        return None

    @staticmethod
    def _is_path_call(node: ast.Call) -> bool:
        func = node.func
        return isinstance(func, ast.Name) and func.id == "Path"

    def learn_assign(self, node: ast.Assign) -> None:
        value = self.eval(node.value)
        if not value:
            return
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.names[target.id] = value


def slug_from_path(path: str) -> str:
    stem = Path(path).stem or "artifact"
    chars = []
    for char in stem.lower():
        chars.append(char if char.isalnum() else "_")
    slug = "_".join(part for part in "".join(chars).split("_") if part)
    return slug or "artifact"


def load_rubric_keys(task_dir: Path) -> list[str]:
    tests_dir = task_dir / "tests"
    rubric_path = tests_dir / AGENTIC_RUBRIC_FILENAME
    if not rubric_path.exists():
        rubric_path = tests_dir / LEGACY_AGENTIC_RUBRIC_FILENAME
    if not rubric_path.exists():
        return []
    try:
        data = json.loads(rubric_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        return [str(key) for key in data.keys()]
    return []


def extract_dict_artifacts(tree: ast.Module, evaluator: PathEvaluator) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for key_node, value_node in zip(node.value.keys, node.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            if not isinstance(value_node, (ast.Tuple, ast.List)) or len(value_node.elts) < 2:
                continue
            predicted = evaluator.eval(value_node.elts[0])
            reference = evaluator.eval(value_node.elts[1])
            if not predicted:
                continue
            name = key_node.value
            artifact = {
                "name": name,
                "predicted_path": predicted,
                "rubric_key": name,
                "media_type": media_type(predicted),
            }
            if reference:
                artifact["reference_path"] = reference
            artifacts.append(artifact)
    return artifacts


def extract_single_artifact(tree: ast.Module, evaluator: PathEvaluator, rubric_keys: list[str]) -> list[dict[str, Any]]:
    values: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = evaluator.eval(node.value)
        if not value:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                values[target.id.lower()] = value

    predicted = None
    reference = None
    for name in PRED_NAMES:
        predicted = values.get(name)
        if predicted:
            break
    for name in REF_NAMES:
        reference = values.get(name)
        if reference:
            break
    if not predicted:
        return []

    rubric_key = slug_from_path(predicted)
    if len(rubric_keys) == 1:
        rubric_key = rubric_keys[0]

    artifact = {
        "name": slug_from_path(predicted),
        "predicted_path": predicted,
        "rubric_key": rubric_key,
        "media_type": media_type(predicted),
    }
    if reference:
        artifact["reference_path"] = reference
    return [artifact]


def inspect_task(task_dir: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    tests_dir = task_dir / "tests"
    judge_path = next((tests_dir / name for name in JUDGE_NAMES if (tests_dir / name).exists()), None)
    if not judge_path:
        raise SystemExit(f"No legacy judge found under {tests_dir}")

    source = judge_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(judge_path))
    evaluator = PathEvaluator()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            evaluator.learn_assign(node)

    rubric_keys = load_rubric_keys(task_dir)
    artifacts = extract_dict_artifacts(tree, evaluator)
    if not artifacts:
        artifacts = extract_single_artifact(tree, evaluator, rubric_keys)

    if not artifacts:
        warnings.append("Could not infer artifacts; inspect the judge manually.")
    if rubric_keys and len(rubric_keys) == 1 and artifacts:
        only_key = rubric_keys[0]
        if only_key in {"visual_requirements", "requirements"}:
            warnings.append(
                f"Rubric key {only_key!r} is generic; rename it to an artifact key and update rubric_key."
            )

    spec = {
        "version": 1,
        "mode": "visual_artifact_comparison",
        "scoring_mode": "rubric",
        "artifacts": artifacts,
    }
    return spec, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, help="Path to a Harbor task directory")
    parser.add_argument("--write", action="store_true", help="Write tests/agentic_judge.json")
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    spec, warnings = inspect_task(task_dir)
    text = json.dumps(spec, indent=2)
    print(text)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    if args.write:
        out = task_dir / "tests" / "agentic_judge.json"
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
