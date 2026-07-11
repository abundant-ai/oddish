from pathlib import Path

from oddish.db.models import WorkerJobKind


def test_report_kind_exists():
    assert WorkerJobKind.REPORT.value == "REPORT"


def test_migration_adds_enum_value_and_chains():
    mig = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "reports_002_worker_job_kind_report.py"
    ).read_text()
    assert 'revision = "reports_002"' in mig
    assert 'down_revision = "reports_001"' in mig
    assert "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'REPORT'" in mig
