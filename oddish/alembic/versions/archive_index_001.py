"""Merge the status fix and invalidate archive indexes on in-place uploads."""

from alembic import op

revision = "archive_index_001"
down_revision = ("legacy_file_index_001", "prepared_status_001")
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION invalidate_task_archive_directory() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE file_indexes SET revision = NULL, next_attempt_at = now()
            WHERE source_key = 'expand:' || NEW.id;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER task_archive_directory_changed
        AFTER UPDATE OF task_s3_key, content_hash ON task_versions
        FOR EACH ROW WHEN (OLD.task_s3_key IS DISTINCT FROM NEW.task_s3_key
            OR OLD.content_hash IS DISTINCT FROM NEW.content_hash)
        EXECUTE FUNCTION invalidate_task_archive_directory()
    """)


def downgrade():
    op.execute("DROP TRIGGER task_archive_directory_changed ON task_versions")
    op.execute("DROP FUNCTION invalidate_task_archive_directory()")
