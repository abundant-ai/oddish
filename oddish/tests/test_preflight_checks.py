from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Severity


def _config(task_dir: Path) -> TaskConfig:
    return TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
