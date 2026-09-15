"""Queue versionless task directories without changing task or trial history."""

from alembic import op
import sqlalchemy as sa

revision = "legacy_file_index_001"
down_revision = "file_index_001"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration creates current models on fresh installations.
    if "task_id" not in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("file_indexes")
    }:
        op.add_column(
            "file_indexes",
            sa.Column(
                "task_id", sa.String(128), sa.ForeignKey("tasks.id", ondelete="CASCADE")
            ),
        )
    op.execute("""
        CREATE FUNCTION queue_legacy_file_directory() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.current_version_id IS NULL AND NEW.deleted_at IS NULL THEN
                INSERT INTO file_indexes (source_key, task_id)
                VALUES ('task:' || NEW.id || ':' || COALESCE(NEW.task_s3_key, ''), NEW.id)
                ON CONFLICT (source_key) DO UPDATE SET task_id = EXCLUDED.task_id;
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER legacy_file_directory_changed
        AFTER INSERT OR UPDATE OF task_s3_key, current_version_id, deleted_at ON tasks
        FOR EACH ROW EXECUTE FUNCTION queue_legacy_file_directory()
    """)
    op.execute("""
        INSERT INTO file_indexes (source_key, task_id)
        SELECT 'task:' || id || ':' || COALESCE(task_s3_key, ''), id
        FROM tasks WHERE current_version_id IS NULL AND deleted_at IS NULL
        ON CONFLICT (source_key) DO UPDATE SET task_id = EXCLUDED.task_id
    """)


def downgrade():
    op.execute("DROP TRIGGER legacy_file_directory_changed ON tasks")
    op.execute("DROP FUNCTION queue_legacy_file_directory()")
    op.execute("DELETE FROM file_indexes WHERE task_id IS NOT NULL")
    op.drop_column("file_indexes", "task_id")
