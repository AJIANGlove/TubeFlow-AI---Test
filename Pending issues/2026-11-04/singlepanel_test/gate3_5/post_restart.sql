-- Gate 3/5 post-restart snapshot (read-only). One JSON document.
-- Usage: psql -v t0='2026-11-04T13:41:55Z' -At -f - < post_restart.sql
with touched as (
  select v.project_id, v.stage, v.archive_stage, v.error_code, v.attempt_id, v.group_id, v.updated_at
  from public.video_generation_attempts v
  where v.updated_at >= :'t0'::timestamptz
),
new_sb as (
  select s.project_id, coalesce(s.input_json->>'grid_layout_version','(none)') as glv, s.status, s.id
  from public.storyboard_generations s
  where s.created_at >= :'t0'::timestamptz
)
select jsonb_pretty(jsonb_build_object(
  't0', :'t0',
  'captured_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'attempts_touched', (select coalesce(jsonb_agg(jsonb_build_object(
      'project_id', project_id, 'stage', stage, 'archive_stage', archive_stage, 'n', n) order by n desc),'[]'::jsonb)
    from (select project_id, stage, archive_stage, count(*) as n from touched group by 1,2,3) s),
  'attempts_touched_total', (select count(*) from touched),
  'attempts_flagged', (select coalesce(jsonb_agg(jsonb_build_object(
      'attempt_id', attempt_id, 'project_id', project_id, 'group_id', group_id,
      'stage', stage, 'archive_stage', archive_stage, 'error_code', error_code,
      'updated_at', updated_at) order by updated_at),'[]'::jsonb)
    from touched
    where stage in ('FAILED','REMOTE_STATE_UNKNOWN')
       or archive_stage = 'QC_FAILED'
       or coalesce(error_code,'') like 'local_integrity_blocked:%'),
  'new_storyboards', (select coalesce(jsonb_agg(jsonb_build_object(
      'project_id', project_id, 'grid_layout_version', glv, 'status', status, 'n', n) order by n desc),'[]'::jsonb)
    from (select project_id, glv, status, count(*) as n from new_sb group by 1,2,3) s),
  'new_storyboards_total', (select count(*) from new_sb),
  'new_storyboards_with_v2', (select count(*) from new_sb where glv = 'grid-v2'),
  'new_storyboards_with_any_key', (select count(*) from new_sb where glv <> '(none)')
));
