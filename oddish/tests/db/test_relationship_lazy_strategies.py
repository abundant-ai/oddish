"""Guard the lazy-loading strategy of hot-path relationships.

A mapper-level ``lazy="selectin"`` on these relationships once made every
``select(TrialModel)`` cascade: trial -> task -> all sibling trials (wide
JSONB columns included) plus the task's experiments -- charging even the
404/org-check helpers for the task's full trial set. These relationships
must stay lazy; call sites that need them add ``selectinload(...)`` or go
through ``awaitable_attrs`` explicitly.
"""

import pytest

from oddish.db.models import TaskModel, TaskVersionModel, TrialModel


@pytest.mark.parametrize(
    ("model", "relationship_name"),
    [
        (TaskModel, "trials"),
        (TaskModel, "experiments"),
        (TrialModel, "task"),
        (TaskVersionModel, "task"),
    ],
)
def test_hot_path_relationships_are_lazy(model, relationship_name):
    strategy = getattr(model, relationship_name).property.lazy
    assert strategy == "select", (
        f"{model.__name__}.{relationship_name} uses lazy={strategy!r}. "
        "Mapper-level eager loading here cascades sibling-trial loads onto "
        "every trial/task fetch; keep it lazy and eager-load per query."
    )
