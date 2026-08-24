"""add durable latest-runner projection to experiments

Revision ID: experiment_runner_001
Revises: feedback_001
Create Date: 2026-08-24 00:00:00.000000

The dashboard previously derived the newest billed user with a correlated
trial/collection subquery inside every bare-text predicate. That work happened
before ordering and pagination and scaled with the organization's complete
trial population.

The three columns below form one projection: the source trial identifies the
row that owns the user and timestamp, making drift directly auditable.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "experiment_runner_001"
down_revision: Union[str, Sequence[str], None] = "feedback_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column("last_runner_trial_id", sa.String(length=160), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "experiments",
        sa.Column("last_runner_user_id", sa.String(length=64), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "experiments",
        sa.Column("last_runner_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )

    # One database-owned refresh function keeps the projection correct for
    # every writer, including bulk SQL, imports, cleanup, retries, and future
    # call sites that do not construct ORM objects. It locks affected
    # experiments in sorted order and recomputes from source rows, which also
    # handles backward movement after a delete, supersede, or collection
    # removal.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_experiment_latest_runners(
            affected_experiment_ids text[]
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            experiment_id_to_lock text;
        BEGIN
            FOR experiment_id_to_lock IN
                SELECT DISTINCT candidate_id
                FROM unnest(affected_experiment_ids) AS candidate(candidate_id)
                WHERE candidate_id IS NOT NULL
                ORDER BY candidate_id
            LOOP
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'experiment-runner:' || experiment_id_to_lock,
                        0
                    )
                );
            END LOOP;

            WITH target_ids AS (
                SELECT DISTINCT candidate_id AS experiment_id
                FROM unnest(affected_experiment_ids) AS candidate(candidate_id)
                WHERE candidate_id IS NOT NULL
            ),
            memberships AS (
                SELECT
                    tr.experiment_id,
                    tr.id AS trial_id,
                    tr.billed_user_id,
                    tr.created_at
                FROM trials tr
                JOIN target_ids target ON target.experiment_id = tr.experiment_id
                WHERE tr.deleted_at IS NULL
                  AND tr.superseded_by_trial_id IS NULL

                UNION ALL

                SELECT
                    et.experiment_id,
                    tr.id AS trial_id,
                    tr.billed_user_id,
                    tr.created_at
                FROM experiment_trials et
                JOIN target_ids target ON target.experiment_id = et.experiment_id
                JOIN trials tr ON tr.id = et.trial_id
                WHERE et.deleted_at IS NULL
                  AND tr.deleted_at IS NULL
                  AND tr.superseded_by_trial_id IS NULL
            ),
            latest AS (
                SELECT DISTINCT ON (experiment_id)
                    experiment_id,
                    trial_id,
                    billed_user_id,
                    created_at
                FROM memberships
                ORDER BY experiment_id, created_at DESC, trial_id DESC
            )
            UPDATE experiments e
            SET last_runner_trial_id = latest.trial_id,
                last_runner_user_id = latest.billed_user_id,
                last_runner_at = latest.created_at
            FROM target_ids target
            LEFT JOIN latest ON latest.experiment_id = target.experiment_id
            WHERE e.id = target.experiment_id
              AND (
                  e.last_runner_trial_id,
                  e.last_runner_user_id,
                  e.last_runner_at
              ) IS DISTINCT FROM (
                  latest.trial_id,
                  latest.billed_user_id,
                  latest.created_at
              );
        END;
        $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_experiment_runners_from_trials()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_ids text[];
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT array_agg(DISTINCT experiment_id)
                INTO affected_ids
                FROM new_trials
                WHERE experiment_id IS NOT NULL;
            ELSIF TG_OP = 'DELETE' THEN
                SELECT array_agg(DISTINCT candidate_id)
                INTO affected_ids
                FROM (
                    SELECT experiment_id AS candidate_id
                    FROM old_trials
                    WHERE experiment_id IS NOT NULL
                    UNION
                    SELECT et.experiment_id
                    FROM experiment_trials et
                    JOIN old_trials old_trial ON old_trial.id = et.trial_id
                    WHERE et.deleted_at IS NULL
                ) candidates;
            ELSE
                WITH changed_trials AS (
                    SELECT
                        old_trial.id,
                        old_trial.experiment_id AS old_experiment_id,
                        new_trial.experiment_id AS new_experiment_id
                    FROM old_trials old_trial
                    JOIN new_trials new_trial USING (id)
                    WHERE (
                        old_trial.experiment_id,
                        old_trial.billed_user_id,
                        old_trial.created_at,
                        old_trial.deleted_at,
                        old_trial.superseded_by_trial_id
                    ) IS DISTINCT FROM (
                        new_trial.experiment_id,
                        new_trial.billed_user_id,
                        new_trial.created_at,
                        new_trial.deleted_at,
                        new_trial.superseded_by_trial_id
                    )
                )
                SELECT array_agg(DISTINCT candidate_id)
                INTO affected_ids
                FROM (
                    SELECT old_experiment_id AS candidate_id
                    FROM changed_trials
                    WHERE old_experiment_id IS NOT NULL
                    UNION
                    SELECT new_experiment_id
                    FROM changed_trials
                    WHERE new_experiment_id IS NOT NULL
                    UNION
                    SELECT et.experiment_id
                    FROM experiment_trials et
                    JOIN changed_trials changed ON changed.id = et.trial_id
                    WHERE et.deleted_at IS NULL
                ) candidates;
            END IF;

            IF affected_ids IS NOT NULL THEN
                PERFORM refresh_experiment_latest_runners(affected_ids);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_experiment_runners_from_memberships()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_ids text[];
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT array_agg(DISTINCT experiment_id)
                INTO affected_ids
                FROM new_memberships;
            ELSIF TG_OP = 'DELETE' THEN
                SELECT array_agg(DISTINCT experiment_id)
                INTO affected_ids
                FROM old_memberships;
            ELSE
                SELECT array_agg(DISTINCT candidate_id)
                INTO affected_ids
                FROM (
                    SELECT old_membership.experiment_id AS candidate_id
                    FROM old_memberships old_membership
                    JOIN new_memberships new_membership
                      ON new_membership.experiment_id = old_membership.experiment_id
                     AND new_membership.trial_id = old_membership.trial_id
                    WHERE old_membership.deleted_at IS DISTINCT FROM
                          new_membership.deleted_at
                ) candidates;
            END IF;

            IF affected_ids IS NOT NULL THEN
                PERFORM refresh_experiment_latest_runners(affected_ids);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_insert ON trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_trials_refresh_experiment_runner_insert
        AFTER INSERT ON trials
        REFERENCING NEW TABLE AS new_trials
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_trials()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_update ON trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_trials_refresh_experiment_runner_update
        AFTER UPDATE ON trials
        REFERENCING OLD TABLE AS old_trials NEW TABLE AS new_trials
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_trials()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_delete ON trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_trials_refresh_experiment_runner_delete
        AFTER DELETE ON trials
        REFERENCING OLD TABLE AS old_trials
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_trials()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_insert "
        "ON experiment_trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_experiment_trials_refresh_runner_insert
        AFTER INSERT ON experiment_trials
        REFERENCING NEW TABLE AS new_memberships
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_memberships()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_update "
        "ON experiment_trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_experiment_trials_refresh_runner_update
        AFTER UPDATE ON experiment_trials
        REFERENCING OLD TABLE AS old_memberships NEW TABLE AS new_memberships
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_memberships()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_delete "
        "ON experiment_trials"
    )
    op.execute(
        """
        CREATE TRIGGER trg_experiment_trials_refresh_runner_delete
        AFTER DELETE ON experiment_trials
        REFERENCING OLD TABLE AS old_memberships
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_experiment_runners_from_memberships()
        """
    )

    # Backfill home and gathered collection membership with the same ordering
    # the dashboard used before this projection existed.
    op.execute(
        """
        WITH memberships AS (
            SELECT
                tr.experiment_id,
                tr.id AS trial_id,
                tr.billed_user_id,
                tr.created_at
            FROM trials tr
            WHERE tr.experiment_id IS NOT NULL
              AND tr.deleted_at IS NULL
              AND tr.superseded_by_trial_id IS NULL

            UNION ALL

            SELECT
                et.experiment_id,
                tr.id AS trial_id,
                tr.billed_user_id,
                tr.created_at
            FROM experiment_trials et
            JOIN trials tr ON tr.id = et.trial_id
            WHERE et.deleted_at IS NULL
              AND tr.deleted_at IS NULL
              AND tr.superseded_by_trial_id IS NULL
        ),
        latest AS (
            SELECT DISTINCT ON (experiment_id)
                experiment_id,
                trial_id,
                billed_user_id,
                created_at
            FROM memberships
            ORDER BY experiment_id, created_at DESC, trial_id DESC
        )
        UPDATE experiments e
        SET last_runner_trial_id = latest.trial_id,
            last_runner_user_id = latest.billed_user_id,
            last_runner_at = latest.created_at
        FROM latest
        WHERE e.id = latest.experiment_id
          AND e.deleted_at IS NULL
        """
    )

    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_experiments_org_last_runner_activity_live
            ON experiments (
                org_id,
                last_runner_user_id,
                last_activity_at DESC NULLS LAST,
                id ASC
            )
            WHERE deleted_at IS NULL
              AND shadow_of IS NULL
              AND last_runner_user_id IS NOT NULL
            """
        )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_delete "
        "ON experiment_trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_update "
        "ON experiment_trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiment_trials_refresh_runner_insert "
        "ON experiment_trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_delete ON trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_update ON trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trials_refresh_experiment_runner_insert ON trials"
    )
    op.execute("DROP FUNCTION IF EXISTS refresh_experiment_runners_from_memberships()")
    op.execute("DROP FUNCTION IF EXISTS refresh_experiment_runners_from_trials()")
    op.execute("DROP FUNCTION IF EXISTS refresh_experiment_latest_runners(text[])")

    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_experiments_org_last_runner_activity_live"
        )
    op.drop_column("experiments", "last_runner_at")
    op.drop_column("experiments", "last_runner_user_id")
    op.drop_column("experiments", "last_runner_trial_id")
