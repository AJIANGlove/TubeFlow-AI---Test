-- Gate 1 population for the 2026-11-04 rerun (USER_DECISIONS §47 / FINAL_ACCEPTANCE §42).
-- Only storyboards whose prompt carries the d5c1a12 no-readable-text rule; the previous
-- round filtered with "not like '%Caption:%'", which pulled in 89 pre-d5c1a12 sheets.
-- Usage (deploy host):
--   docker exec -i tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow -At -f - < baseline.sql
select jsonb_pretty(jsonb_build_object(
  'captured_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'total', count(*),
  'rows', jsonb_agg(jsonb_build_object(
    'generation_id', g.id,
    'project_id', g.project_id,
    'project_title', p.title,
    'group_id', g.group_id,
    'asset_id', g.asset_id,
    'object_key', m.object_key,
    'sha256', m.sha256,
    'size_bytes', m.size_bytes,
    'shots', json_array_length(g.input_json->'shots'),
    'created_at', g.created_at
  ) order by g.project_id, g.created_at, g.id)
))
from public.storyboard_generations g
join public.projects p on p.id=g.project_id
join public.media_assets m on m.id=g.asset_id
where g.status='SUCCEEDED'
  and coalesce(g.input_json->>'prompt','') like '%Do not draw any readable text anywhere in the image%';
