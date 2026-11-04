copy (
  select g.id::text as group_id,
         g.project_id::text as project_id,
         (select count(*) from public.storyboard_generations s where s.group_id=g.id) as sb_rows,
         (select coalesce(max(s.version),0) from public.storyboard_generations s where s.group_id=g.id) as sb_version_max,
         (select count(*) from public.video_generation_attempts a where a.group_id=g.id) as attempt_rows,
         (select coalesce(max(a.intent_sequence),0) from public.video_generation_attempts a where a.group_id=g.id) as intent_max,
         (select count(*) from public.group_current_videos c where c.group_id=g.id) as has_current
  from public.authoritative_narration_groups g
) to stdout with csv header;
