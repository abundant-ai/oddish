"""Unit tests that do NOT require a database.

Covers the dedupe of the preview seed's dropped-column warning (Fix 1) and
the bootstrap script's refusal to stamp head when the parent revision is not
in the branch's Alembic history (Fix 2).

``test_preview_seed.py`` skips its whole module without ``ODDISH_DATABASE_URL``
(every test there touches Postgres); these checks are pure functions, so they
live here and always run.
"""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
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
# Fix 2: stamping the parent revision fails loudly (no silent ``stamp head``)
# when that revision is not in the branch's Alembic history.
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


def test_stamp_to_parent_raises_when_revision_not_in_history(monkeypatch):
    """A 'Can't locate revision' from the stamp must raise SystemExit with an
    actionable message and must NOT fall back to ``stamp head`` (which would
    assert an unverified schema and corrupt the branch)."""
    boot = _load_bootstrap()
    calls = []

    def fake_run(args, *a, **kw):
        calls.append(args)
        return SimpleNamespace(
            returncode=1,
            args=args,
            stdout="",
            stderr=(
                "FAILED: Can't locate revision identified by 'deadbeef0000'"
            ),
        )

    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        boot._stamp_to_parent(Path("/tmp/proj"), "alembic_version_oddish", "deadbeef0000")

    message = str(exc.value)
    assert "deadbeef0000" in message
    assert "not in this branch's Alembic history" in message
    assert "merge `main`" in message
    # Only the stamp attempt ran; no second 'stamp head' subprocess.
    assert calls == [["alembic", "stamp", "deadbeef0000"]]
    assert not any("head" in c for c in calls)


def test_stamp_to_parent_returns_on_success(monkeypatch):
    """A clean stamp returns without raising and runs exactly one command."""
    boot = _load_bootstrap()
    calls = []

    def fake_run(args, *a, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, args=args, stdout="ok\n", stderr="")

    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    boot._stamp_to_parent(Path("/tmp/proj"), "alembic_version_oddish", "abc123")
    assert calls == [["alembic", "stamp", "abc123"]]


def test_stamp_to_parent_reraises_unrelated_failure(monkeypatch):
    """A stamp failure that is NOT 'Can't locate revision' re-raises as a
    CalledProcessError (unchanged behavior)."""
    boot = _load_bootstrap()

    def fake_run(args, *a, **kw):
        return SimpleNamespace(
            returncode=2, args=args, stdout="", stderr="some other alembic error"
        )

    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        boot._stamp_to_parent(Path("/tmp/proj"), "alembic_version_oddish", "abc123")
