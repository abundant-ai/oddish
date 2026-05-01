"""Unit tests for ``resolve_dashboard_experiment_author``.

The dashboard list view picks an author per experiment row by combining
the stamped ``experiments.created_by_user_id`` (joined to ``users``)
with the latest-task fallback used for legacy rows. The helper is pure
so we exercise the precedence directly without the full SQL join.

Importing ``oddish.core.dashboard`` pulls in SQLAlchemy and the rest of
the cloud DB layer. We mirror the pattern used in
``test_backend_task_user_resolution`` and ``test_backend_task_file_etag``:
slice the helper out of the module source and ``exec`` it in isolation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _load_helper() -> Any:
    src_path = (
        Path(__file__).resolve().parents[2]
        / "oddish"
        / "src"
        / "oddish"
        / "core"
        / "dashboard.py"
    )
    source = src_path.read_text(encoding="utf-8")

    needle = "def resolve_dashboard_experiment_author"
    start = source.index(needle)
    tail = source[start:]
    end = tail.index("\n\n\n")
    snippet = tail[:end]

    # Drop type annotations that reference SQLAlchemy types we don't have
    # in this isolated namespace — the helper itself is plain Python.
    snippet = re.sub(r":\s*tuple\[.*?\]", "", snippet)

    namespace: dict[str, Any] = {}
    exec(compile(snippet, str(src_path), "exec"), namespace)
    return namespace["resolve_dashboard_experiment_author"]


resolve_dashboard_experiment_author = _load_helper()


def test_creator_email_wins_over_latest_task():
    name, source = resolve_dashboard_experiment_author(
        creator={"email": "creator@example.com", "github_username": "creator-gh"},
        last_github_username="someone-else-gh",
        last_user="someone-else",
    )
    assert name == "creator@example.com"
    assert source == "creator"


def test_creator_without_email_falls_back_to_latest_task():
    # If the stored creator has no email, treat the row as if no creator
    # were known and use the legacy latest-task derivation.
    name, source = resolve_dashboard_experiment_author(
        creator={"email": None, "github_username": "creator-gh"},
        last_github_username="latest-gh",
        last_user="latest-user",
    )
    assert name == "latest-gh"
    assert source == "github"


def test_no_creator_uses_github_username_when_present():
    name, source = resolve_dashboard_experiment_author(
        creator=None,
        last_github_username="latest-gh",
        last_user="latest-user",
    )
    assert name == "latest-gh"
    assert source == "github"


def test_no_creator_no_github_username_falls_back_to_last_user():
    name, source = resolve_dashboard_experiment_author(
        creator=None,
        last_github_username=None,
        last_user="latest-user",
    )
    assert name == "latest-user"
    assert source == "api"


def test_legacy_row_with_root_user_unchanged_when_no_creator():
    # Documents the historical-data state: until the migration backfill
    # runs (or task.user is rewritten), legacy rows still surface "root"
    # via the latest-task path.
    name, source = resolve_dashboard_experiment_author(
        creator=None,
        last_github_username=None,
        last_user="root",
    )
    assert name == "root"
    assert source == "api"


def test_creator_present_overrides_root_sentinel():
    # The migration backfills experiments.created_by_user_id from the
    # earliest task's FK, so even rows whose latest task still has
    # task.user="root" should attribute to the real creator once the
    # join lands.
    name, source = resolve_dashboard_experiment_author(
        creator={"email": "real@example.com", "github_username": None},
        last_github_username=None,
        last_user="root",
    )
    assert name == "real@example.com"
    assert source == "creator"


def test_empty_inputs_return_none_name():
    name, source = resolve_dashboard_experiment_author(
        creator=None,
        last_github_username=None,
        last_user=None,
    )
    assert name is None
    # Source defaults to "api" when neither identity is present; callers
    # check ``name`` to decide whether to render anything.
    assert source == "api"
