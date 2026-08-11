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


def test_verdict_state_migration_is_in_a_single_linear_head() -> None:
    # The real invariant is a single head (no forks); verdict_state_001 no
    # longer names it -- later revisions (task_browse_summary_001, then this
    # PR's trials.kind chain) legitimately extend past it. Assert one head and
    # that verdict_state_001 is an ancestor of it, not that it *is* the head.
    oddish_root = Path(__file__).resolve().parents[2]
    config = Config(str(oddish_root / "alembic.ini"))
    config.set_main_option("script_location", str(oddish_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head, got {heads}"
    chain = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
    assert "verdict_state_001" in chain
