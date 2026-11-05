-- crop-grid-v2 拒收导致 AUTO 暂停：取证 SQL（只读）
-- 目标项目：You're the Explorer Who Found Machu Picchu
-- 用法（部署机）：
--   docker exec -i tubeflowai-test-postgres-1 \
--     psql -U tubeflow -d tubeflow -A -F'|' -f - < collect.sql

\set pid '247462a4-e719-4488-ae14-aab962de1a27'
\echo '=== 1. AUTO 运行状态 ==='
select state, reason_code, authorization_round,
       payload->>'phase' as phase,
       to_char(to_timestamp((payload->>'paused_at_ms')::bigint / 1000) at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS') as paused_utc,
       payload->>'failure_code' as failure_code,
       payload->'group_sequences' as group_sequences,
       updated_at
from auto_pipeline_runs where project_id = :'pid';

\echo '=== 2. 故事板生成计数 ==='
select status, kind, count(*), min(created_at), max(created_at)
from storyboard_generations where project_id = :'pid'
group by 1, 2 order by 3 desc;

\echo '=== 3. 失败码分布 ==='
select coalesce(checkpoint_json->>'failure_code', '-') as code,
       coalesce(checkpoint_json->>'failure_stage', '-') as stage,
       count(*), min(created_at), max(created_at)
from storyboard_generations
where project_id = :'pid' and status = 'FAILED'
group by 1, 2 order by 3 desc;

\echo '=== 4. 分组结果（成功/失败/预算是否打光） ==='
with g as (
  select group_id,
         count(*) filter (where status = 'SUCCEEDED') as ok,
         max(failover_total_attempt) as max_total,
         max(created_at) as last_at
  from storyboard_generations where project_id = :'pid'
  group by 1
)
select count(*) as groups,
       count(*) filter (where ok > 0) as with_success,
       count(*) filter (where ok = 0) as no_success,
       count(*) filter (where ok = 0 and max_total >= 14) as exhausted
from g;

\echo '=== 5. 按 provider/model 的成功与拒收 ==='
select status, provider, input_json->>'model' as model, count(*) as n,
       count(*) filter (where checkpoint_json->>'failure_code' = 'storyboard_panel_layout_unreadable') as unreadable
from storyboard_generations where project_id = :'pid'
group by 1, 2, 3 order by 1, n desc;

\echo '=== 6. 单组 4 条 14 次预算链（恢复次数证据） ==='
select left(failover_chain_id::text, 8) as chain, count(*) as n,
       to_char(min(created_at) at time zone 'Asia/Shanghai', 'MM-DD HH24:MI') as t0,
       to_char(max(created_at) at time zone 'Asia/Shanghai', 'MM-DD HH24:MI') as t1
from storyboard_generations
where project_id = :'pid' and group_id::text like '9b6a530a%'
group by 1 order by 3;

\echo '=== 7. 本次取样的四张失败整图 ==='
select left(g.id::text, 8) as generation, left(g.group_id::text, 8) as grp,
       g.provider, g.input_json->>'model' as model,
       jsonb_array_length(g.input_json::jsonb->'shots') as panels,
       right(g.checkpoint_json->>'download_token', 32) as token_tail,
       to_char(g.created_at at time zone 'Asia/Shanghai', 'MM-DD HH24:MI:SS') as created
from storyboard_generations g
where g.project_id = :'pid'
  and g.checkpoint_json->>'download_token' in (
    'https://tos-hk.lingkeai.vip/uploads/2026.11/05/20261105125931_18e50d692f62b8feced3.jpg',
    'https://tos-hk.lingkeai.vip/uploads/2026.11/05/20261105130023_18e50d7517bd27f3c979.jpg',
    'https://tos-hk.lingkeai.vip/uploads/2026.11/05/20261105125915_18e50d65708670e85129.jpg',
    'https://tos-hk.lingkeai.vip/uploads/2026.11/05/20261105125923_18e50d6741b5d75b4515.jpg')
order by g.created_at;

\echo '=== 8. 取样的两张通过整图 ==='
select left(g.group_id::text, 8) as grp, g.asset_id,
       jsonb_array_length(g.input_json::jsonb->'shots') as panels,
       g.checkpoint_json->>'qc_width' as w, g.checkpoint_json->>'qc_height' as h,
       g.checkpoint_json->>'qc_mime' as mime,
       to_char(g.created_at at time zone 'Asia/Shanghai', 'MM-DD HH24:MI:SS') as created
from storyboard_generations g
where g.project_id = :'pid' and g.status = 'SUCCEEDED'
order by g.created_at desc limit 3;

\echo '=== 9. 近 7 天同码影响面（按项目） ==='
select p.id, left(p.title, 34) as title, count(*) as n, min(g.created_at), max(g.created_at)
from storyboard_generations g join projects p on p.id = g.project_id
where g.checkpoint_json->>'failure_code' = 'storyboard_panel_layout_unreadable'
  and g.created_at > now() - interval '7 days'
group by 1, 2 order by n desc limit 8;

\echo '=== 10. 开关取值（部署机 .env，非库内） ==='
\echo '见 README「环境」一节：TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true'
