from oddish.db import PromptModel, PromptVersionModel


def test_models_expose_expected_columns():
    assert PromptModel.__tablename__ == "prompts"
    assert PromptVersionModel.__tablename__ == "prompt_versions"
    cols = set(PromptModel.__table__.columns.keys())
    assert {"id", "kind", "description", "created_at", "deleted_at"} <= cols
    assert "active_version" not in cols
    vcols = set(PromptVersionModel.__table__.columns.keys())
    assert {
        "id",
        "prompt_id",
        "version",
        "content",
        "created_at",
        "created_by",
    } <= vcols


def test_unique_constraint_on_prompt_id_version():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in PromptVersionModel.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("prompt_id", "version") in uniques
