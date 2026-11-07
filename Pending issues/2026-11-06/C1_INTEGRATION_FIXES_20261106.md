# C1：集成测试欠账修复，以及 A/B/C2/C3 汇报复核（2026-11-06）

依据：`NEXT_STEPS_IMPLEMENTATION_PLAN_20261106.md` 第 C 节（PR #18 分支），实施方提交 `ff179a7`、`dd8524e`。

## 1. 实施方汇报复核（对照仓库内证据）

| 项 | 结论 | 说明 |
| --- | --- | --- |
| A 端到端 | 属实，跑通 | 9/9 故事板、9/9 视频一次成功；9 条归档都带 `motion_metrics.status=ok`（§47 最后一项兑现）。暂停原因与证据一致：8 句单元撞上 `min(2×句数, 8)` 镜头上限。重剪与末次合并都没触发，§45 第 7 项仍未验证。 |
| B 隔帧修补 | 属实，结论成立 | B1 四条判据中两条不满足（只有 1/4 条高停顿样片降到 0.2 以下；6 条全部丢帧）。不提交修补方案。 |
| C2 测试 MinIO | 属实 | 独立容器 `tubeflow-contract-minio`，127.0.0.1:59000，线上 MinIO 未改动。 |
| C3 五条失败 | 属实，根因已用实验验证 | 4 条：`Database` 的 `connect_args` 覆盖了 URL 里的 `search_path`，写入落进 `public`（产品缺陷，只影响隔离）。1 条：测试期望的异常类型过时。 |

另注意到：A 的 held_share 均值 0.521，但组间差一个数量级（0.05 到 0.93），与此前「隔帧停顿主要取决于单次生成本身」的判断一致。

## 2. 本次修改

产品代码只改一处：

- `adapters/persistence/database.py`：新增 `postgres_connection_options(url)`，把 URL 里已有的 `options` 与超时**合并**后再传 `connect_args`。URL 不带 `options` 时，结果与原来逐字相同（单测覆盖）。

测试代码：

| 改动 | 修掉的失败 |
| --- | --- |
| `test_video_postgres.py`：期望改为 `VideoCapacityUnavailable` | C3 第 5 条 |
| `conftest.py` 新增 `migration_head()`（从 `migrations/` 读 head）；两处 `== "0039"` 改用它 | C1 两条过时期望 |
| 新增 `object_store_guard.py`：目标 MinIO 有线上桶 `tubeflow-private` 就判失败；所有建 MinIO 客户端的集成测试（6 个文件、15 处）在建桶前调用 | 防止测试连到线上 MinIO |
| 清理测试的 10 处 MinIO 凭据：原来读 `MINIO_ROOT_USER/PASSWORD`、缺省回退到开发默认口令，改为只读 `TUBEFLOW_TEST_MINIO_ACCESS_KEY/SECRET_KEY` | 计划外发现：机器上若设了线上 root 凭据，这 10 条会拿它去连 |
| 新增 `test_database_options_postgres.py`：真实连接上检查 `current_schema()` 是临时 schema、超时为 15s / 1min | C3 第 1–4 条的直接回归测试 |
| 单测：合并逻辑 5 条；MinIO 守卫 2 条；源码扫描 3 条（未调守卫、使用 root/默认凭据、把当前 head 写死成字面量。夹具显式钉住的版本不算，如 `test_staged_cleanup_migration.py` 的 0006） | — |

源码扫描在 main 上会报出：6 个未加守卫的文件、1 个用 root 凭据的文件、2 个写死 `0039` 的文件；在本分支上都为空。

## 3. 未验证（Claude 云端工作区没有 SQLAlchemy、pytest，也不能装包）

- 新增和修改的单元测试、集成测试都**没有运行过**；只做了 ruff（相对 main 无新增）、mypy（`database.py` 通过）和语法编译。
- 12 条缺 MinIO 的失败能否转绿，要在测试机配好测试 MinIO 后实跑。
- 部署机 `DATABASE_URL` 若带 `options=`，本修改会让它从「被静默丢弃」变为「生效」。需只读确认（见提示词）。

## 4. 测试库 `public` 里的泄漏数据

修复后测试不再写 `public`，这些行不再影响结果，可以留着。是否清理（`same-admin`、`system_initialization`、33 行 `staged_objects`；`mig-probe` 不动）需要用户另行授权。
