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
SQL
