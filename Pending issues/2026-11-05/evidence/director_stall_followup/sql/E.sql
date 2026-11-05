-- E. 三个项目的全部导演运行及其 owner_id（工作流 ID = project-director-<owner_id>-<run_id>，
--    附录 A/B/C/D 都没有 owner_id，缺它拼不出工作流 ID）
select id as run_id, owner_id, project_id, created_at, status, failure_code, recovery_source_run_id
from project_director_runs
where project_id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                     'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                     'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
order by project_id, created_at;
