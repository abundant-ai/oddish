"""Schema-level tests for the tag tables and projection columns.

These mirror ``test_worker_jobs_schema.py``: metadata-only assertions that
detect drift between the ORM and the migration without needing a DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_tag_state_members():
    from oddish.db import TagState

    assert {s.value for s in TagState} == {"ACTIVE", "ARCHIVED", "MERGED", "DELETED"}


def test_tag_visibility_members():
    from oddish.db import TagVisibility

    assert {s.value for s in TagVisibility} == {"PRIVATE", "PUBLIC"}


def test_tag_assignment_scope_members():
    from oddish.db import TagAssignmentScope

    assert {s.value for s in TagAssignmentScope} == {"VERSION", "TASK", "EXPERIMENT"}


def test_tag_assignment_state_members():
    from oddish.db import TagAssignmentState

    assert {s.value for s in TagAssignmentState} == {"ACTIVE", "REMOVED"}


def test_tag_assignment_source_members():
    from oddish.db import TagAssignmentSource

    assert {s.value for s in TagAssignmentSource} == {
        "DIRECT",
        "EXPERIMENT_SNAPSHOT",
        "EXPERIMENT_LIVING",
    }


def test_tag_grant_capability_members():
    from oddish.db import TagGrantCapability

    assert {s.value for s in TagGrantCapability} == {
        "RENAME",
        "MERGE",
        "DELETE",
        "EDIT",
    }


def test_tag_model_columns():
    from oddish.db import TagModel

    cols = {c.name for c in TagModel.__table__.columns}
    required = {
        "id",
        "org_id",
        "key",
        "normalized_key",
        "value",
        "normalized_value",
        "color",
        "description",
        "visibility",
        "state",
        "merged_into_id",
        "owner_user_id",
        "row_version",
        "created_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    missing = required - cols
    assert not missing, f"TagModel missing columns: {sorted(missing)}"


def test_tag_model_table_name():
    from oddish.db import TagModel

    assert TagModel.__tablename__ == "tags"


def test_tag_model_partial_unique_index_present():
    from oddish.db import TagModel

    names = {idx.name for idx in TagModel.__table__.indexes}
    assert "uq_tags_org_normalized" in names


def test_tag_model_state_and_visibility_indexes_present():
    from oddish.db import TagModel

    names = {idx.name for idx in TagModel.__table__.indexes}
    assert "idx_tags_org_state" in names
    assert "idx_tags_org_visibility" in names


def test_tag_model_registered_for_soft_delete():
    from oddish.db import TagModel
    from oddish.db.soft_delete import get_soft_delete_models

    assert TagModel in get_soft_delete_models()


def test_tag_assignment_model_columns():
    from oddish.db import TagAssignmentModel

    cols = {c.name for c in TagAssignmentModel.__table__.columns}
    required = {
        "id",
        "tag_id",
        "org_id",
        "scope",
        "target_id",
        "task_id",
        "state",
        "source",
        "source_experiment_id",
        "source_assignment_id",
        "row_version",
        "assigned_by_user_id",
        "assigned_at",
        "removed_at",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    missing = required - cols
    assert not missing, f"TagAssignmentModel missing columns: {sorted(missing)}"


def test_tag_assignment_indexes_present():
    from oddish.db import TagAssignmentModel

    names = {idx.name for idx in TagAssignmentModel.__table__.indexes}
    assert "uq_tag_assignments_target" in names
    assert "idx_tag_assignments_tag_scope_state" in names
    assert "idx_tag_assignments_scope_target_state" in names
    assert "idx_tag_assignments_org_tag_state" in names
    assert "idx_tag_assignments_source_experiment" in names


def test_tag_exclusion_columns():
    from oddish.db import TagExclusionModel

    cols = {c.name for c in TagExclusionModel.__table__.columns}
    required = {
        "id",
        "tag_id",
        "org_id",
        "experiment_id",
        "scope",
        "target_id",
        "created_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    missing = required - cols
    assert not missing, f"TagExclusionModel missing columns: {sorted(missing)}"
    names = {idx.name for idx in TagExclusionModel.__table__.indexes}
    assert "uq_tag_exclusions_target" in names


def test_tag_grant_columns():
    from oddish.db import TagGrantModel

    cols = {c.name for c in TagGrantModel.__table__.columns}
    required = {
        "id",
        "tag_id",
        "org_id",
        "principal_type",
        "principal_user_id",
        "capability",
        "granted_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    missing = required - cols
    assert not missing, f"TagGrantModel missing columns: {sorted(missing)}"
    names = {idx.name for idx in TagGrantModel.__table__.indexes}
    assert "uq_tag_grants_principal" in names


def test_tag_event_columns():
    from oddish.db import TagEventModel

    cols = {c.name for c in TagEventModel.__table__.columns}
    required = {
        "id",
        "event_uuid",
        "org_id",
        "action",
        "tag_id",
        "scope",
        "target_id",
        "actor_user_id",
        "actor_type",
        "source",
        "reason",
        "payload",
        "occurred_at",
    }
    missing = required - cols
    assert not missing, f"TagEventModel missing columns: {sorted(missing)}"
    names = {idx.name for idx in TagEventModel.__table__.indexes}
    assert "idx_tag_events_org_tag_occurred_at" in names


def test_tag_policy_columns():
    from oddish.db import TagPolicyModel

    cols = {c.name for c in TagPolicyModel.__table__.columns}
    required = {
        "org_id",
        "max_tags_per_entity",
        "name_max_len",
        "name_charset",
        "reserved_prefixes",
        "who_can_create",
        "profanity_mode",
        "profanity_allowlist",
        "profanity_denylist",
        "updated_by_user_id",
        "updated_at",
    }
    missing = required - cols
    assert not missing, f"TagPolicyModel missing columns: {sorted(missing)}"
    assert TagPolicyModel.__table__.primary_key.columns.keys() == ["org_id"]


def test_saved_tag_filter_columns():
    from oddish.db import SavedTagFilterModel

    cols = {c.name for c in SavedTagFilterModel.__table__.columns}
    required = {
        "id",
        "org_id",
        "owner_user_id",
        "name",
        "filter_ast",
        "visibility",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    missing = required - cols
    assert not missing, f"SavedTagFilterModel missing columns: {sorted(missing)}"
    names = {idx.name for idx in SavedTagFilterModel.__table__.indexes}
    assert "uq_saved_tag_filters_owner_name" in names


def test_task_model_has_effective_tag_ids_columns():
    from oddish.db import TaskModel

    cols = {c.name for c in TaskModel.__table__.columns}
    assert "effective_tag_ids" in cols
    assert "current_version_tag_ids" in cols


def test_task_version_model_has_effective_tag_ids_column():
    from oddish.db import TaskVersionModel

    cols = {c.name for c in TaskVersionModel.__table__.columns}
    assert "effective_tag_ids" in cols
