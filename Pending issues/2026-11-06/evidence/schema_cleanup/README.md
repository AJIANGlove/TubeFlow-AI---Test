# 遗留测试 schema：定位、停删与盘点（2026-11-06）

对象：`contract_be30c85aac3347ffbf3bc45bc6ea12de`（集成测试夹具遗留的空 schema）。
授权：用户 2026-11-06 授权删除，**范围仅此一个**，并附硬限制「若在业务库 `tubeflow`：立即停下，只汇报，不删除」。

术语：**部署机** = 192.168.110.200；**测试机** = 实施方机器。全程只读（除运维记录文件的写入外），未调用任何 Provider，未重启容器，未改 `.env`。

## 1. 结论

**没有删除。** 该 schema 不在测试库，而在**业务库 `tubeflow`**；按授权里的硬限制，定位到业务库时立即停止，`b)–e)` 全部未执行。

## 2. 关键事实

| 项 | 结果 |
| --- | --- |
| 测试库 `tubeflowtest`（容器 `tubeflow-contract-pg`，角色 `postgres`，宿主 127.0.0.1:55432） | 该 schema **0 命中** |
| 业务库 `tubeflow`（容器 `tubeflowai-test-postgres-1`，角色 `tubeflow`） | **1 命中** = 该 schema |
| 业务库内该 schema 的对象数（仅信息性） | `pg_class` 0 / `pg_proc` 0 / `pg_type` 0（空） |
| 是否执行 `DROP` | **否** |
| 数据库写操作 | **0 次**（只有 `SELECT`） |

**注意与任务预期的差异**：任务里写「预期只有 `contract_be30c85a…` 一个」是就**测试库**而言；实测测试库为 **0 行**，那一个在业务库。这也是停删的直接原因。

## 3. 其他遗留（只读，未删除）

| 查询 | 结果 |
| --- | --- |
| 业务库 `contract\_%` | 1 行：`contract_be30c85aac3347ffbf3bc45bc6ea12de` |
| 业务库 `identity\_test\_%` / `narration\_authority\_%` / `voice\_race\_%` | 0 行 |
| 测试库 上述三类前缀 + `contract\_%` | 0 行 |
| 业务库集群 `datname like 'alembic\_url\_%'` | 0 行 |
| 测试库集群 `datname like 'alembic\_url\_%'` | 0 行 |
| 业务库非系统 schema（额外观察） | `audit_272879e_20260829a`、`audit_272879e_exact_20260829a`、`public`、`staged_cleanup_gate`、`contract_be30c85aac3347ffbf3bc45bc6ea12de` |

`audit_*` 与 `staged_cleanup_gate` 不在任务列出的前缀内，仅列出，未做任何处理。

## 4. 夹具修复的效果

PR #16 已合并（`55a92a3`）：`postgres_database` 把 `alembic upgrade` 与新引擎创建移进 `try`，`finally` 里 `engine` 为 `None` 时跳过 `dispose`，随后一律 `DROP SCHEMA … CASCADE`。
本次两轮集成（main 用旧夹具、分支用新夹具）跑完后，测试库 `contract\_%` 复查仍为 **0 行**——本轮没有新的泄漏。

## 5. 目录

| 文件 | 内容 |
| --- | --- |
| `gates/SUMMARY.md` | 门禁逐项对比、新单测实跑结果、两库查询 |
| `gates/{main,branch}/` | 两侧单元、集成、ruff、mypy 原始输出与失败集合 |
| `queries.txt` | 本次全部只读 SQL 与原始输出 |
| `operations-record.md` | 部署机 `runtime/operations/schema-cleanup-20261106/README.md` 的镜像副本（AGENT_AUTHORIZATION §5 六项） |

## 6. 后续

- 该空 schema 仍在业务库中。**删除需要新的、明确指向业务库的授权**；本任务不具此权限。
- 若后续要处理，建议同时确认业务库内 `audit_272879e_*`、`staged_cleanup_gate` 的归属（本次未查证来源，只列出）。
