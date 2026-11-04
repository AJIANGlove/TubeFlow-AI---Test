-- One compact JSON line per monitoring tick (read-only).
-- Usage: psql -v p='<project uuid>' -At -f - < monitor.sql >> monitor.jsonl
with run as (
  select state, authorization_round, reason_code, payload->>'phase' as phase, updated_at
  from public.auto_pipeline_runs where project_id = :'p'
  order by updated_at desc limit 1
),
sb as (
  select status, count(*) as n from public.storyboard_generations where project_id = :'p' group by 1
),
sbv as (
  select coalesce(input_json->>'grid_layout_version','(none)') as glv, count(*) as n
  from public.storyboard_generations where project_id = :'p' group by 1
),
first_ok as (
  select coalesce(input_json->>'grid_layout_version','(none)') as glv, id, created_at
  from public.storyboard_generations
  where project_id = :'p' and status = 'SUCCEEDED' order by created_at limit 1
),
va as (
  select stage, count(*) as n from public.video_generation_attempts where project_id = :'p' group by 1
),
grp as (
  select count(*) as n, count(*) filter (where exists (
      select 1 from public.group_current_videos c where c.group_id = g.id)) as with_video
  from public.authoritative_narration_groups g where g.project_id = :'p'
),
sent as (
  select count(*) as n from public.authoritative_narration_sentences where project_id = :'p'
)
select jsonb_build_object(
  'at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'auto_state', (select state from run),
  'auto_round', (select authorization_round from run),
  'auto_phase', (select phase from run),
  'auto_reason', (select reason_code from run),
  'auto_updated_at', (select updated_at from run),
  'storyboards', (select coalesce(jsonb_object_agg(status, n),'{}'::jsonb) from sb),
  'storyboard_versions', (select coalesce(jsonb_object_agg(glv, n),'{}'::jsonb) from sbv),
  'first_succeeded_glv', (select glv from first_ok),
  'video_attempts', (select coalesce(jsonb_object_agg(stage, n),'{}'::jsonb) from va),
  'groups', (select n from grp),
  'groups_with_video', (select with_video from grp),
  'sentences', (select n from sent)
)::text;
