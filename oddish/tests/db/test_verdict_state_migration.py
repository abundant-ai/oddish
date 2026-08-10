from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from oddish.db import TaskModel


def test_published_verdict_constraint_matches_model_and_migration() -> None:
    constraint_name = "ck_tasks_published_verdict_status"
    model_constraints = {item.name for item in TaskModel.__table__.constraints}
    assert constraint_name in model_constraints

    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "verdict_state_001_enforce_published_verdict.py"
    ).read_text()
    assert constraint_name in migration
    assert "verdict_status = 'SUCCESS'" in migration
    assert "verdict_status = 'FAILED'" in migration
    assert "verdict <> 'null'::jsonb" in migration
    assert "NOT VALID" in migration
    assert "VALIDATE CONSTRAINT" in migration


def test_verdict_state_migration_is_the_only_head() -> None:
    oddish_root = Path(__file__).resolve().parents[2]
    config = Config(str(oddish_root / "alembic.ini"))
    config.set_main_option("script_location", str(oddish_root / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == ["verdict_state_001"]
