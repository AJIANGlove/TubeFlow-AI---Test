# 业务库遗留 schema：取证、删除与三 schema 盘点（2026-11-06）

术语：**部署机** = 192.168.110.200；**测试机** = 实施方机器；**业务库** = `tubeflow`（容器 `tubeflowai-test-postgres-1`，角色 `tubeflow`）；**测试库** = `tubeflowtest`（容器 `tubeflow-contract-pg`，角色 `postgres`）。

本次**不合并 PR #17**（用户 2026-11-06 裁定：守卫以「`public.projects` 有数据」判业务库，会误拦测试库；作者将改为「库名须以 test 结尾」后另行通知）。因此**未跑任何门禁、未构建、未部署、未重启容器、未改 `.env`、未调用任何 Provider**。

## 1. 结论

- **删除完成**：业务库中的 `contract_be30c85aac3347ffbf3bc45bc6ea12de` 已用 `DROP SCHEMA … RESTRICT` 删除（07:07:35Z，`DROP SCHEMA`，rc=0）。删除前四项检查全部满足（存在、`pg_class`/`pg_proc`/`pg_type` 均 0、属主 `tubeflow`）。
- **复查**：业务库与测试库的 `contract\_%` 均为 **0 行**；`/health/live` = **200**。
- **取证的关键发现**：该 schema 的 `age(xmin)` = **51,673,862**，**比 `projects` 里任何一行都老**（最老的一行 2026-09-04，age 48,580,910）。它**不是** 2026-11-05 那次运行留下的，而是更早（早于 2026-09-04）的对象；容器日志（只覆盖 2026-09-23 之后）里没有任何痕迹。
- **溯源**：测试机上**没有**任何记录把 `TUBEFLOW_TEST_POSTGRES_URL` 指向业务库（仓库唯一记录的取值指向 `tubeflowtest`；PSReadLine 历史 0 命中；无 `.bash_history`；工作树无 `.env`）。
- **另三个 schema 未改动**：都是 2026-08 的迁移快照（`alembic_version` = 0006/0007），合计只有 5 行数据。

## 2. 与 PR #17 的关系（不合并的原因）

PR #17 新增的 `tests/integration/database_guard.py` 以「目标库存在 `public.projects` 且有行」判定业务库。本环境里**测试库** `tubeflowtest` 的 `public` 有 70 张业务表（`alembic_version = 0047`），且 `public.projects` 有 **1 行**：

| 列 | 值 |
| --- | --- |
| 行数 | **1** |
| `id` | `00000000-0000-0000-0000-0000000000f1` |
| `title` | `mig-probe` |
| `created_at` | `2026-11-05 09:56:42.046067+00` |

该行是迁移探针留下的，不是真实业务数据，但守卫只看「存在且有行」，合并后会把**全部**集成测试判失败。**该行未动**（用户明示）。

## 3. 目录

| 文件 | 内容 |
| --- | --- |
| `forensics.txt` | 删除前只读取证 a–d 的原始输出（含时间推断与局限） |
| `drop.md` | 删除各步：记录 → 复查为空 → `DROP … RESTRICT` → 复查 |
| `inventory.md` | 三个 schema 的属主、对象数、体积、真实行数 |
| `tables_detail.txt` | 三 schema 每张表的 `reltuples` 与占用（原始 psql 输出） |
| `operations-record.md` | 部署机 `runtime/operations/schema-forensics-20261106/README.md` 的镜像（AGENT_AUTHORIZATION §5 六项） |
| `queries.txt` | 本次用到的全部 SQL |

## 4. 未完成 / 限制

- **时间只能给上界**：`xmin` 无法把创建时间夹到区间内（该 schema 比所有项目都老）。日志最早只到 2026-09-23，覆盖不到。
- 三个 schema 的**真实行数**用 `count(*)` 得到（`reltuples` 多为 `-1`，表示从未 `ANALYZE`，不能当行数用）。
- 未查证 `audit_*` 与 `staged_cleanup_gate` 的**用途来源**；只从 `alembic_version` 与命名推断为 2026-08-29 前后的迁移/审计快照。
- 删除只针对**业务库**的那一个 schema；测试库未做任何写操作。
