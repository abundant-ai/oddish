from oddish.db import AnalyzerBlockModel
from oddish.db.models import TaskModel, TaskVersionModel

_PRE_TRIAL_COLS = {
    "pre_trial", "pre_trial_status", "pre_trial_error",
    "pre_trial_started_at", "pre_trial_finished_at",
}


def test_task_version_has_pre_trial_columns():
    cols = set(TaskVersionModel.__table__.columns.keys())
    assert _PRE_TRIAL_COLS <= cols


def test_task_does_not_have_pre_trial_columns():
    # Pre-trial audits a specific source snapshot, so the columns live on
    # task_versions -- they must not creep back onto tasks.
    cols = set(TaskModel.__table__.columns.keys())
    assert not (_PRE_TRIAL_COLS & cols)


def test_analyzer_block_records_prompt_version():
    cols = set(AnalyzerBlockModel.__table__.columns.keys())
    assert {"prompt_key", "prompt_version"} <= cols
