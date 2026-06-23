"""Unit tests that do NOT require a database.

Covers the dedupe of the preview seed's dropped-column warning (Fix 1) and the
bootstrap's rebuild-from-base decision -- fresh branch, or a reused branch whose
schema drifted from the models (Fix 2).

``test_preview_seed.py`` skips its whole module without ``ODDISH_DATABASE_URL``
(every test there touches Postgres); these checks are pure functions, so they
live here and always run.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import JSON, Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

import models  # noqa: F401  registers the cloud tables on the shared Base
import preview_seed

# ---------------------------------------------------------------------------
# Fix 1: the dropped-column warning fires at most once per (table, column).
# ---------------------------------------------------------------------------


def _toy_table() -> Table:
    md = MetaData()
    return Table(
        "worker_jobs",
        md,
        Column("id", String, primary_key=True),
        Column("payload", JSONB),
    )


def test_prepare_row_drops_unknown_columns_warning_once(capsys, monkeypatch):
    """A column present in prod but absent from the branch schema warns ONCE
    across many rows, not once per row -- otherwise a six-figure draw floods
    stderr and the job hangs for hours."""
    monkeypatch.setattr(preview_seed, "_warned_dropped_columns", set())
    table = _toy_table()

    rows = [
        {"id": f"wj-{i}", "payload": {"k": i}, "provider": "anthropic", "external_id": "x"}
        for i in range(50)
    ]
    prepared = [preview_seed._prepare_row(table, r) for r in rows]

    # Behavior preserved: known columns kept, unknown dropped.
    assert all(set(p) == {"id", "payload"} for p in prepared)

    err = capsys.readouterr().err
    assert err.count("dropped worker_jobs.provider") == 1
    assert err.count("dropped worker_jobs.external_id") == 1


def test_prepare_row_warns_per_distinct_column_and_table(capsys, monkeypatch):
    """Each distinct (table, column) still gets its own legible warning."""
    monkeypatch.setattr(preview_seed, "_warned_dropped_columns", set())
    table = _toy_table()

    preview_seed._prepare_row(table, {"id": "a", "provider": "p"})
    preview_seed._prepare_row(table, {"id": "b", "external_id": "e"})

    err = capsys.readouterr().err
    assert err.count("dropped worker_jobs.provider") == 1
    assert err.count("dropped worker_jobs.external_id") == 1


def test_prepare_row_preserves_json_coercion_and_filtering():
    """JSON/JSONB strings are decoded; columns are still filtered to the
    target schema."""
    md = MetaData()
    table = Table(
        "t",
        md,
        Column("id", String, primary_key=True),
        Column("blob", JSON),
    )
    out = preview_seed._prepare_row(
        table, {"id": "x", "blob": '{"a": 1}', "gone": "y"}
    )
    assert out == {"id": "x", "blob": {"a": 1}}


# ---------------------------------------------------------------------------
# Fix 2: the bootstrap rebuilds the branch schema from base instead of trusting
# the inherited (stale) snapshot -- on a fresh branch, and on a reused branch
# whose schema still drifts from the models after upgrade (old-stamp poison).
# ---------------------------------------------------------------------------


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "scripts"
        / "preview"
        / "bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_run(calls):
    """Record alembic invocations; all succeed (no DB in these tests)."""

    def run(args, *a, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, args=args, stdout="", stderr="")

    return run


def _patch_db(monkeypatch, boot, *, trusted, resets, marks, creates):
    async def fake_trusted(url):
        return trusted

    async def fake_reset(url):
        resets.append(url)

    async def fake_mark(url):
        marks.append(url)

    async def fake_create_all(url):
        creates.append(url)

    monkeypatch.setattr(boot, "_schema_trusted", fake_trusted)
    monkeypatch.setattr(boot, "_reset_public_schema", fake_reset)
    monkeypatch.setattr(boot, "_mark_trusted", fake_mark)
    monkeypatch.setattr(boot, "_create_all_from_models", fake_create_all)


def _prime_env(monkeypatch, tmp_path):
    sentinel = tmp_path / "rebuilt"
    monkeypatch.setenv("ODDISH_DATABASE_URL", "postgresql+asyncpg://u@h/db")
    monkeypatch.setenv("SCHEMA_REBUILT_FILE", str(sentinel))
    return sentinel


def test_trusted_branch_upgrades_without_rebuild(monkeypatch, tmp_path):
    """A branch this script already built (marker present) just upgrades to
    head -- no drop, no create_all, no stamp, no re-seed signal."""
    boot = _load_bootstrap()
    calls, resets, marks, creates = [], [], [], []
    monkeypatch.setattr(boot.subprocess, "run", _fake_run(calls))
    _patch_db(monkeypatch, boot, trusted=True, resets=resets, marks=marks, creates=creates)
    sentinel = _prime_env(monkeypatch, tmp_path)

    boot.main()

    assert resets == [] and marks == [] and creates == []
    upgrades = [c for c in calls if c[:2] == ["alembic", "upgrade"]]
    assert len(upgrades) == len(boot.STACKS)
    assert not any(c[:2] == ["alembic", "stamp"] for c in calls)
    assert not sentinel.exists()


def test_untrusted_branch_rebuilds_marks_and_signals(monkeypatch, tmp_path):
    """A fresh snapshot or stamp-poisoned branch (marker absent) is dropped,
    rebuilt from the model graph, stamped to head, marked, and signals a re-seed."""
    boot = _load_bootstrap()
    calls, resets, marks, creates = [], [], [], []
    monkeypatch.setattr(boot.subprocess, "run", _fake_run(calls))
    _patch_db(monkeypatch, boot, trusted=False, resets=resets, marks=marks, creates=creates)
    sentinel = _prime_env(monkeypatch, tmp_path)

    boot.main()

    assert len(resets) == 1 and len(creates) == 1 and len(marks) == 1
    stamps = [c for c in calls if c[:2] == ["alembic", "stamp"]]
    assert len(stamps) == len(boot.STACKS)
    assert not any(c[:2] == ["alembic", "upgrade"] for c in calls)
    assert sentinel.read_text() == "1"
