from pathlib import Path

from oddish.db.models import WorkerJobKind


def test_analyzer_kind_exists():
    assert WorkerJobKind.ANALYZER.value == "ANALYZER"


def test_migration_adds_enum_value_and_chains():
    mig = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "analyzers_002_worker_job_kind_analyzer.py"
    ).read_text()
    assert 'revision = "analyzers_002"' in mig
    assert 'down_revision = "analyzers_001"' in mig
    assert "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'ANALYZER'" in mig
