"""Add qa_assignments plus the analyzer_runs idempotency columns.

An assignment binds one registry prompt to a QA lifecycle stage at a scope,
so pre-/post-trial QA becomes configurable data instead of hardcoded call
sites. Nothing executes assignments at this revision -- the cutover away from
``classify_trial_and_store`` is a later change, and ``downgrade()`` does not
attempt to restore it: rolling back past that cutover needs the code revert
too.

Every statement is inspector-guarded because ``000_initial_schema`` bootstraps
a fresh database with ``Base.metadata.create_all()`` against the live model
graph -- so on an empty database this table and these columns already exist by
the time this revision runs, and an unguarded ``create_table`` / ``add_column``
would raise DuplicateTable / DuplicateColumn.

Revision ID: qa_assignments_001
Revises: prompt_scope_org_001
"""

from alembic import op
import sqlalchemy as sa


revision = "qa_assignments_001"
down_revision = "prompt_scope_org_001"
branch_labels = None
depends_on = None


def _tables(bind: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind: sa.engine.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _fk_on(bind: sa.engine.Connection, table: str, column: str) -> str | None:
    """Name of the first FK constraint on ``column``, if any.

    Matched by column rather than by name on purpose: ``create_all`` names the
    constraint it derives from the model ``analyzer_runs_qa_assignment_id_fkey``,
    so a name-based guard would not see it and would add a second, redundant FK
    with identical semantics on every fresh database.
    """
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk.get("constrained_columns") == [column]:
            return fk.get("name") or ""
    return None


def upgrade() -> None:
    bind = op.get_bind()

    if "qa_assignments" not in _tables(bind):
        op.create_table(
            "qa_assignments",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("org_id", sa.String(64), nullable=True),
            sa.Column(
                "prompt_id",
                sa.String(64),
                sa.ForeignKey("prompts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("prompt_version", sa.Integer(), nullable=True),
            sa.Column("stage", sa.String(32), nullable=False),
            sa.Column("scope_type", sa.String(32), nullable=False),
            sa.Column("scope_id", sa.String(160), nullable=False),
            sa.Column("model", sa.String(255), nullable=False),
            sa.Column("reasoning_effort", sa.String(32), nullable=True),
            sa.Column("llm_client_type", sa.String(32), nullable=False),
            sa.Column(
                "allow_oddish_cli",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("created_by_user_id", sa.String(64), nullable=True),
            sa.Column(
                "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )

    assignment_indexes = _indexes(bind, "qa_assignments")
    if "ix_qa_assignments_org_id" not in assignment_indexes:
        op.create_index("ix_qa_assignments_org_id", "qa_assignments", ["org_id"])
    if "ix_qa_assignments_org_scope" not in assignment_indexes:
        op.create_index(
            "ix_qa_assignments_org_scope",
            "qa_assignments",
            ["org_id", "scope_type", "scope_id"],
        )

    run_columns = _columns(bind, "analyzer_runs")
    if "qa_assignment_id" not in run_columns:
        op.add_column(
            "analyzer_runs", sa.Column("qa_assignment_id", sa.String(64), nullable=True)
        )
    if "stage_event_key" not in run_columns:
        op.add_column(
            "analyzer_runs", sa.Column("stage_event_key", sa.String(255), nullable=True)
        )

    if _fk_on(bind, "analyzer_runs", "qa_assignment_id") is None:
        op.create_foreign_key(
            "fk_analyzer_runs_qa_assignment_id",
            "analyzer_runs",
            "qa_assignments",
            ["qa_assignment_id"],
            ["id"],
            ondelete="SET NULL",
        )

    run_indexes = _indexes(bind, "analyzer_runs")
    if "ix_analyzer_runs_qa_assignment_id" not in run_indexes:
        op.create_index(
            "ix_analyzer_runs_qa_assignment_id", "analyzer_runs", ["qa_assignment_id"]
        )
    if "uq_analyzer_runs_assignment_event" not in run_indexes:
        # Partial so ad-hoc `oddish qa` runs (no assignment) stay exempt while
        # assignment-driven ones can fire at most once per event.
        op.create_index(
            "uq_analyzer_runs_assignment_event",
            "analyzer_runs",
            ["qa_assignment_id", "stage_event_key"],
            unique=True,
            postgresql_where=sa.text("qa_assignment_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()

    run_indexes = _indexes(bind, "analyzer_runs")
    if "uq_analyzer_runs_assignment_event" in run_indexes:
        op.drop_index("uq_analyzer_runs_assignment_event", table_name="analyzer_runs")
    if "ix_analyzer_runs_qa_assignment_id" in run_indexes:
        op.drop_index("ix_analyzer_runs_qa_assignment_id", table_name="analyzer_runs")
    fk_name = _fk_on(bind, "analyzer_runs", "qa_assignment_id")
    if fk_name:
        # Drop whichever name this database actually carries -- a fresh DB got
        # create_all's derived name, an upgraded one got the explicit name.
        op.drop_constraint(fk_name, "analyzer_runs", type_="foreignkey")

    run_columns = _columns(bind, "analyzer_runs")
    if "stage_event_key" in run_columns:
        op.drop_column("analyzer_runs", "stage_event_key")
    if "qa_assignment_id" in run_columns:
        op.drop_column("analyzer_runs", "qa_assignment_id")

    if "qa_assignments" in _tables(bind):
        assignment_indexes = _indexes(bind, "qa_assignments")
        if "ix_qa_assignments_org_scope" in assignment_indexes:
            op.drop_index("ix_qa_assignments_org_scope", table_name="qa_assignments")
        if "ix_qa_assignments_org_id" in assignment_indexes:
            op.drop_index("ix_qa_assignments_org_id", table_name="qa_assignments")
        op.drop_table("qa_assignments")
