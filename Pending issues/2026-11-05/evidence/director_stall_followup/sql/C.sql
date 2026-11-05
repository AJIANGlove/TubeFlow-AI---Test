--  Q3 全量：已通过（非复用）单元内相邻对实际改了哪些轴
with s as (
  select u.run_id, u.ordinal, e.idx, e.shot
  from project_director_units u
  cross join lateral jsonb_array_elements(u.shots_json::jsonb) with ordinality as e(shot, idx)
  where u.project_id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                         'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                         'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
    and u.kind = 'SHOT_PLAN' and u.status = 'SUCCEEDED' and u.reused_from_unit_id is null
), p as (
  select run_id, ordinal, idx, shot as cur,
         lag(shot) over (partition by run_id, ordinal order by idx) as prev
  from s
)
select (prev->>'semantic_event'    <> cur->>'semantic_event')    as event_changed,
       (prev->>'subject_focus'     <> cur->>'subject_focus')     as focus_changed,
       (prev->>'time_relation'     <> cur->>'time_relation')     as time_changed,
       (prev->>'location_relation' <> cur->>'location_relation') as location_changed,
       count(*)
from p where prev is not null
group by 1, 2, 3, 4 order by count(*) desc;
