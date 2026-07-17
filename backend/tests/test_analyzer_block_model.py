from oddish.db.models import AnalyzerBlockModel, JobStatus


def test_tablename_and_columns():
    cols = set(AnalyzerBlockModel.__table__.columns.keys())
    assert AnalyzerBlockModel.__tablename__ == "analyzer_blocks"
    # DB column is literally "metadata", not "block_metadata".
    assert "metadata" in cols
    assert "block_metadata" not in cols
    expected = {
        "id", "created_at", "updated_at", "deleted_at",
        "analyzer_id", "type", "key_prefix", "llm_client_type",
        "prompt", "input", "output", "status", "error",
        "job_started_at", "job_ended_at", "job_duration_seconds", "metadata",
    }
    assert expected <= cols


def test_metadata_attribute_maps_to_metadata_column():
    # The Python attribute is block_metadata (metadata is reserved on Base).
    assert AnalyzerBlockModel.block_metadata.property.columns[0].name == "metadata"


def test_status_reuses_jobstatus_enum():
    status_col = AnalyzerBlockModel.__table__.columns["status"]
    assert status_col.type.name == "jobstatus"
    # Must not try to CREATE TYPE — the type already exists.
    assert status_col.type.create_type is False


def test_importable_from_db_package():
    from oddish.db import AnalyzerBlockModel as Exported
    assert Exported is AnalyzerBlockModel
