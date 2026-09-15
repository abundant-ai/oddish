"""Store bounded directory pages and artifact-only metadata indexes."""

from alembic import op
import sqlalchemy as sa

revision = "file_index_001"
down_revision = "prepared_reads_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "file_indexes",
        sa.Column("source_key", sa.Text(), primary_key=True),
        sa.Column(
            "task_version_id",
            sa.Text(),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "trial_id", sa.String(160), sa.ForeignKey("trials.id", ondelete="CASCADE")
        ),
        sa.Column("root_prefix", sa.Text()),
        sa.Column("revision", sa.String(32)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )
    op.create_table(
        "file_entries",
        sa.Column(
            "source_key",
            sa.Text(),
            sa.ForeignKey("file_indexes.source_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("parent", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger()),
        sa.Column("is_directory", sa.Boolean(), nullable=False),
        sa.Column("artifact", sa.Boolean(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "ix_file_entries_directory",
        "file_entries",
        ["source_key", "parent", "path"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_file_entries_artifacts",
        "file_entries",
        ["source_key", "path"],
        postgresql_where=sa.text("artifact"),
        if_not_exists=True,
    )

    op.create_index(
        "ix_file_indexes_pending",
        "file_indexes",
        ["next_attempt_at"],
        postgresql_where=sa.text("revision IS NULL"),
        if_not_exists=True,
    )
    op.execute("""
        CREATE FUNCTION queue_file_directory() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE item jsonb := to_jsonb(NEW);
        BEGIN
            IF TG_TABLE_NAME = 'task_versions' AND (item->>'deleted_at') IS NULL THEN
                INSERT INTO file_indexes (source_key, task_version_id)
                VALUES (COALESCE((item->>'expanded_manifest_key'), 'expand:' || (item->>'id')), (item->>'id'))
                ON CONFLICT (source_key) DO UPDATE SET task_version_id = EXCLUDED.task_version_id;
            ELSIF TG_TABLE_NAME = 'trials' AND (item->>'deleted_at') IS NULL
                AND ((item->>'trial_s3_key') IS NOT NULL OR (item->>'finished_at') IS NOT NULL) THEN
                INSERT INTO file_indexes (source_key, trial_id)
                VALUES ('trial:' || (item->>'id') || ':' || COALESCE(item->>'attempts', '0') || ':' || COALESCE((item->>'trial_s3_key'), ''), (item->>'id'))
                ON CONFLICT (source_key) DO UPDATE SET trial_id = EXCLUDED.trial_id;
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER file_directory_changed AFTER INSERT OR UPDATE OF expanded_manifest_key, deleted_at ON task_versions FOR EACH ROW EXECUTE FUNCTION queue_file_directory()"
    )
    op.execute(
        "CREATE TRIGGER file_directory_changed AFTER INSERT OR UPDATE OF trial_s3_key, attempts, finished_at, deleted_at ON trials FOR EACH ROW EXECUTE FUNCTION queue_file_directory()"
    )
    op.execute("""INSERT INTO file_indexes (source_key, task_version_id)
        SELECT COALESCE(expanded_manifest_key, 'expand:' || id), id FROM task_versions WHERE deleted_at IS NULL
        ON CONFLICT (source_key) DO UPDATE SET task_version_id = EXCLUDED.task_version_id""")
    op.execute("""INSERT INTO file_indexes (source_key, trial_id)
        SELECT 'trial:' || id || ':' || COALESCE(attempts, 0)::text || ':' || COALESCE(trial_s3_key, ''), id
        FROM trials WHERE deleted_at IS NULL AND (trial_s3_key IS NOT NULL OR finished_at IS NOT NULL)
        ON CONFLICT (source_key) DO UPDATE SET trial_id = EXCLUDED.trial_id""")


def downgrade():
    op.execute("DROP TRIGGER file_directory_changed ON task_versions")
    op.execute("DROP TRIGGER file_directory_changed ON trials")
    op.execute("DROP FUNCTION queue_file_directory()")
    op.drop_table("file_entries")
    op.drop_table("file_indexes")
