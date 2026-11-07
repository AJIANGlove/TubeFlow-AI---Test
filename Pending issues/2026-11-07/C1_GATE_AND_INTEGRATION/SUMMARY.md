# C1 门禁 + 带测试 MinIO 全量集成（2026-11-07，测试机）

**结论：门禁全绿；集成从 main 的 20 failed 降到分支 1 failed，但那 1 条仍在，按用户规则不合并，等 Claude 复核。**

## 0. 前置只读

| 检查 | 结果 |
| --- | --- |
| 部署机 `.env` 是否含 `options=` | **0 行（无）**；`TUBEFLOW_DATABASE_URL` 1 行。（只报有/无，未读地址与口令） |
| 分支 | `claude/c1-integration-fixes-20261106` = `2cd03cf`，基于 `dd8524e`，`git diff --stat` **16 个文件**，与说明一致 |
| 代码审阅 | `postgres_connection_options()` 把 URL 的 `options` 前置再拼超时；`object_store_guard` 以「存在 `tubeflow-private` 即失败」判定；均与文档一致 |

## 1. 门禁（main `dd8524e` vs 分支 `2cd03cf`，测试机）

| 检查 | main | 分支 | 判定 |
| --- | --- | --- | --- |
| 单元全量 | **2489 passed / 11 skipped**（118 s） | **2499 passed / 11 skipped**（108 s） | 无失败；+10 = 新增用例 |
| `test_database_connection_options.py` | 不存在 | **5 passed** | ✅ |
| `test_integration_fixture_cleanup.py` | 8 条（既有） | 13 passed（**新增 5**） | ✅ 新增 5 条实际运行并通过 |
| 新增 10 条合计 | — | **10 passed**（连同整文件 18 passed） | ✅ 无 skip / 无 xfail |
| ruff（`check .`，同命令同机） | 18 errors | 18 errors | 逐条 diff **完全相同**，无新增 |
| mypy（`src`） | 12 errors / 7 files / 208 源 | 12 errors / 7 files / 208 源 | 逐行相同 |
| npm | **未跑**（前端文件零改动） | 同 | — |

说明：历史门禁记的「ruff 15」是旧口径（历史日志里报 `src\...` 的 E501）；本次两侧都用同一命令 `ruff check .`（18 条，含 tests/migrations 既有项），A/B 逐条 diff 为空才是有效判据。已知偶发用例（`test_sql_concurrent_failover_supersede_creates_exactly_one_successor`）两侧均未失败，未触发 3 次复跑。

## 2. 集成（两侧各一次完整跑，同一 MinIO / 同一 DB）

- 隧道：`55441 → 部署机 127.0.0.1:55432`（`tubeflowtest`，`alembic 0047`）；`59100 → 部署机 127.0.0.1:59000`。
- 开跑前确认 59100 = `tubeflow-contract-minio`：`list_buckets()` 为空、无 `tubeflow-private`。
- 环境：`TUBEFLOW_TEST_POSTGRES_URL` + `TUBEFLOW_TEST_MINIO_ENDPOINT=127.0.0.1:59100` + `TUBEFLOW_TEST_MINIO_ACCESS_KEY/SECRET_KEY`（取自 `runtime/operations/contract-minio-20261106/`）；**未设** `TUBEFLOW_TEST_TEMPORAL_TARGET`，与基线一致。
- 两侧日志均**未出现** `holds the application bucket`（守卫未误报，也没连错 MinIO）。

| 侧 | 结果 | 用时 |
| --- | --- | --- |
| main `dd8524e` | **20 failed / 125 passed / 3 skipped** | 275.6 s |
| 分支 `2cd03cf` | **1 failed / 145 passed / 3 skipped** | 279.7 s |

> 基线 19F/122P/7S 是**未配 MinIO** 时的数字。配上测试 MinIO 后，原来 skip/KeyError 的用例真的会跑，所以 main 侧变成 20F/125P/3S —— 两侧必须同样配 MinIO 才可比。

### 2.1 main 侧 20 条失败（逐条）

| # | 用例 | 原始异常/原因 | 分支侧 |
| ---: | --- | --- | --- |
| 1 | `test_cloned_voices_postgres::test_elevenlabs_project_and_snapshot_facts_round_trip` | `assert '0047' == '0039'`（写死迁移 head） | ✅ 通过 |
| 2 | `test_elevenlabs_pool_postgres::test_elevenlabs_pool_schema_matches_models_and_downgrade_drops_tables` | 同上 | ✅ 通过 |
| 3 | `test_identity_project_postgres::test_real_postgres_schema_round_trip_and_atomic_initialization` | 两线程都 409：`Database` 把 `search_path` 覆盖掉，`same-admin` 已在 `public` | ✅ 通过 |
| 4 | `test_staged_upload_protocol::test_postgres_advisory_claim_lock_prevents_same_generation_phantoms` | `assert 1 == 2`（写入落 `public`） | ✅ 通过 |
| 5 | `test_staged_upload_protocol::test_fixed_catalog_writer_can_resume_matching_unattached_stage` | `assert None is not None` | ✅ 通过 |
| 6 | `test_staged_upload_protocol::test_identity_claim_handle_blocks_aba_and_refreshes_reopened_generation` | 同上 | ✅ 通过 |
| 7 | `test_video_postgres::test_postgres_round_trip_revision_cas_and_atomic_capacity` | `VideoCapacityUnavailable` vs 期望 `VideoRuleViolation` | ✅ 通过 |
| 8 | `test_media_content_postgres::test_real_minio_authenticated_http_serves_current_and_historical_video_assets` | MinIO 默认/root 凭据 → 鉴权失败 | ✅ 通过 |
| 9–19 | `test_project_purge_shared_assets.py` 11 条 | 同上：`AccessDenied`/`SignatureDoesNotMatch`，来自缺省回退的开发口令 | ✅ 全部通过 |
| 20 | `test_narration_postgres::test_empty_postgres_authority_round_trip_after_migration` | **见 2.2** | ❌ 仍失败 |

### 2.2 分支侧唯一剩余失败（原始异常）

```
tests\integration\test_narration_postgres.py:652: in test_empty_postgres_authority_round_trip_after_migration
    app = create_app(settings)
src\tubeflow_test\api\app.py:367: in create_app
    video_prompt_compiler = build_video_prompt_compiler(
E   TypeError: test_empty_postgres_authority_round_trip_after_migration.<locals>.<lambda>() takes 1 positional argument but 2 were given
```

- **main 侧同一条用例、同一个异常、同一行**（`main` 日志第 31–37 行），所以**不是 C1 引入的**。
- 根因：`build_video_prompt_compiler(settings, diagnostics=None)`（`video_prompt/compiler.py:270`）已带第二个参数，产品侧 `app.py:367` 按位置传两个；测试里 `monkeypatch.setattr(..., lambda _settings: compiler)`（`test_narration_postgres.py:651`）还是 1 个参数 → 陈旧夹具。
- 这条以前被 `KeyError: 'TUBEFLOW_TEST_MINIO_ENDPOINT'` 挡在前面，配了 MinIO 才走到 `create_app`，把既有夹具缺陷暴露出来。

## 3. 跑完的只读复核

| 检查 | 结果 |
| --- | --- |
| 测试 MinIO 桶列表 | **有 2 个遗留**：`narration-check-e668d346…`、`narration-check-e6c77275…` —— 由上面那条失败用例在建桶之后、清理之前中止造成；**未删除**（本步只做只读查询） |
| 测试库 `contract\_%` schema | **0**（无残留） |
| `public` 内容 | 未主动修改；但 `public.staged_objects` 从 33 行变 **39 行（+6）** —— 这是 **main 侧**跑集成时旧 bug 继续泄漏所致（分支侧写入已落临时 schema）；按「不改数据库内容」未清理 |

## 4. 合并决定

**未合并。** 依据用户规则「有任何剩余失败，逐条列出原始异常，不合并，等 Claude 复核」。

建议给 Claude 的最小修复（未实施）：把 `test_narration_postgres.py:651` 的 `lambda _settings: compiler` 改成接受两个位置参数（`lambda *_args: compiler`），然后只重跑该用例 + 分支全量集成；绿了就合并。

## 5. 证据

`gate-unit-main.log`、`gate-unit-branch.log`、`gate-ruff-main.txt`、`gate-ruff-branch.txt`（逐条 diff 为空）、`integration-main.log`、`integration-branch.log`、`main-failures.txt`（20 条）、`branch-failures.txt`（1 条）。

机器：门禁与集成均在**测试机**执行；部署机只做了 `.env` 只读检查与两条 SSH 隧道，未重启容器、未改 `.env`、未调用 Provider。
