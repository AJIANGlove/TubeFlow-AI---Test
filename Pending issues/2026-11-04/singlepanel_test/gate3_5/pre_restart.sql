-- Gate 3/5 pre-restart snapshot (read-only). One JSON document.
-- Usage: docker exec -i tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow -At -f - < pre_restart.sql
with runs as (
  select a.project_id, a.state, a.authorization_round, a.reason_code,
         a.payload->>'phase' as phase, a.updated_at
  from public.auto_pipeline_runs a
  where a.state in ('RUNNING','PAUSED')
),
vif as (
  select v.project_id, v.stage, v.archive_stage, count(*) as n
  from public.video_generation_attempts v
  where v.stage not in ('FAILED','CANCELLED','ARCHIVED')
    and v.archive_stage <> 'QC_FAILED'
    and coalesce(v.error_code,'') not like 'local_integrity_blocked:%'
  group by 1,2,3
),
sif as (
  select s.project_id, count(*) as n
  from public.storyboard_generations s
  where s.status not in ('SUCCEEDED','FAILED')
  group by 1
),
base as (
  select p.id as project_id, p.title, p.production_mode, p.created_at,
         (select count(*) from public.storyboard_generations s where s.project_id=p.id) as sb_rows,
         (select count(*) from public.storyboard_generations s where s.project_id=p.id and s.status='SUCCEEDED') as sb_ok,
         (select count(*) from public.group_current_videos c
            join public.authoritative_narration_groups g on g.id=c.group_id
           where g.project_id=p.id) as current_videos
  from public.projects p
)
select jsonb_pretty(jsonb_build_object(
  'captured_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'auto_runs', (select coalesce(jsonb_agg(jsonb_build_object(
      'project_id', project_id, 'state', state, 'round', authorization_round,
      'phase', phase, 'reason', reason_code, 'updated_at', updated_at) order by updated_at desc),'[]'::jsonb)
    from runs),
  'video_in_flight', (select coalesce(jsonb_agg(jsonb_build_object(
      'project_id', project_id, 'stage', stage, 'archive_stage', archive_stage, 'n', n) order by n desc),'[]'::jsonb)
    from vif),
  'video_in_flight_total', (select coalesce(sum(n),0) from vif),
  'storyboards_in_flight', (select coalesce(jsonb_agg(jsonb_build_object('project_id', project_id, 'n', n) order by n desc),'[]'::jsonb)
    from sif),
  'storyboards_in_flight_total', (select coalesce(sum(n),0) from sif),
  'projects', (select jsonb_agg(jsonb_build_object(
      'project_id', b.project_id,
      'title', b.title,
      'production_mode', b.production_mode,
      'created_at', b.created_at,
      'auto_state', coalesce(r.state,'-'),
      'auto_round', r.authorization_round,
      'auto_reason', r.reason_code,
      'auto_phase', r.phase,
      'storyboard_rows', b.sb_rows,
      'storyboards_succeeded', b.sb_ok,
      'current_videos', b.current_videos,
      'in_flight_storyboards', coalesce(si.n,0),
      'in_flight_videos', coalesce(vi.n,0),
      'class', (case when b.sb_rows > 0 then 'B' else 'C' end)
               || (case when r.state in ('RUNNING','PAUSED') or coalesce(si.n,0) > 0 or coalesce(vi.n,0) > 0 then '+A' else '' end)
    ) order by b.created_at desc)
    from base b
    left join (select project_id, state, authorization_round, reason_code, phase from runs) r on r.project_id=b.project_id
    left join sif si on si.project_id=b.project_id
    left join (select project_id, sum(n) as n from vif group by 1) vi on vi.project_id=b.project_id)
));
