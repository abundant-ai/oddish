import pytest
from pydantic import ValidationError

from oddish.schemas import TaskSweepSubmission


def _base(**kw):
    return TaskSweepSubmission(task_id="t1", configs=[], **kw)


def test_probe_scope_defaults_to_task():
    assert _base().probe_scope == "task"


def test_probe_scope_accepts_experiment():
    assert _base(probe_scope="experiment").probe_scope == "experiment"


def test_probe_scope_rejects_unknown():
    with pytest.raises(ValidationError):
        _base(probe_scope="galaxy")
