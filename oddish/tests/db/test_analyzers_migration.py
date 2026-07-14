from pathlib import Path


MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "analyzers_001_add_analyzers.py"
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
