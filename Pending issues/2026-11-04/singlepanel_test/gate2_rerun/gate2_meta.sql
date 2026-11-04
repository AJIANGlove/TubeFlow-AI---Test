select jsonb_pretty(jsonb_build_object(
  'captured_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'v2_keys', (select count(*) from public.storyboard_generations where jsonb_exists(input_json::jsonb, 'grid_layout_version')),
  'v2_keys_non_null', (select count(*) from public.storyboard_generations where input_json->>'grid_layout_version' is not null),
  'storyboard_rows', (select count(*) from public.storyboard_generations),
  'storyboard_succeeded', (select count(*) from public.storyboard_generations where status='SUCCEEDED'),
  'video_attempt_rows', (select count(*) from public.video_generation_attempts),
  'auto_runs', (select jsonb_agg(jsonb_build_object('project_id',project_id,'state',state,'round',authorization_round,'phase',payload->>'phase','reason',reason_code,'updated_at',updated_at) order by updated_at desc) from public.auto_pipeline_runs where state in ('RUNNING','PAUSED')),
  'non_terminal_video', (select jsonb_build_object('count',count(*)) from public.video_generation_attempts where stage not in ('ARCHIVED','FAILED','CANCELLED')),
  'active_storyboards', (select jsonb_build_object('count',count(*)) from public.storyboard_generations where status not in ('SUCCEEDED','FAILED'))
));
