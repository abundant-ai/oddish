-- One-time setup for the slackbot's read-only Postgres role. Run as an
-- admin against the SESSION pooler (port 5432, never 6543 -- DDL/session
-- state on the transaction pooler poisons shared backends).
--
-- Grants are the security boundary: the role can read exactly the curated
-- v_* views (created by backend migration sbviews_001) and no base tables.
-- The GUCs must live on the role, not the connection: Supavisor
-- transaction-mode pooling drops client-supplied server_settings (same
-- reason as apply_role_defaults() in oddish/src/oddish/db/connection.py).
--
-- After running, put the DSN in the oddish-slackbot Modal secret as
-- ODDISH_RO_DATABASE_URL (plain postgresql://, transaction pooler is fine
-- for the bot's queries). Re-run the GRANT after adding a new view; a
-- DROP + CREATE (unlike CREATE OR REPLACE) also drops the grant.

CREATE ROLE slackbot_ro LOGIN PASSWORD :'slackbot_ro_password';

GRANT SELECT ON
    v_trials,
    v_experiment_summary,
    v_daily_spend,
    v_quota_status,
    v_org_quota_status,
    v_queue_state,
    v_runtime_status
TO slackbot_ro;

ALTER ROLE slackbot_ro SET default_transaction_read_only = on;
ALTER ROLE slackbot_ro SET statement_timeout = '5s';
ALTER ROLE slackbot_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE slackbot_ro SET timezone = 'UTC';
