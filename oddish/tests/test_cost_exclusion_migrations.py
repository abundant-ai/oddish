from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic/versions"


def test_model_exclusion_upgrade_preserves_existing_key_policy():
    source = (VERSIONS / "costexcl02_model_and_experiment_exclusions.py").read_text()
    upgrade = source.split("def upgrade() -> None:", 1)[1].split(
        "def downgrade() -> None:", 1
    )[0]

    assert "DROP TABLE IF EXISTS cost_excluded_llm_keys" not in upgrade


def test_repair_revision_restores_table_for_already_migrated_databases():
    source = (VERSIONS / "costexcl03_preserve_llm_key_exclusions.py").read_text()

    assert (
        'down_revision: Union[str, Sequence[str], None] = "expmodelrename01"' in source
    )
    assert "CREATE TABLE IF NOT EXISTS cost_excluded_llm_keys" in source
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_excluded_llm_keys_hash_live"
        in source
    )


def test_cost_exclusion_repair_keeps_one_migration_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))

    assert scripts.get_heads() == ["expmodelrename02"]
