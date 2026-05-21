#!/usr/bin/env bash
# Quiesce in-flight work copied from production into a freshly-created
# Supabase preview branch.
#
# This should run only when branch_was_created=true. Existing preview
# branches may contain intentional work created by a reviewer, so repeated
# pushes must not cancel them again.
set -euo pipefail

: "${ODDISH_DATABASE_URL:?}"

psql "${ODDISH_DATABASE_URL/+asyncpg/}" <<'SQL'
UPDATE worker_jobs
   SET status = 'CANCELLED'
 WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED');
UPDATE tasks
   SET status = 'failed'
 WHERE status IN ('pending', 'running', 'analyzing', 'verdict_pending');
UPDATE trials
   SET status = 'failed'
 WHERE status IN ('pending', 'queued', 'running', 'retrying');
-- Record the preview cutover so the reaper (which runs in previews too)
-- only respawns rows created AFTER this moment. Anything older is
-- cloned-from-prod and must not be touched.
INSERT INTO oddish_runtime_config (key, value)
VALUES ('preview_epoch_at', NOW()::text)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
SQL
