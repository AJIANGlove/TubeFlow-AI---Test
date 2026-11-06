# 运维操作记录：删除遗留测试 schema（schema-cleanup-20261106）

## 1. 操作身份与状态

- 操作 ID：`schema-cleanup-20261106`
- 任务目标：删除集成测试夹具遗留的**空** schema
- 精确对象：PostgreSQL schema `contract_be30c85aac3347ffbf3bc45bc6ea12de`（**仅此一个**）
- 状态：**未执行（按授权限制停止）** —— 对象位于业务库 `tubeflow`，授权明文要求此时立即停下

## 2. 用户授权证据

- 2026-11-06 用户指令原文要点：「删除遗留的那一个空 schema …… 用户 2026-11-06 授权，范围仅限这一个 schema」。
- 依据：`Pending issues/2026-11-06/MOTION_METRICS_ROLLOUT_REVIEW_20261106.md` 第 4 节（用户回复「按你推荐执行」）。
- 同一条授权里的硬限制：「**若在业务库 tubeflow：立即停下，只汇报，不删除**」；`DROP` 报错即停，**不得改用 CASCADE**。

## 3. 允许范围

- 动作：`DROP SCHEMA "contract_be30c85aac3347ffbf3bc45bc6ea12de" RESTRICT;`
- 前置：确认它在**测试库 `tubeflowtest`**；schema 内 `pg_class` / `pg_proc` / `pg_type` 三项计数均为 0；此刻无集成测试在跑。
- 禁止：删除任何其它 schema 或对象；删除业务库内任何东西；构建镜像、重启容器、改 `.env`；调用任何 Provider。

## 4. 上限与消耗

| 项 | 值 |
| --- | --- |
| 删除上限 | 1 个 schema |
| 实际删除 | **0**（本次，见第 5 节） |
| 未决尝试 | 无 |
| 生成额度 | **0**（本操作不涉及任何 Provider 调用） |

## 5. 实际执行证据

### 5.1 a) 定位（2026-11-06T06:26:10Z）

| 库 | 容器 / 角色 | 命中 |
| --- | --- | --- |
| `tubeflowtest`（测试库） | `tubeflow-contract-pg` / `postgres`（宿主 127.0.0.1:55432） | **0** |
| `tubeflow`（**业务库**） | `tubeflowai-test-postgres-1` / `tubeflow` | **1** |

查询：`select nspname from pg_namespace where nspname = '"'"'contract_be30c85aac3347ffbf3bc45bc6ea12de'"'"';`（两库各一次）

**判定：对象在业务库。按授权「若在业务库 tubeflow：立即停下，只汇报，不删除」，此处停止。**

### 5.2 b)–e) 未执行

`b`（三项空检查）、`c`（确认无集成测试在跑）、`d`（`DROP SCHEMA … RESTRICT`）、`e`（复查）**全部未执行**。
本次未对任何数据库执行任何写操作；只有 `SELECT`。

备查（仅信息性，未据此删除）：业务库内该 schema 的对象计数
`pg_class=0`、`pg_proc=0`、`pg_type=0`（空）。

### 5.3 f) 其他遗留（只读列出，未删除，2026-11-06T06:26:17Z）

| 查询 | 结果 |
| --- | --- |
| 业务库 `contract\_%` | 1 行：`contract_be30c85aac3347ffbf3bc45bc6ea12de` |
| 业务库 `identity\_test\_%` / `narration\_authority\_%` / `voice\_race\_%` | 0 行 |
| 测试库 上述三类前缀 + `contract\_%` | 0 行 |
| 业务库集群 `datname like 'alembic\_url\_%'` | 0 行 |
| 测试库集群 `datname like 'alembic\_url\_%'` | 0 行 |
| 业务库非系统 schema（额外观察，未删） | `audit_272879e_20260829a`、`audit_272879e_exact_20260829a`、`public`、`staged_cleanup_gate`、`contract_be30c85aac3347ffbf3bc45bc6ea12de` |

### 5.4 环境旁证

- 部署机 `127.0.0.1:55432` 处于 LISTEN，那是 `tubeflow-contract-pg` 的端口映射（常驻），**不是**本次集成测试留下的隧道；测试用的 55437 隧道在集成跑完后已关闭。

## 6. 失效与续接

- 本次操作**未产生任何变更**；授权在该对象上**未消耗**（删除上限 1，实际 0）。
- 该 schema 仍留在业务库 `tubeflow` 中（空）。**如需删除，须由用户单独授权**——业务库属生产库，本任务授权明确排除。
- 造成泄漏的夹具路径已由 PR #16 修复（`postgres_database` 把迁移放进 `try`，失败也会 `DROP … CASCADE`），测试库复查为 0 行；本次两轮集成测试也未产生新泄漏。
- 本记录只读复核可追溯：任务 `Pending issues/2026-11-06/evidence/schema_cleanup/`。
