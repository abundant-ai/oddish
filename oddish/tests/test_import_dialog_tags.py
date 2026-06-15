"""Tests for tag handling on the task-import endpoint."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_import_endpoint_accepts_tags_form_field():
    import pytest

    tasks_router = pytest.importorskip("backend.api.routers.imports")

    handler = (
        getattr(tasks_router, "import_task", None)
        or getattr(tasks_router, "import_task_zip", None)
        or getattr(tasks_router, "import_zip_endpoint", None)
    )
    assert handler is not None, "import handler not found"
    sig = inspect.signature(handler)
    assert "tags" in sig.parameters
