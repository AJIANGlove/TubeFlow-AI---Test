-- 抖动分层导出（只读）。任务给定 SQL 的 from/join/where 逐字不变，
-- select 列表仅追加 g.project_id 与 g.sequence，用于「DB 有 / survey 无」的归因。
\set ON_ERROR_STOP on
SET default_transaction_read_only = on;
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '300s';

COPY (
  select a.attempt_id::text                                as attempt_id,
         a.input_snapshot_json->'video_prompt'->>'strategy' as strategy,
         p.visual_style                                    as visual_style,
         g.project_id::text                                 as project_id,
         g.sequence                                         as sequence
  from group_current_videos c
  join video_generation_attempts a on a.attempt_id = c.attempt_id
  join authoritative_narration_groups g on g.id = c.group_id
  join projects p on p.id = g.project_id
  where a.updated_at >= '2026-10-01'
) TO STDOUT WITH (FORMAT csv, HEADER true, DELIMITER E'\t', ENCODING 'UTF8');

COMMIT;
