"""Raw bounded queries for the task-page first-paint resource."""

from sqlalchemy import text

IDENTITY_SQL = text(
    """
    WITH identity AS (
      SELECT t.id AS task_id, t.name, lower(t.status::text) AS status,
             lower(t.priority::text) AS priority, t."user", t.task_path, t.link,
             t.tags, t.current_version_id, t.run_analysis,
             lower(t.verdict_status::text) AS verdict_status,
             t.verdict, t.verdict_error,
             t.effective_tag_ids, t.current_version_tag_ids,
             t.created_at, t.updated_at, t.org_id,
             dv.id AS default_version_id, dv.version AS default_version,
             dv.message AS default_version_message,
             dv.created_at AS default_version_created_at,
             sv.id AS selected_version_id, sv.version AS selected_version,
             sv.message AS selected_version_message,
             sv.created_at AS selected_version_created_at
      FROM tasks t
      LEFT JOIN task_versions dv ON dv.id = t.current_version_id
        AND dv.task_id = t.id AND dv.deleted_at IS NULL
      LEFT JOIN task_versions sv
        ON sv.id = COALESCE(CAST(:version_id AS text), t.current_version_id)
        AND sv.task_id = t.id AND sv.deleted_at IS NULL
      WHERE t.id = :task_id AND t.deleted_at IS NULL
        AND (CAST(:org_id AS text) IS NULL OR t.org_id = :org_id)
      LIMIT 1
    )
    SELECT i.*,
      COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
        SELECT e.id, e.name FROM task_experiments te
        JOIN experiments e ON e.id = te.experiment_id
        WHERE te.task_id = i.task_id AND te.deleted_at IS NULL
          AND e.deleted_at IS NULL AND (i.org_id IS NULL OR e.org_id = i.org_id)
        ORDER BY e.name, e.id LIMIT 20
      ) x), '[]'::jsonb) AS experiments,
      COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
        SELECT canonical.id AS tag_id, canonical.key, canonical.value,
               canonical.color, canonical.visibility::text AS visibility,
               bool_or(raw.id = ANY(i.current_version_tag_ids)) AS current,
               bool_or(NOT (raw.id = ANY(i.current_version_tag_ids))) AS older
        FROM unnest(i.effective_tag_ids) AS effective(tag_id)
        JOIN tags raw ON raw.id = effective.tag_id
        JOIN tags canonical ON canonical.id = COALESCE(raw.merged_into_id, raw.id)
        WHERE raw.deleted_at IS NULL AND canonical.deleted_at IS NULL
          AND canonical.state <> 'DELETED'
          AND (i.org_id IS NULL OR canonical.org_id = i.org_id)
        GROUP BY canonical.id, canonical.key, canonical.value,
                 canonical.color, canonical.visibility
        ORDER BY canonical.key, canonical.id LIMIT 50
      ) x), '[]'::jsonb) AS task_tags,
      COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
        SELECT canonical.id AS tag_id, canonical.key, canonical.value,
               canonical.color, canonical.visibility::text AS visibility,
               true AS current, false AS older
        FROM tag_assignments assignment
        JOIN tags raw ON raw.id = assignment.tag_id
        JOIN tags canonical ON canonical.id = COALESCE(raw.merged_into_id, raw.id)
        WHERE assignment.scope = 'VERSION'
          AND assignment.target_id = i.selected_version_id
          AND assignment.state = 'ACTIVE'
          AND assignment.deleted_at IS NULL
          AND canonical.deleted_at IS NULL
          AND canonical.state <> 'DELETED'
          AND (i.org_id IS NULL OR canonical.org_id = i.org_id)
        GROUP BY canonical.id, canonical.key, canonical.value,
                 canonical.color, canonical.visibility
        ORDER BY canonical.key, canonical.id LIMIT 50
      ) x), '[]'::jsonb) AS selected_version_tags
    FROM identity i
    """
)

AGGREGATE_SQL = text(
    """
    WITH qa_rows AS (
      SELECT ac.id, COALESCE(ac.cost_usd, 0.0) AS cost
      FROM analysis_costs ac
      WHERE ac.deleted_at IS NULL AND ac.task_id = :task_id
        AND (CAST(:org_id AS text) IS NULL OR ac.org_id = :org_id)
      UNION
      SELECT ac.id, COALESCE(ac.cost_usd, 0.0) AS cost
      FROM analysis_costs ac JOIN trials qat ON qat.id = ac.trial_id
      WHERE ac.deleted_at IS NULL AND qat.task_id = :task_id
        AND (CAST(:org_id AS text) IS NULL OR ac.org_id = :org_id)
    ), eligible AS (
      SELECT tr.*,
        (tr.cost_usd IS NULL AND
         (COALESCE(tr.input_tokens, 0) > 0 OR COALESCE(tr.output_tokens, 0) > 0
          OR COALESCE(tr.cache_write_tokens, 0) > 0)) AS has_estimatable_tokens
      FROM trials tr
      WHERE tr.task_id = :task_id AND tr.deleted_at IS NULL
        AND tr.superseded_by_trial_id IS NULL
        AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
        AND (CAST(:org_id AS text) IS NULL OR tr.org_id = :org_id)
    ), groups AS (
      SELECT (tr.task_version_id = CAST(:version_id AS text)) AS is_selected,
        (CAST(:current_version_id AS text) IS NULL
         OR tr.task_version_id = CAST(:current_version_id AS text)) AS is_current,
        (tr.billed_user_id IS NOT NULL) AS is_billed, tr.is_probe,
        tr.agent, tr.provider, tr.model, count(*) AS total,
        count(*) FILTER (WHERE tr.status = 'SUCCESS') AS completed,
        count(*) FILTER (WHERE tr.status = 'FAILED') AS failed,
        count(*) FILTER (WHERE tr.status = 'SKIPPED') AS skipped,
        count(*) FILTER (WHERE tr.status = 'SUCCESS' AND tr.reward = 1) AS pass_count,
        count(*) FILTER (WHERE tr.status = 'SUCCESS' AND tr.reward NOT IN (0, 1)) AS partial_count,
        count(*) FILTER (WHERE tr.status = 'SUCCESS' AND tr.reward = 0) AS fail_count,
        COALESCE(sum(tr.reward) FILTER (WHERE tr.status = 'SUCCESS'), 0.0) AS reward_sum,
        count(tr.reward) FILTER (WHERE tr.status = 'SUCCESS') AS reward_total,
        max(COALESCE(tr.finished_at, tr.started_at, tr.created_at)) AS last_run_at,
        COALESCE(sum(EXTRACT(EPOCH FROM (tr.finished_at - tr.started_at)))
          FILTER (WHERE tr.started_at IS NOT NULL AND tr.finished_at IS NOT NULL
            AND tr.finished_at >= tr.started_at), 0.0) AS duration_sum_seconds,
        count(*) FILTER (WHERE tr.started_at IS NOT NULL
          AND tr.finished_at IS NOT NULL
          AND tr.finished_at >= tr.started_at) AS duration_trial_count,
        COALESCE(sum(GREATEST(COALESCE(tr.input_tokens, 0), 0)
                   + GREATEST(COALESCE(tr.output_tokens, 0), 0)), 0) AS token_count,
        count(*) FILTER (WHERE tr.input_tokens IS NOT NULL
                           OR tr.output_tokens IS NOT NULL) AS token_trial_count,
        COALESCE(sum(tr.cost_usd) FILTER (WHERE tr.cost_usd IS NOT NULL), 0.0) AS native_cost,
        count(tr.cost_usd) AS native_count,
        count(*) FILTER (WHERE tr.has_estimatable_tokens) AS estimated_count,
        COALESCE(sum(GREATEST(COALESCE(tr.input_tokens, 0)
          - GREATEST(COALESCE(tr.cache_tokens, 0), 0)
          - GREATEST(COALESCE(tr.cache_write_tokens, 0), 0), 0)
          + GREATEST(COALESCE(tr.cache_tokens, 0), 0)
          + GREATEST(COALESCE(tr.cache_write_tokens, 0), 0))
          FILTER (WHERE tr.has_estimatable_tokens), 0) AS estimated_input,
        COALESCE(sum(GREATEST(COALESCE(tr.output_tokens, 0), 0))
          FILTER (WHERE tr.has_estimatable_tokens), 0) AS estimated_output,
        COALESCE(sum(GREATEST(COALESCE(tr.cache_tokens, 0), 0))
          FILTER (WHERE tr.has_estimatable_tokens), 0) AS estimated_cache,
        COALESCE(sum(GREATEST(COALESCE(tr.cache_write_tokens, 0), 0))
          FILTER (WHERE tr.has_estimatable_tokens), 0) AS estimated_cache_write
      FROM eligible tr
      GROUP BY is_selected, is_current, is_billed, tr.is_probe,
               tr.agent, tr.provider, tr.model
    ), selected_experiments AS (
      SELECT DISTINCT e.id, e.name
      FROM eligible tr JOIN experiments e ON e.id = tr.experiment_id
      WHERE tr.task_version_id = CAST(:version_id AS text)
        AND tr.is_probe IS NOT TRUE AND e.deleted_at IS NULL
        AND (CAST(:org_id AS text) IS NULL OR e.org_id = :org_id)
    )
    SELECT COALESCE((SELECT jsonb_agg(to_jsonb(g)) FROM (
             SELECT * FROM groups ORDER BY is_selected DESC, is_probe,
               agent, model, provider, is_billed
           ) g), '[]'::jsonb) AS groups,
           COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM (
             SELECT id, name FROM selected_experiments ORDER BY name, id
           ) e), '[]'::jsonb) AS experiments,
           (SELECT COALESCE(sum(cost), 0.0) FROM qa_rows) AS qa_cost_usd
    """
)

PREVIEW_SQL = text(
    """
    SELECT tr.id, tr.name, tr.experiment_id, tr.task_version_id, tr.agent,
      tr.provider, tr.model, lower(tr.status::text) AS status, tr.reward,
      CASE WHEN tr.error_message IS NULL THEN NULL
           WHEN tr.error_message LIKE '%AgentTimeoutError%'
             OR tr.error_message LIKE '%Agent execution timed out%' THEN 'timeout'
           ELSE 'error' END AS error_kind,
      tr.is_probe, tr.cost_usd, tr.input_tokens, tr.output_tokens,
      tr.cache_tokens, tr.cache_write_tokens, tr.billed_user_id,
      tr.created_at, tr.started_at, tr.finished_at
    FROM trials tr
    WHERE tr.task_id = :task_id AND tr.task_version_id = :version_id
      AND tr.deleted_at IS NULL AND tr.superseded_by_trial_id IS NULL
      AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
      AND (CAST(:org_id AS text) IS NULL OR tr.org_id = :org_id)
    ORDER BY COALESCE(tr.finished_at, tr.started_at, tr.created_at) DESC, tr.id DESC
    LIMIT 21
    """
)
