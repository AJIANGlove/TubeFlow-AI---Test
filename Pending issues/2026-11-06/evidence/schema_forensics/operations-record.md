# 运维操作记录：业务库遗留 schema 取证与删除（schema-forensics-20261106）

## 1. 操作身份与状态

- 操作 ID：`schema-forensics-20261106`
- 任务目标：在**业务库 `tubeflow`** 中删除集成测试夹具遗留的**空** schema，并先做只读取证
- 精确对象：PostgreSQL schema `contract_be30c85aac3347ffbf3bc45bc6ea12de`（仅此一个）
- 状态：**已完成**（删除成功，授权额度已用尽）

## 2. 用户授权证据

- 2026-11-06 用户指令：「按授权删除业务库中的那一个空 schema …… 授权：用户 2026-11-06『全部按建议执行』，见复核文档第 3 节；范围仅限这一个 schema」。
- 复核文档：`Pending issues/2026-11-06/SCHEMA_CLEANUP_REVIEW_20261106.md` 第 3 节（该文件在 PR #17 分支上，本地未合并）。
- 硬限制：删除前再次确认其中无任何对象；用 `DROP SCHEMA … RESTRICT`，有对象即报错、不删除；**不得改用 CASCADE**；报错即停下汇报。

## 3. 允许范围

- 动作：`DROP SCHEMA "contract_be30c85aac3347ffbf3bc45bc6ea12de" RESTRICT;`，以角色 `tubeflow`、库 `tubeflow`
- 前置：`pg_class` / `pg_proc` / `pg_type` 三项对象计数均为 0；属主为 `tubeflow`
- 禁止：删除其它 schema 或对象；`CASCADE`；换超级用户；重启容器；改 `.env`；调用任何 Provider

## 4. 上限与消耗

| 项 | 值 |
| --- | --- |
| 删除上限 | 1 个 schema |
| 实际删除 | **1**（`contract_be30c85aac3347ffbf3bc45bc6ea12de`） |
| 未决尝试 | 无 |
| 生成额度 | **0**（本操作不涉及任何 Provider 调用） |

## 5. 实际执行证据

### 5.1 删除前只读取证（详见仓库 `Pending issues/2026-11-06/evidence/schema_forensics/`）

- a) `pg_namespace` 五行（07:03:46Z）：目标 schema `oid=60061`、属主 **`tubeflow`**、`nspacl` 空、`xmin=30623`、`age=51673862`；`public` oid 2200 / 属主 `pg_database_owner`；`staged_cleanup_gate` oid 75274 age 51673038；`audit_272879e_20260829a` oid 289394 age 51661842；`audit_272879e_exact_20260829a` oid 291804 age 51661473。
- b) 时间推断：`projects` 62 行，`age(xmin)` 范围 **31487 … 48580910**（对应 2026-11-05 09:03Z … 2026-09-04 16:00Z）。目标 schema 的 age **51673862 大于全部项目**，即**早于最早的项目行**；用相邻项目无法夹出区间，只能给出上界。局限：`xmin` 32 位可回卷；并发下 xid 顺序与提交时间不严格单调；`projects.xmin` 是最后一次修改而非创建；该行若被 `ALTER SCHEMA` 改过属主，`xmin` 反映的是那次修改。
- c) 日志：容器创建 **2026-09-23T06:40:04Z**，日志覆盖 **2026-09-23T06:43:19Z … 2026-09-29T18:36:06Z**（3779 行），首行即 `PostgreSQL Database directory appears to contain a database; Skipping initialization`（数据目录是拷贝来的）。`grep -iE "contract_be30|permission denied|must be owner|extension"` **0 命中**。
- d) 测试机溯源：仓库内唯一记录的 `TUBEFLOW_TEST_POSTGRES_URL` 取值指向 `tubeflowtest`（`evidence/jitter_ab/gates/SUMMARY.md`）；仓库 `.env` 无该键；各 `wt-*` 工作树无 `.env`；无 `~/.bash_history`；PSReadLine 历史（3321 字节）中 `TUBEFLOW_TEST_POSTGRES_URL|tubeflow@|psql .*-d tubeflow` **0 命中**。→ **没有留下把集成套件指向业务库的记录**。

### 5.2 删除前再次确认为空（07:07:31Z）

`exists=1`、`pg_class=0`、`pg_proc=0`、`pg_type=0`、`owner=tubeflow` → 允许删除。

### 5.3 执行（07:07:35Z）

```
$ psql -U tubeflow -d tubeflow -c 'DROP SCHEMA "contract_be30c85aac3347ffbf3bc45bc6ea12de" RESTRICT;'
DROP SCHEMA
rc=0
```

未使用 `CASCADE`、未换超级用户、未加 `IF EXISTS`。

### 5.4 删除后复查（07:07:40Z）

| 项 | 结果 |
| --- | --- |
| 业务库该 schema | **0** |
| 业务库 `contract\_%` | **0 行** |
| 测试库 `contract\_%` | **0 行** |
| 业务库剩余非系统 schema | `audit_272879e_20260829a`、`audit_272879e_exact_20260829a`、`public`、`staged_cleanup_gate` |
| `/health/live` | **200** |

## 6. 失效与续接

- 授权额度：上限 1、**实际 1**，**已用尽**；本操作到此终止，不再对任何 schema 执行写操作。
- 另三个 schema（`audit_272879e_20260829a`、`audit_272879e_exact_20260829a`、`staged_cleanup_gate`）**未改动**，只读盘点见仓库 `evidence/schema_forensics/inventory.md`；是否清理需用户另行决定。
