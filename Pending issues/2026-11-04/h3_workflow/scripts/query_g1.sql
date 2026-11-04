-- Read-only export: G1 isolated project's H3 attempts.
-- video_generation_attempts has no created_at; provider_audit_events.occurred_at
-- is used only as an observation window, and all project attempts are returned.
WITH att AS (
  SELECT a.attempt_id,
         a.group_id,
         a.node_id,
         a.stage,
         a.archive_stage,
         a.request_json,
         a.input_snapshot_json,
         (SELECT min(e.occurred_at) FROM provider_audit_events e
           WHERE e.attempt_id = a.attempt_id) AS first_event_at,
         (SELECT max(e.occurred_at) FROM provider_audit_events e
           WHERE e.attempt_id = a.attempt_id) AS last_event_at,
         (SELECT e.workflow_id FROM provider_audit_events e
           WHERE e.attempt_id = a.attempt_id
           ORDER BY e.event_sequence LIMIT 1) AS audit_workflow_id,
         (SELECT e.workflow_version FROM provider_audit_events e
           WHERE e.attempt_id = a.attempt_id
           ORDER BY e.event_sequence LIMIT 1) AS audit_workflow_version
  FROM video_generation_attempts a
  WHERE a.project_id = 'dd4f88ef-bbe4-5ee4-ac00-bede0e5a507f'
)
SELECT jsonb_pretty(jsonb_build_object(
  'project_id', 'dd4f88ef-bbe4-5ee4-ac00-bede0e5a507f',
  'exported_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'attempt_count', (SELECT count(*) FROM att),
  'attempts', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'attempt_id', attempt_id,
      'group_id', group_id,
      'node_id', node_id,
      'stage', stage,
      'archive_stage', archive_stage,
      'first_event_at', first_event_at,
      'last_event_at', last_event_at,
      'audit_workflow_id', audit_workflow_id,
      'audit_workflow_version', audit_workflow_version,
      'request_json_workflow', request_json->'workflow',
      'request_json_parameters', request_json->'parameters',
      'references_source', CASE
        WHEN request_json ? 'references' THEN 'request_json.references'
        WHEN input_snapshot_json->'logical_request' ? 'references'
          THEN 'input_snapshot_json.logical_request.references'
        ELSE 'not found'
      END,
      'references', COALESCE(
        request_json->'references',
        input_snapshot_json->'logical_request'->'references',
        '[]'::jsonb
      ),
      'reference_count', COALESCE(
        jsonb_array_length(request_json->'references'),
        jsonb_array_length(input_snapshot_json->'logical_request'->'references'),
        0
      ),
      'reference_binding_keys', (
        SELECT jsonb_agg(r->>'binding_key' ORDER BY (r->>'ordinal')::int)
        FROM jsonb_array_elements(COALESCE(
          request_json->'references',
          input_snapshot_json->'logical_request'->'references',
          '[]'::jsonb
        )) AS r
      ),
      'reference_policy_strategy', input_snapshot_json->'reference_policy'->>'strategy',
      'reference_derivation_json', input_snapshot_json->'reference_policy'->'derivation',
      'request_json_staged_assets', request_json->'staged_assets'
    ) ORDER BY first_event_at NULLS LAST, attempt_id)
    FROM att
  ), '[]'::jsonb)
));
