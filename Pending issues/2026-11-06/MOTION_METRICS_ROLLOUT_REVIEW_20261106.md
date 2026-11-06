# 观测指标上线报告的复核与后续处理（2026-11-06）

| 项 | 内容 |
| --- | --- |
| 对象 | 实施方报告「PR #15 合并 → §52 部署 → 部署机复算 → 遗留三件事」。合并提交 `bbd6f08`，证据与文档提交 `8d96e41`。 |
| 性质 | 只读复核，在 Claude 云端工作区读取仓库证据并重算，没有访问部署机，没有调用 Provider。 |
| 结论 | 合并、门禁、部署、复算均与证据一致；第 7 组第 1 镜的结论需要更正（第 2 节）。用户 2026-11-06 回复「按你推荐执行」，处理见第 4 节。 |

## 1. 核对结果

- `bbd6f08` 的代码树与 PR #15 head `da60ef6` 相同；GitHub 已标为 merged。
- 部署机复算（`evidence/motion_metrics/recompute/recompute.json`）与 A/B 结果逐项一致：
  - 整条：隔帧占比 0.49、运动窗口 45、held 窗口 22、HF 0.874、抖镜头 2；
  - 逐镜 HF 差为 0。
- 以下两点已解释清楚：
  - 适配器单测在测试机被跳过（无 ffmpeg），但它在 Claude 云端工作区通过，部署机又用真实归档视频完整跑通了同一段代码。
  - main 的集成通过数从 121 变为 122，是因为 main 已包含 PR #14 新增的那条集成用例。
- 我在 2026-11-06 的提问选项里估计镜像「约增大 70 MB」，实际增加 316 MB（+28.6%），**估计有误**。部署机有 954 GB NVMe，构建时间没有变化。

## 2. 更正：第 7 组第 1 镜

实施方的结论是：静态背景逐帧对齐，HF 高是主体把相位相关带偏造成的测量误差。

复核时只测画面两侧的背景，避开人物与纸箱：左侧门窗（x < 330）、右侧货架（x ≥ 830）。用两种方法分别测量：
- 相位相关；
- ORB 特征 + RANSAC。

脚本与输出：`evidence/motion_metrics/recompute/shot1_background.py`、`shot1_background.txt`，数据来自实施方导出的原分辨率帧。

- 相邻帧间的背景位移在 −3.4 到 +2.2 px 之间（左侧条带），方向不规则；右侧条带最多约 5.7 px。
- 背景条带上的相位相关响应为 0.84–1.0，说明位移估计可信。
- 对照：63→64 这一对，特征法测得约 0 px，两侧条带的相位相关结果也不超过 1 px。方法能区分「没动」与「在动」。

**正确的结论**：
- 没有几十像素的大突跳，这一点实施方说得对；
- 但整帧有 1–3 px 的不规则微颤，属于 484746c4 根因文档中的 C 类（静止机位微颤）；
- 所以 HF 1.578 的量级大致可信，不是测量误差。

已在 `docs/DEPLOYMENT.md` §43 与 `evidence/motion_metrics/README.md` 追加更正，原记录保留。

## 3. 遗留 schema

- 原因已核实：`tests/integration/conftest.py::postgres_database` 先建 schema，再执行 `alembic upgrade`，但后者在 `try/finally` 之外；迁移失败或中断时不会删除 schema。
  - 用原夹具复现：迁移失败后只执行了 `CREATE SCHEMA`，没有执行 `DROP`。
  - 修复后：同样失败也会执行 `DROP SCHEMA … CASCADE`。
- 证据中没有写明该 schema 所在的数据库。集成测试用的是 `tubeflowtest`（`evidence/jitter_ab/gates/SUMMARY.md`），业务库是 `tubeflow`。
- 其他会建临时 schema 或数据库的集成测试：
  - `test_identity_project_postgres`、`test_project_director_v4_migration`、`test_narration_postgres`：建好后的步骤都在清理保护内。
  - `test_alembic_url_precedence`：建临时数据库，`finally` 中删除。
  - `test_voice_catalog_concurrency`：迁移和运行时构造在 `try` 之外，有同样的泄漏可能。它需要配置 MinIO，而目前测试机没有配置，会在建 schema 之前就失败，所以本次未改，记为已知项。

## 4. 处理（用户 2026-11-06「按你推荐执行」）

| 事项 | 处理 |
| --- | --- |
| 第 7 组第 1 镜结论更正 | 本 PR：DEPLOYMENT §43、证据 README 追加更正，并附测量脚本与输出 |
| 修复夹具 | 本 PR：`conftest.py` 把迁移与引擎创建放进清理保护内；新增单元测试（需要 alembic，测试机上实际运行），模拟迁移失败，断言执行了 `DROP SCHEMA` |
| 删除遗留空 schema `contract_be30c85aac3347ffbf3bc45bc6ea12de` | 授权实施方在部署机执行。前提：确认它位于测试库 `tubeflowtest` 且库内无任何对象。用 `DROP SCHEMA … RESTRICT`，只要 schema 内有对象就报错，不会删除。若它位于业务库 `tubeflow`，停下汇报。授权范围只限这一个 schema |
| 上线后第一条带 `motion_metrics` 的视频 | 等下次正常生成时自然出现，再核对 |
