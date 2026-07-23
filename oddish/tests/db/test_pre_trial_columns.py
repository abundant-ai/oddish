from oddish.db import AnalyzerBlockModel
from oddish.db.models import TaskModel


def test_task_has_pre_trial_columns():
    cols = set(TaskModel.__table__.columns.keys())
    assert {
        "pre_trial", "pre_trial_status", "pre_trial_error",
        "pre_trial_started_at", "pre_trial_finished_at",
    } <= cols


def test_analyzer_block_records_prompt_version():
    cols = set(AnalyzerBlockModel.__table__.columns.keys())
    assert {"prompt_key", "prompt_version"} <= cols
