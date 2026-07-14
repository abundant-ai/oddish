from pathlib import Path


MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "analyzers_001_add_analyzers.py"
)

MIG_003 = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "analyzers_003_add_save_trial_analyses.py"
)


def test_migration_exists_and_chains_off_trajgraph():
    text = MIG.read_text()
    assert 'revision = "analyzers_001"' in text
    assert 'down_revision = "trajgraph_001"' in text
    # Idempotency + safe-DDL conventions.
    assert "CREATE TABLE IF NOT EXISTS analyzers" in text
    assert "CREATE TABLE IF NOT EXISTS analyzer_experiments" in text
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in text
    assert "SET lock_timeout = '8s'" in text
    assert "NOT VALID" in text and "VALIDATE CONSTRAINT" in text


def test_migration_003_adds_save_trial_analyses_column():
    text = MIG_003.read_text()
    assert 'revision = "analyzers_003"' in text
    assert 'down_revision = "analyzers_002"' in text
    assert "ADD COLUMN IF NOT EXISTS save_trial_analyses BOOLEAN NOT NULL DEFAULT false" in text
    assert "DROP COLUMN IF EXISTS save_trial_analyses" in text
    assert "SET lock_timeout = '8s'" in text
