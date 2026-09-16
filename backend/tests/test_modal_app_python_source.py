"""Worker images must copy every top-level backend module auth imports."""

from __future__ import annotations

import ast
from pathlib import Path


def _worker_image_python_sources() -> list[str]:
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("modal_app.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_local_python_source"
        ):
            return [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
    raise AssertionError("add_local_python_source not found in modal_app.py")


def test_worker_image_copies_org_access() -> None:
    assert "org_access" in _worker_image_python_sources()
