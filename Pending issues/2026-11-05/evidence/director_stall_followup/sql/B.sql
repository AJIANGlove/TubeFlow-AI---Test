--  结论 4：8697ee51 的失败单元与租约
select ordinal, status, attempt_count, last_failure_code, failure_code,
       lease_until, next_attempt_at, response_diagnostics_json
from project_director_units
where run_id = (select id from project_director_runs where id::text like '8697ee51%')
  and status in ('FAILED', 'REMOTE_STATE_UNKNOWN', 'SUBMITTING', 'LEASED');
