-- Gate 2 window activity: rows created (storyboards) or touched (attempts) after the deploy.
-- video_generation_attempts has no created_at column, so new attempts are counted as the
-- delta of the per-group attempt_rows CSV snapshots; here we only report stage moves.
-- :t0 is passed as a psql variable, e.g.
--   psql -v t0='2026-11-04T09:45:08Z' -At -f - < gate2_window.sql
select jsonb_pretty(jsonb_build_object(
  't0', :'t0',
  'captured_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'new_storyboards', (select count(*) from public.storyboard_generations where created_at >= :'t0'::timestamptz),
  'new_storyboards_by_project', (select coalesce(jsonb_agg(jsonb_build_object('project_id',project_id,'n',n) order by n desc),'[]'::jsonb)
      from (select project_id::text as project_id, count(*) as n from public.storyboard_generations
            where created_at >= :'t0'::timestamptz group by 1) s),
  'new_storyboards_with_grid_layout_version', (select count(*) from public.storyboard_generations
      where created_at >= :'t0'::timestamptz and input_json->>'grid_layout_version' is not null),
  'storyboards_touched_by_project', (select coalesce(jsonb_agg(jsonb_build_object('project_id',project_id,'status',status,'n',n) order by n desc),'[]'::jsonb)
      from (select project_id::text as project_id, status, count(*) as n from public.storyboard_generations
            where created_at >= :'t0'::timestamptz group by 1,2) s),
  'attempts_touched_by_stage', (select coalesce(jsonb_agg(jsonb_build_object('project_id',project_id,'stage',stage,'n',n) order by n desc),'[]'::jsonb)
      from (select project_id::text as project_id, stage, count(*) as n from public.video_generation_attempts
            where updated_at >= :'t0'::timestamptz group by 1,2) s),
  'auto_runs', (select jsonb_agg(jsonb_build_object('project_id',project_id,'state',state,'round',authorization_round,'reason',reason_code,'updated_at',updated_at) order by updated_at desc)
      from public.auto_pipeline_runs where state in ('RUNNING','PAUSED'))
));
