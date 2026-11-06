# PR #17 修订版合并前门禁（2026-11-06）

**判定：通过 —— 相对 main 无新增失败；新单测 8 passed / 0 skipped；集成失败集合与 main 逐条相同，守卫未误拦测试库。**

- PR #17 修订版 head `c2bfb67`，base `b91ddc1`，**1 个提交、11 个文件**。
- `git diff --stat b91ddc1 c2bfb67 -- backend/src frontend` → **空**（只动 `backend/tests/**`、`docs/**`、`Pending issues/**`）。
- `backend/pyproject.toml` 未改 ⇒ **未重装依赖**。
- 两侧工作树（**测试机**）：`wt-pr17-main` @ `b91ddc1`、`wt-pr17-branch` @ `c2bfb67`，均设 `PYTHONPATH=<该侧>/backend/src`。

## 1. 守卫的修订

第一版用「`public.projects` 有数据」识别业务库，而测试库 `tubeflowtest.public.projects` 有 1 行（`mig-probe`），会拦下全部集成测试。
修订版改为**库名白名单**：`select current_database()` 的返回值必须以 `test` 结尾（不区分大小写）才放行，否则 `pytest.fail`。`tubeflowtest` 以 `test` 结尾 ⇒ 放行。

## 2. 逐项对比（测试机）

| 检查 | main `b91ddc1` | 分支 `c2bfb67` | 结论 |
| --- | --- | --- | --- |
| `ruff check src tests` | Found 15 errors | Found 15 errors | **逐字节相同** |
| `mypy src` | 12 errors in 7 files（208 源） | 同 | **逐字节相同** |
| 单元（全量） | 2482 passed / 11 skipped | **2489 passed / 11 skipped** | +7 = 单测由 1 条变 8 条，两侧 0 失败 |
| `test_integration_fixture_cleanup.py` | 文件存在（1 条） | **8 passed, 0 skipped**（5.00 s） | 实跑通过，未跳过 |
| 集成 | 19 failed / 122 passed / 7 skipped | 19 / 122 / 7 | 失败 nodeid **逐条相同**，且与上一轮一致 |
| `npm run check` | — | — | **未跑**（前端未改） |

**守卫是否误拦（本次的核心判据）**：

| 检查 | main 日志 | 分支日志 |
| --- | ---: | ---: |
| `must name a database whose name ends with 'test'` 出现次数 | **0** | **0** |
| `database_guard` / `refuse_application_database` 相关失败 | **0** | **0** |

⇒ 守卫在测试库 `tubeflowtest` 上**没有**误拦，集成用例照常执行（19 条既有失败与守卫无关，两侧同一集合）。

## 3. 两库 `contract\_%`（部署机，07:39:26Z）

| 库 | 容器 / 角色 | 结果 |
| --- | --- | --- |
| `tubeflowtest`（测试库） | `tubeflow-contract-pg` / `postgres` | **0 行** |
| `tubeflow`（业务库） | `tubeflowai-test-postgres-1` / `tubeflow` | **0 行** |

旁证：业务库非系统 schema 仍为 `audit_272879e_20260829a`、`audit_272879e_exact_20260829a`、`public`、`staged_cleanup_gate` —— 与上一轮删除后一致，**未改动**。

## 4. 未运行 / 未覆盖

- `npm run check` **未跑**（前端未改）。
- 未验证「去掉守卫调用后相应测试会失败」——那需要改代码，属另一件事；PR 文档里那条结论来自作者侧的复现。
- 未构建镜像、未部署、未重启容器、未改 `.env`、未调用任何 Provider（本 PR 无产品代码、无迁移、无依赖变化）。
- 数据库只执行了 `SELECT`；`mig-probe` 行与业务库三个审计 schema 均未触碰。
