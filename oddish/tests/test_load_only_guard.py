"""Enforce ``load_only()`` coverage for FE-surfaced model columns.

This is the executable form of the CLAUDE.md "load_only / MissingGreenlet"
gotcha and a regression guard for the 2026-06-24 incident (PR #413 surfaced
``trials.harbor_sha`` in the compact ``/tasks`` builder without adding it to the
``load_only`` set, 500ing every ``GET /tasks`` for 20 minutes).

The live check (``test_compact_builders_columns_are_load_only``) statically
diffs the columns read on the compact response-builder path against the columns
declared in the matching ``load_only`` sets. The remaining tests pin the guard's
own behaviour so it can't silently start passing vacuously.

See ``scripts/load_only_guard.py`` for the implementation and rationale.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ODDISH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ODDISH_ROOT / "src"))

_GUARD_PATH = _ODDISH_ROOT / "scripts" / "load_only_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("load_only_guard", _GUARD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass() can resolve the module by __module__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def test_compact_builders_columns_are_load_only():
    """Every column read on the compact /tasks path must be in its load_only set."""
    violations = guard.find_violations()
    if violations:
        lines = [
            f"{model}.{col}"
            for model, cols in sorted(violations.items())
            for col in cols
        ]
        pytest.fail(
            "Columns read by the compact /tasks response builders are missing "
            "from their load_only() set in list_tasks_core -- under async "
            "SQLAlchemy these lazy-load outside the request greenlet and 500 "
            "every GET /tasks with MissingGreenlet (the 2026-06-24 incident). "
            "Add each to the matching load_only(...) set:\n  " + "\n  ".join(lines)
        )


def test_guard_reads_are_non_empty():
    """Guard must actually discover reads -- a vacuous pass would hide regressions.

    Catches the failure mode where the compact entry point is renamed/refactored
    and the call-graph walk silently finds nothing to check.
    """
    metas = guard.introspect_models()
    reads = guard.collect_read_columns(guard._HELPERS_PATH, metas)
    # The compact path surfaces well over a dozen trial columns and a handful of
    # task/experiment columns; require a healthy floor on each, not just > 0.
    assert len(reads.get("TrialModel", set())) >= 20
    assert len(reads.get("TaskModel", set())) >= 10
    assert len(reads.get("ExperimentModel", set())) >= 3


def test_guard_detects_missing_column():
    """The diff logic must flag a read column absent from its load_only set."""
    metas = guard.introspect_models()
    reads = {"TrialModel": {"harbor_sha", "status", "id"}}
    declared = {"TrialModel": {"status"}}  # harbor_sha intentionally omitted
    violations = guard.compute_violations(reads, declared, metas)
    assert violations == {"TrialModel": ["harbor_sha"]}


def test_guard_allows_unlisted_primary_key():
    """Primary keys are never deferred by load_only, so an unlisted PK is fine."""
    metas = guard.introspect_models()
    reads = {"TrialModel": {"id"}}  # PK only, declared set empty
    violations = guard.compute_violations(reads, {"TrialModel": set()}, metas)
    assert violations == {}


def test_full_trial_builder_is_excluded():
    """``build_trial_response`` (non-compact path, no load_only) must not be checked.

    It legitimately reads columns the compact sets omit (e.g. ``trial.result``);
    diffing it against the compact sets would be a false positive. It must not be
    reachable from the compact entry point.
    """
    import ast

    tree = ast.parse(guard._HELPERS_PATH.read_text())
    reachable = guard._reachable_functions(tree, guard._COMPACT_ENTRY)
    assert "build_compact_trial_response" in reachable
    assert "_build_task_status_response" in reachable
    assert "build_trial_response" not in reachable
