from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from oddish.db import (
    QAReportItemModel,
    QAReportModel,
    QAReportPublicationModel,
    QAReportTaskModel,
)


ODDISH_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ODDISH_ROOT / "alembic" / "versions" / "qa_reports_001_add_curated_qa_reports.py"
)


def test_qa_report_migration_matches_model_tables() -> None:
    source = MIGRATION.read_text()
    models = (
        QAReportModel,
        QAReportTaskModel,
        QAReportItemModel,
        QAReportPublicationModel,
    )

    for model in models:
        assert f'"{model.__tablename__}"' in source
        for column in model.__table__.columns:
            assert f'"{column.name}"' in source

    assert "present == tables" in source
    assert "if present == tables:" in source
    assert "Incomplete QA report schema" in source
    assert "server_default=sa.func.now()" in source
    assert QAReportPublicationModel.__table__.c.published_at.server_default is not None


def test_qa_report_migration_has_one_head_and_correct_parent() -> None:
    config = Config(str(ODDISH_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ODDISH_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["qa_reports_001"]
    assert scripts.get_revision("qa_reports_001").down_revision == (
        "quota_pause_status_001"
    )


def test_qa_report_downgrade_drops_children_before_parent() -> None:
    source = MIGRATION.read_text()
    downgrade = source[source.index("def downgrade()") :]
    order = [
        'op.drop_table("qa_report_publications")',
        'op.drop_table("qa_report_items")',
        'op.drop_table("qa_report_tasks")',
        'op.drop_table("qa_reports")',
    ]

    positions = [downgrade.index(statement) for statement in order]
    assert positions == sorted(positions)
