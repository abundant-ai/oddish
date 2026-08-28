"""Keep raw SQL diagnostics and cleanup scans aware of paused trials."""

from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "oddish"


def _normalized_source(relative_path: str) -> str:
    return " ".join((SRC / relative_path).read_text(encoding="utf-8").split())


def test_admin_orphan_queries_treat_paused_trials_as_active():
    source = _normalized_source("core/admin.py")
    active_statuses = "tr.status IN ('QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')"

    # The first occurrence produces the count and the second produces the
    # diagnostic sample. They must classify a paused-only task identically.
    assert source.count(active_statuses) == 2


def test_cleanup_queries_treat_paused_trials_as_active():
    source = _normalized_source("workers/queue/cleanup.py")
    active_statuses = "'PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING'"

    assert f"tr.status IN ({active_statuses})" in source
    assert f"base.status IN ( {active_statuses} )" in source
    assert f"a.status IN ({active_statuses})" in source
