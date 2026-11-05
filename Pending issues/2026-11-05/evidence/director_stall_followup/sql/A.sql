--  三个项目所有导演运行中「未成功或有过拒收」的单元（Q2 的完整位置分布；补 d449aa5f）
select r.project_id, r.id as run_id, r.created_at, r.status as run_status,
       r.failure_code as run_failure, r.recovery_source_run_id,
       u.ordinal, u.kind, u.prompt_ref, u.status, u.attempt_count,
       u.last_failure_code, u.failure_code, u.response_diagnostics_json,
       (u.reused_from_unit_id is not null) as reused
from project_director_runs r
join project_director_units u on u.run_id = r.id
where r.project_id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                       'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                       'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
  and (u.status <> 'SUCCEEDED' or u.last_failure_code is not null)
order by r.project_id, r.created_at, u.ordinal;
