"""Fixtures and opt-in gating for the task-submission performance harness.

These tests are deliberately kept out of normal CI. They carry the ``perf``
marker and are skipped unless BOTH switches are set:

* ``ODDISH_PERF`` is truthy -- the opt-in switch, and
* ``ODDISH_API_URL`` points at a target API (a local server or a preview
  deploy) -- the harness uploads and submits real tasks against it.

``ODDISH_API_KEY`` should also be set for any authenticated target; an
auth-less local server works without it.

The fixture builds N *real* (non-symlink) Harbor task directories. Real dirs
matter because ``archive_task_dir`` (oddish/src/oddish/cli/api.py) is now a
plain ``tar.add`` after the ``ODDISH_ARCHIVE_DEREF_SYMLINKS`` revert, so a
symlink-based fixture would archive broken links.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

PERF_ENV_FLAG = "ODDISH_PERF"
API_URL_ENV = "ODDISH_API_URL"
TASK_COUNT_ENV = "ODDISH_PERF_TASKS"
DEFAULT_TASK_COUNT = 30

# A minimal but valid Harbor task. Mirrors `harbor task init` output: enough to
# satisfy both `harbor.models.task.task.Task(...)` (used by `validate_tasks`)
# and oddish's `validate_task_timeout_config` (requires the three timeout
# fields below).
_TASK_TOML = """\
version = "1.0"

[metadata]

[verifier]
timeout_sec = 900.0

[agent]
timeout_sec = 900.0

[environment]
build_timeout_sec = 600.0
"""

_DOCKERFILE = "FROM ubuntu:24.04\n\nWORKDIR /app\n"

_TEST_SH = """\
#!/bin/bash
# Minimal verifier: always award full reward. The perf harness measures
# submission throughput and never executes trials, so this is never run.
mkdir -p /logs/verifier
echo 1.0 > /logs/verifier/reward.txt
"""

_SOLVE_SH = "#!/bin/bash\n# no-op solution (perf harness never runs the agent)\n"


def _write_task_dir(root: Path, name: str, nonce: str) -> Path:
    task_dir = root / name
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "tests").mkdir()
    (task_dir / "solution").mkdir()
    (task_dir / "task.toml").write_text(_TASK_TOML, encoding="utf-8")
    # Unique instruction content per task -> unique content hash, so the upload
    # /init step never short-circuits on a prior identical upload. Every task in
    # the run therefore measures a genuine upload + submit.
    (task_dir / "instruction.md").write_text(
        f"# Perf harness task {name}\n\n"
        f"Synthetic task (nonce {nonce}). Do nothing; the verifier always "
        "returns reward 1.0.\n",
        encoding="utf-8",
    )
    (task_dir / "environment" / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text(_TEST_SH, encoding="utf-8")
    (task_dir / "solution" / "solve.sh").write_text(_SOLVE_SH, encoding="utf-8")
    return task_dir


def make_synthetic_task_dirs(
    root: Path, n: int, *, nonce: str | None = None
) -> list[Path]:
    """Create *n* real (non-symlink) minimal Harbor task dirs under *root*."""
    nonce = nonce or uuid.uuid4().hex[:12]
    return [
        _write_task_dir(root, f"perf-{nonce}-{i:04d}", f"{nonce}-{i}") for i in range(n)
    ]


def perf_task_count(default: int = DEFAULT_TASK_COUNT) -> int:
    """Resolve the task count (N), overridable via ``ODDISH_PERF_TASKS``."""
    raw = os.environ.get(TASK_COUNT_ENV, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def perf_enabled() -> bool:
    return os.environ.get(PERF_ENV_FLAG, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "perf: opt-in task-submission performance harness (skipped unless "
        "ODDISH_PERF is truthy and ODDISH_API_URL points at a target API).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every ``perf``-marked test unless explicitly opted in."""
    if perf_enabled() and os.environ.get(API_URL_ENV):
        return
    if perf_enabled():
        reason = (
            f"perf harness needs a target API: set {API_URL_ENV} "
            "(a local server or preview-deploy URL)"
        )
    else:
        reason = (
            f"perf harness is opt-in: set {PERF_ENV_FLAG}=1 and {API_URL_ENV} to run it"
        )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("perf"):
            item.add_marker(skip)


@pytest.fixture
def synthetic_task_dirs(tmp_path: Path) -> list[Path]:
    """N real (non-symlink) minimal Harbor task dirs in a tmp dir.

    N defaults to 30; override with ``ODDISH_PERF_TASKS``.
    """
    return make_synthetic_task_dirs(tmp_path, perf_task_count())
