"""The preview bootstrap must rebuild a cached schema when the model graph changes.

Regression for the stale-trust bug: a preview branch that gained a column after
its first prep (a merge adding a migration, or a re-pointed migration chain) kept
its stale cached schema -- alembic was already stamped at head, so
``alembic upgrade head`` was a no-op, the new column was never created, and every
read of it 500'd. Folding the model-graph fingerprint into the schema trust
marker invalidates the cache on any table/column change, so the schema is rebuilt
from the current models.
"""

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
    assert mod._trust_marker() == f"{mod.SCHEMA_MARKER}:deadbeef"
    # The bare pre-fix marker must no longer be accepted as trusted.
    assert mod._trust_marker() != mod.SCHEMA_MARKER
