"""Preview schemas rebuild when model or migration inputs change."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fingerprint_is_deterministic():
    mod = _load_bootstrap()
    m = sa.MetaData()
    sa.Table("trials", m, sa.Column("id", sa.String), sa.Column("status", sa.String))
    assert mod._fingerprint_metadata(m) == mod._fingerprint_metadata(m)


def test_adding_a_column_changes_the_fingerprint():
    mod = _load_bootstrap()
    before = sa.MetaData()
    sa.Table(
        "trials", before, sa.Column("id", sa.String), sa.Column("status", sa.String)
    )
    after = sa.MetaData()
    sa.Table(
        "trials",
        after,
        sa.Column("id", sa.String),
        sa.Column("status", sa.String),
        sa.Column("harbor_sha", sa.String),
    )
    assert mod._fingerprint_metadata(before) != mod._fingerprint_metadata(after)


def test_trust_marker_folds_in_the_fingerprint(monkeypatch):
    mod = _load_bootstrap()
    monkeypatch.setattr(mod, "_model_fingerprint", lambda: "deadbeef")
    monkeypatch.setattr(mod, "_migration_fingerprint", lambda: "cafef00d")
    assert mod._trust_marker() == f"{mod.SCHEMA_MARKER}:deadbeef:cafef00d"
    assert mod._trust_marker() != mod.SCHEMA_MARKER


def test_migration_fingerprint_reads_both_stacks(monkeypatch, tmp_path):
    mod = _load_bootstrap()
    oddish = tmp_path / "oddish"
    backend = tmp_path / "backend"
    for project, body in ((oddish, "a"), (backend, "b")):
        versions = project / "alembic" / "versions"
        versions.mkdir(parents=True)
        (versions / "rev.py").write_text(body)
        cache = versions / "__pycache__"
        cache.mkdir()
        (cache / "ignored.py").write_text("ignored")

    monkeypatch.setattr(mod, "STACKS", (oddish, backend))
    before = mod._migration_fingerprint()
    (backend / "alembic" / "versions" / "__pycache__" / "ignored.py").write_text(
        "changed"
    )
    assert mod._migration_fingerprint() == before
    (backend / "alembic" / "versions" / "rev.py").write_text("changed")
    after = mod._migration_fingerprint()

    assert before != after
