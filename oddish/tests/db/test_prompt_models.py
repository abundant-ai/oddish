from oddish.db import PromptModel, PromptVersionModel, QARunModel


def test_prompt_models_expose_canonical_schema():
    assert PromptModel.__tablename__ == "prompts"
    assert PromptVersionModel.__tablename__ == "prompt_versions"
    assert {
        "id",
        "key",
        "description",
        "active_version",
        "created_at",
        "deleted_at",
    } <= set(PromptModel.__table__.columns.keys())
    assert {
        "id",
        "prompt_id",
        "version",
        "content",
        "created_at",
        "created_by",
    } <= set(PromptVersionModel.__table__.columns.keys())


def test_prompt_versions_are_unique_per_prompt():
    uniques = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in PromptVersionModel.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("prompt_id", "version") in uniques


def test_qa_run_references_exact_prompt_version():
    foreign_keys = {fk.target_fullname for fk in QARunModel.__table__.foreign_keys}
    assert "prompt_versions.id" in foreign_keys
