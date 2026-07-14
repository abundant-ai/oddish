from pathlib import Path


MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "reports_001_add_reports.py"
)


def test_migration_exists_and_chains_off_trajgraph():
    text = MIG.read_text()
    assert 'revision = "reports_001"' in text
    assert 'down_revision = "trajgraph_001"' in text
    # Idempotency + safe-DDL conventions.
    assert "CREATE TABLE IF NOT EXISTS reports" in text
    assert "CREATE TABLE IF NOT EXISTS report_experiments" in text
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in text
    assert "SET lock_timeout = '8s'" in text
    assert "NOT VALID" in text and "VALIDATE CONSTRAINT" in text
