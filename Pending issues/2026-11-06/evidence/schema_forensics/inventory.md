三个 schema 的只读盘点（业务库 tubeflow，2026-11-06T07:07:51Z）

**未做任何改动**：没有 DROP、没有 VACUUM/ANALYZE、没有写操作。

## 1. 概况

| schema | 属主 | 表 | 索引 | 序列 | 视图 | 物化视图 | 函数 | 类型 | 总占用（表+索引+TOAST） |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `audit_272879e_20260829a` | tubeflow | 39 | 123 | 0 | 0 | 0 | 0 | 78 | **1176 kB** |
| `audit_272879e_exact_20260829a` | tubeflow | 39 | 123 | 0 | 0 | 0 | 0 | 78 | **1256 kB** |
| `staged_cleanup_gate` | tubeflow | 39 | 123 | 0 | 0 | 0 | 0 | 78 | **1216 kB** |

- 对象数按 `pg_class.relkind` 分组统计；「类型」78 = 39 张表各自的行类型与数组类型，**没有独立创建的类型**（函数数 0）。
- 总占用 = `sum(pg_total_relation_size(oid))` 只对 `relkind in ('r','m')` 求和，避免索引被重复计入（`pg_total_relation_size` 已包含索引与 TOAST）。
- 三者合计约 **3.6 MB**。

## 2. 行数

`reltuples` 大多为 **-1**（表示该表自创建以来从未被 `ANALYZE`/`VACUUM` 统计过，**不能**当行数用）。
因此另做了一次真实 `count(*)`（只读）：

| schema | 表数 | 真实行数合计 | 有数据的表 |
| --- | ---: | ---: | --- |
| `audit_272879e_20260829a` | 39 | **1** | `alembic_version` = 1 |
| `audit_272879e_exact_20260829a` | 39 | **3** | `accounts` = 1、`alembic_version` = 1、`staged_objects` = 1 |
| `staged_cleanup_gate` | 39 | **1** | `alembic_version` = 1 |

合计 **5 行**，其余 114 张表全空。

## 3. 迁移版本与内容线索

| schema | `alembic_version.version_num` | 备注 |
| --- | --- | --- |
| `audit_272879e_20260829a` | `0007` | 早期迁移快照 |
| `audit_272879e_exact_20260829a` | `0006` | 唯一有业务行的一个：`accounts` 里 1 行，`username = audit-owner`、`role = USER` |
| `staged_cleanup_gate` | `0006` | 早期迁移快照 |

三者都是「建了整套表结构、只跑了 0006/0007 迁移、几乎没有数据」的壳，与名字里的 `20260829`（2026-08-29）
以及 `pg_namespace.age(xmin)` 显示的时间段吻合。**用途来源未查证**，只列出事实。

## 4. 每张表的明细

见同目录 `tables_detail.txt`（117 行 psql 原始输出：schema / table / `reltuples` / `pg_size_pretty`）。
注意其中 `reltuples` 为 `-1` 的行表示「无统计信息」，不代表 0 行；真实行数见上表。
