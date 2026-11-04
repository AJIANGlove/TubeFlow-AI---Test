-- Read-only export: project fa3a0b30 current group video routing evidence.
-- request_json.references is absent after staging; fall back to the immutable
-- logical request stored in input_snapshot_json.logical_request.references.
WITH grp AS (
  SELECT g.sequence,
         g.id AS group_id,
         v.attempt_id,
         v.updated_at AS pointer_updated_at,
         a.node_id,
         a.stage,
         a.archive_stage,
         a.superseded_attempt_id,
         a.request_json,
         a.input_snapshot_json
  FROM authoritative_narration_groups g
  JOIN group_current_videos v ON v.group_id = g.id
  JOIN video_generation_attempts a ON a.attempt_id = v.attempt_id
  WHERE g.project_id = 'fa3a0b30-9222-4ca4-a475-3bfd6cc57220'
),
missing AS (
  SELECT g.sequence
  FROM authoritative_narration_groups g
  LEFT JOIN group_current_videos v ON v.group_id = g.id
  WHERE g.project_id = 'fa3a0b30-9222-4ca4-a475-3bfd6cc57220'
    AND v.group_id IS NULL
)
SELECT jsonb_pretty(jsonb_build_object(
  'project_id', 'fa3a0b30-9222-4ca4-a475-3bfd6cc57220',
  'exported_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'group_count', (SELECT count(*) FROM grp),
  'groups', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'sequence', sequence,
      'group_id', group_id,
      'attempt_id', attempt_id,
      'node_id', node_id,
      'stage', stage,
      'archive_stage', archive_stage,
      'superseded_attempt_id', superseded_attempt_id,
      'group_current_videos_updated_at', pointer_updated_at,
      'request_json_workflow', request_json->'workflow',
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
      'request_json_top_level_keys', (
        SELECT jsonb_agg(keys.k ORDER BY keys.k)
        FROM jsonb_object_keys(request_json) AS keys(k)
      ),
      'request_json_staged_assets', request_json->'staged_assets'
    ) ORDER BY sequence)
    FROM grp
  ), '[]'::jsonb),
  'route', (
    SELECT to_jsonb(r)
    FROM h3_project_workflow_routes r
    WHERE r.project_id = 'fa3a0b30-9222-4ca4-a475-3bfd6cc57220'
  ),
  'missing_groups', COALESCE((
    SELECT jsonb_agg(sequence ORDER BY sequence) FROM missing
  ), '[]'::jsonb)
));
