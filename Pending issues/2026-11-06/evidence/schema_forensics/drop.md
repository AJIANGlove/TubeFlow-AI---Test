删除操作记录（部署机，2026-11-06）

授权：用户 2026-11-06「全部按建议执行」（见复核文档 `SCHEMA_CLEANUP_REVIEW_20261106.md` 第 3 节），
      范围**仅限** `contract_be30c85aac3347ffbf3bc45bc6ea12de` 一个 schema。
运维记录（AGENT_AUTHORIZATION §5）：部署机 `runtime/operations/schema-forensics-20261106/README.md`，
      仓库副本见本目录 `operations-record.md`。**先写记录、后执行。**

## 1. 删除前再次确认为空（07:07:31Z，业务库 tubeflow）

  exists = 1
  pg_class = 0
  pg_proc  = 0
  pg_type  = 0
  owner    = tubeflow

SQL：
  select count(*) from pg_namespace where nspname='contract_be30c85aac3347ffbf3bc45bc6ea12de';
  select count(*) from pg_class  c join pg_namespace n on n.oid=c.relnamespace  where n.nspname='contract_be30c85aac3347ffbf3bc45bc6ea12de';
  select count(*) from pg_proc   p join pg_namespace n on n.oid=p.pronamespace  where n.nspname='contract_be30c85aac3347ffbf3bc45bc6ea12de';
  select count(*) from pg_type   t join pg_namespace n on n.oid=t.typnamespace  where n.nspname='contract_be30c85aac3347ffbf3bc45bc6ea12de';

四项全部满足才继续。

## 2. 执行（07:07:35Z）

  $ docker exec tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow \
      -c 'DROP SCHEMA "contract_be30c85aac3347ffbf3bc45bc6ea12de" RESTRICT;'
  DROP SCHEMA
  rc = 0

未使用 CASCADE，未加 IF EXISTS，未切换超级用户。

## 3. 删除后复查（07:07:40Z）

| 项 | 结果 |
| --- | --- |
| 业务库该 schema 是否还在 | **0**（已删除） |
| 业务库 `contract\_%` | **0 行** |
| 测试库 `contract\_%` | **0 行** |
| 业务库剩余非系统 schema | audit_272879e_20260829a / audit_272879e_exact_20260829a / public / staged_cleanup_gate |
| `/health/live`（部署机 127.0.0.1:8000） | **200** |

未重启任何容器；服务未受影响（live 200）。
