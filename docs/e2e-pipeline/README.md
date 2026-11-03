# TubeFlow AI - Test：视频项目端到端处理流程文档集

> 生成日期：2026-10-14
> 依据：当前代码仓库实际实现（后端 `backend/src/tubeflow_test/`，前端 `frontend/api-client.js`），辅以 `docs/planning/USER_DECISIONS.md` 的章节号交叉引用。
> 约束：只写代码中已确认存在的内容；推断内容以 **【推断】** 标注并给出依据；无法确认的写「未找到相关实现」。

## 文档清单与关联关系

| 文档 | 内容 | 与其他文档的关系 |
| --- | --- | --- |
| `README.md`（本文） | 范围、口径、文档地图 | 索引入口 |
| `01-flow.md` | **分步流程说明**：从创建项目到导出 ZIP 的 14 个环节，每步标注核心文件路径、类/函数名、关键行号 | 主文档；各环节的状态码、重试预算细节指向 `03`；各环节提示词原文指向 `04` |
| `02-overview.md` | **链路总览**：Mermaid 流程图 + 时序列表 + 每步输入/输出/状态流转/失败重试表 | `01` 的图形化压缩版；状态名与 `03` 对齐 |
| `03-states-and-errors.md` | **状态与异常处理**：各子系统状态机、失败分类、重试预算、恢复入口 | 被 `01`/`02` 引用；提示词相关失败码指向 `04` 对应环节 |
| `04-prompts.md` | **提示词清单**：每个环节实际使用的 Prompt 原文、占位符格式、上下文注入来源；无提示词环节标注「无」 | 独立成册；`01` 各环节仅引用其锚点 |

## 流程口径说明

- 当前代码新建项目一律为 `production_mode="AUTO"`（`identity_project/service.py:628`），即 `USER_DECISIONS.md` §15 的「一键自动生产」；既有 MANUAL 项目的分步手动入口仍在代码中存在，本文档以 AUTO 全链路为主线，并在每一步标注 MANUAL 差异。
- 「封面」阶段（第十阶段）按 §18 是**独立手动功能**，不纳入 AUTO 自动链路；本文档将其单列为支线环节（`01-flow.md` 第 13 节），其提示词仍收入 `04-prompts.md`。
- 「回调通知」口径：代码中**不存在**任何出站 webhook/回调（唯一的 `webHook` 出现在 grsai 故事板提交体中且固定为 `"-1"`，即显式关闭，`adapters/grsai_grid_sheet.py:83`）。系统对前端的状态通知全部通过 **GET 轮询 + `public_status`/`workflow_summary` 合同**完成，详见 `01-flow.md` 第 14 节与 `03-states-and-errors.md`。

## 代码结构速查

| 层 | 目录 | 职责 |
| --- | --- | --- |
| API | `backend/src/tubeflow_test/api/` | FastAPI 路由与运行时装配（`app.py` 为总装配） |
| 应用服务 | `backend/src/tubeflow_test/application/` | 配音、导演、故事板、批量、合成、导出、封面编排 |
| 领域 | `backend/src/tubeflow_test/domain/` | 分句、时间轴、分组、镜头、封面规则等纯逻辑 |
| 适配器 | `backend/src/tubeflow_test/adapters/` | CosyVoice / ElevenLabs / grsai / DeepSeek / H3 Engine / FFmpeg / MinIO / SQLAlchemy |
| 工作流 | `backend/src/tubeflow_test/workflows/` | Temporal workflow/activity 定义与 Worker 装配（`worker.py`） |
| AUTO 编排 | `backend/src/tubeflow_test/auto_pipeline/` | 一键生产编排器（`service.py` 决策树） |
| 视频生成 | `backend/src/tubeflow_test/video_generation/` | H3 意图、编排器、Engine 客户端、outbox |
| 提示词编译 | `backend/src/tubeflow_test/video_prompt/` | H3 视频提示词编译器与版本化模板 |
| 拆句 | `backend/src/tubeflow_test/sentence_split/` | 超长句二级拆分 agent |
