# 集成测试 5 条失败排查（C3，2026-11-06，测试机）

环境：测试机跑 pytest（venv `tubeflow-test`，Python 3.12.14），经 SSH 隧道 `127.0.0.1:55441 → 部署机 127.0.0.1:55432` 连测试库 `tubeflowtest`（`alembic_version=0047`）。**未设** `TUBEFLOW_TEST_MINIO_*`（C1 未合并，不跑完整集成）。全部结论来自真实运行，日志见本目录。

## 1. 结论表

| # | 用例 | 现象 | 原始异常（截取） | 根因 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 1 | `test_identity_project_postgres::test_real_postgres_schema_round_trip_and_atomic_initialization` | 两个并发初始化都返回 409；期望一 201 一 409 | `AssertionError: assert [(409,'admin_already_initialized'),(409,...)] == [(201,None),(409,...)]` | `Database.__init__` 用 `connect_args={"options": "-c lock_timeout… -c statement_timeout…"}` **覆盖**了 URL 里的 `options=-csearch_path=<临时 schema>`，运行期连接实际落在 `public`；`public` 里早有上次泄漏留下的 `accounts.same-admin` 与 `system_initialization` 行 → 两个线程都判定「已初始化」并撞 `uq_accounts_username` | 产品缺陷（适配器层，仅影响隔离） |
| 2 | `test_staged_upload_protocol::test_postgres_advisory_claim_lock_prevents_same_generation_phantoms` | 32 路并发认领期望代号 2，实得 1 | `assert 1 == 2`（`test_staged_upload_protocol.py:143`） | 同上：写入走 `Database` → 落 `public.staged_objects`，而该键在 `public` 已有上一轮的行（33 行遗留），认领返回既有 generation 1 | 同上（同一根因） |
| 3 | `test_staged_upload_protocol::test_fixed_catalog_writer_can_resume_matching_unattached_stage` | 断言行不存在 | `assert None is not None`（`:187`） | 同上：`Database` 写到 `public`，断言用 schema 作用域的 `postgres_database.sessions` 读 → 读不到 | 同上 |
| 4 | `test_staged_upload_protocol::test_identity_claim_handle_blocks_aba_and_refreshes_reopened_generation` | 断言 finalized 行不存在 | `assert (None is not None)`（`:217`） | 同上 | 同上 |
| 5 | `test_video_postgres::test_postgres_round_trip_revision_cas_and_atomic_capacity` | 容量耗尽时期望 `VideoRuleViolation`，实际抛 `VideoCapacityUnavailable` | `tubeflow_test.video_generation.domain.VideoCapacityUnavailable: no compatible H3 node has capacity` | 产品代码按设计抛专用异常 `VideoCapacityUnavailable(DomainError)`（`orchestration.py:642,789` 专门捕获它），而测试写的是 `pytest.raises(VideoRuleViolation, match="capacity")`；两者**不是同一继承链**（`VideoRuleViolation(ValueError)` / `VideoCapacityUnavailable(DomainError)`）。节点夹具五项与 `workflow` 快照完全匹配，容量判定本身正确 | 期望过时 |

## 2. 共同根因的证据（第 1–4 条）

**代码事实** `backend/src/tubeflow_test/adapters/persistence/database.py:19-33`：PostgreSQL 分支固定传 `connect_args={"options": "-c lock_timeout=15s -c statement_timeout=60s"}`。SQLAlchemy 里显式 `connect_args` 优先于 URL query，因此 URL 的 `options=-csearch_path=…` 被丢弃。

**实测**（`experimental-patches.diff` 为实验补丁，已还原）：

| 场景 | `show search_path` | `current_schema()` |
| --- | --- | --- |
| `create_engine(url?options=-csearch_path=probe_x)` | `probe_x` | schema（存在时） |
| `Database(url?options=-csearch_path=probe_x)` | `"$user", public` | `public` |

**验证实验**：在 worktree 里只把 `Database` 改成「把 URL 里已有的 options 与超时合并」，同一 4 条用例 → **4 passed**（日志 `experiment-database-options.log`；调试输出显示 `setup_complete=False` → 一线程 `insert OK`、另一线程 `UniqueViolation uq_accounts_username`，与期望一 201 一 409 完全一致）。补丁已 `git checkout` 还原，worktree `git status` 干净。

**为什么以前没暴露**：`Database` 只在走应用服务/仓储的路径上用到；`postgres_database` 夹具自己用 `create_engine(schema_url)`（不走 `Database`），所以「建表在临时 schema、断言也在临时 schema」看不出问题，只有经过 `Database` 的写入才会漏到 `public`。

## 3. 第 5 条的验证

把测试期望改为 `VideoCapacityUnavailable` 后单跑 → **1 passed**（`experiment-video-capacity-type.log`）。说明夹具、兼容判定、CAS、释放节点等其余断言都成立，只是异常类型写错。

## 4. 测试库 `tubeflowtest.public` 的数据来源

只读查询（2026-11-06 09:0xZ）：

| 表 | 行数 | 内容/时间 |
| --- | ---: | --- |
| `public.accounts` | 2 | `mig-probe`（ADMIN）、`same-admin`（admin） |
| `public.system_initialization` | 1 | `completed_at = 2026-11-05T13:51:25Z` |
| `public.projects` | 1 | `mig-probe`，`created_at = 2026-11-05T09:56:42Z` |
| `public.channels` | 1 | — |
| `public.staged_objects` | 33 | 键形如 `identity-claim-*`、`concurrent-logical-*`、`voice-catalog/v1/resume-*`，`generation` 1–2 |
| 其他业务表 | 0 | 70 张表，`alembic_version=0047` |

结论（两批来源，都能对上）：

1. **手工迁移探针**：`public.projects.mig-probe`（`…0000f1`）+ 迁移把它建在 `public`，时间是 2026-11-05 09:56Z。仓库里 `grep mig-probe` 在 tests/src 下**零命中**，不是测试写的。
2. **测试泄漏**：`public.accounts.same-admin`、`public.system_initialization`（2026-11-05 13:51Z）、`public.staged_objects` 33 行，全部是第 1–4 条用例经 `Database` 写到 `public` 的产物——**就是上述 connect_args 覆盖造成的串扰**。

即：`public` 那套表不是空库自带的，而是「迁移探针 + 测试泄漏」叠出来的；测试泄漏同时是 4 条失败的直接原因。

## 5. 未做的事

- 未清理 `public` 的任何数据（用户要求只读；清理需新授权）。
- 未改测试库 schema、未动 `mig-probe` 行。
- 未实现修复（C1 由 Claude 出 PR；本目录只提供根因与验证）。

## 6. 建议给 C1 的两个最小修复（供 Claude 参考，本次未实施）

1. `Database.__init__`：把 URL query 的 `options` 与超时 `options` **合并**再传给 `connect_args`（现有 URL 无 options 时行为不变），并在集成守卫里加一条「schema 隔离必须生效」的断言。
2. `test_video_postgres.py:146`：期望改为 `VideoCapacityUnavailable`（或让仓储抛 `VideoRuleViolation`——但产品侧有两处 `except VideoCapacityUnavailable`，改产品会破坏语义，故建议改测试）。

## 证据

- `run1.log`：5 条原始失败（`-vv --tb=long`）
- `experiment-database-options.log`：合并 options 后 4 条转通过
- `experiment-video-capacity-type.log`：改异常类型后第 5 条通过
- `experimental-patches.diff`：三次临时调试/验证补丁（**均未提交**）
