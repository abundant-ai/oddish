from oddish.db.models import (
    AnalyzerModel,
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
)


def test_tables_and_columns():
    assert CapabilityCategoryModel.__tablename__ == "capability_categories"
    assert {"slug", "name", "description", "sort_order",
            "created_at", "updated_at", "deleted_at"} <= set(
        CapabilityCategoryModel.__table__.columns.keys())

    assert CapabilityModel.__tablename__ == "capabilities"
    assert {"slug", "name", "description", "example",
            "created_at", "updated_at", "deleted_at"} <= set(
        CapabilityModel.__table__.columns.keys())

    assert CapabilityCategoryTagModel.__tablename__ == "capability_category_tags"
    assert {"capability_slug", "category_slug", "is_primary"} <= set(
        CapabilityCategoryTagModel.__table__.columns.keys())

    assert CapabilityProposalModel.__tablename__ == "capability_proposals"
    assert {"id", "slug_suggestion", "name", "description", "example",
            "category_slugs", "analyzer_id", "trial_ids", "trajectory_link",
            "status", "promoted_capability_slug", "created_at",
            "reviewed_at", "reviewed_by"} <= set(
        CapabilityProposalModel.__table__.columns.keys())


def test_taxonomy_is_global_no_org_scoping():
    """The taxonomy is global by design -- an org_id here would silently make
    cross-org capability counts incomparable."""
    for m in (CapabilityModel, CapabilityCategoryModel):
        assert "org_id" not in m.__table__.columns.keys()


def test_tag_primary_key_is_composite():
    pk = {c.name for c in CapabilityCategoryTagModel.__table__.primary_key}
    assert pk == {"capability_slug", "category_slug"}


def test_analyzer_gains_taxonomy_snapshot_columns():
    cols = set(AnalyzerModel.__table__.columns.keys())
    assert {"taxonomy_version", "taxonomy_snapshot"} <= cols


def test_capability_tables_are_registered_for_soft_delete():
    """A deleted_at column does nothing on its own -- the session filter only
    applies to classes passed to register_soft_delete_models. Unregistered,
    retiring a capability would set the tombstone and load_taxonomy would keep
    rendering it into the rubric anyway."""
    from oddish.db.soft_delete import _SOFT_DELETE_MODELS  # registry

    assert CapabilityModel in _SOFT_DELETE_MODELS
    assert CapabilityCategoryModel in _SOFT_DELETE_MODELS
