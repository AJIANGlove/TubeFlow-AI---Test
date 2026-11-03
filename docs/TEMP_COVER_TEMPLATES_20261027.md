# 封面页频道模板 V2（多模板 + 手动保存）2026-10-27

> 状态：代码与测试完成，**未构建、未上线**（上传源码到部署机时审批弹窗超时，见文末）。
> 取代 `docs/TEMP_COVER_TEMPLATES_20261021.md` 的单封面 + A/B/C 三框版本。

## 1. 已确认的行为

| 项 | 结论 |
|---|---|
| 模板定义 | 一个模板 = 一张封面 + 一段提示词 + 名称 + 顺序；频道级、所有项目共用 |
| 增删 | 整体增删（图 + 文一起）；可建空模板；数量不限；至少保留一个（UI 限制） |
| 名称 | 自定义，默认自动编号「模板 N」（取已有编号最大值 +1，删除后不重排）；上限 30 字；允许重名；清空回退自动编号 |
| 排序 | 默认按创建时间（position 递增）；桌面鼠标拖动，拖完立即落库；新增排末尾 |
| 展示 | 纵向卡片平铺；每个模板一张卡片（缩略图 + 名称 + 文本 + 按钮） |
| 保存 | 每个模板行内一颗【保存】，只提交名称 + 文本；图片上传即时生效 |
| 未保存 | 切阶段/切项目/删除/复制前拦截；三选一：保存并继续 / 放弃修改 / 取消 |
| 关闭页面 | 只能弹浏览器原生提示（浏览器不允许自定义按钮）；未保存内容会丢 |
| 复制文本 | 有未保存修改时先保存，保存成功才复制已保存内容；失败则中止并提示 |
| 复制图片 | 复制原图；`execCommand` 选中图片优先，失败打开原图并提示右键复制 |
| 删除 | 二次确认，显示名称与「封面图一并删除，不可恢复」；仅剩一个时按钮禁用 |
| 生成关系 | 完全不参与封面生成/解析，仅作复制库；无版本号，覆盖即替换 |
| 权限 | 频道所有者；非本人一律 404 |

## 2. 数据

- 新表 `channel_cover_template_items`（迁移 `0044`）：
  `id / channel_id / name / position / prompt / 原图 7 列 / 压缩图 7 列 / last_save_key / created_at / updated_at`。
- 旧表 `channel_cover_templates` **冻结保留**（回滚用），频道删除时一并删除。
- 迁移 0044 把旧表每行复制为一条「模板 1」，保留 `prompt_a` 与图片字节；`prompt_b/prompt_c` 丢弃（线上实测两行 B/C 均为空，零损失）。
- 频道删除摘要 `cover_templates` 改为统计新表条数。

## 3. 接口（`/identity/channels/{id}/cover-templates`）

- `GET /` → `{channel_id, items:[...], updated_at}`
- `POST /items` body `{name?}` → 201 单项
- `PATCH /items/{item_id}` body `{name,prompt}` + **`Idempotency-Key` 必填** → 单项；同键重试不覆盖
- `DELETE /items/{item_id}` → `{item_id, deleted:true}`
- `PUT /items/order` body `{item_ids:[...]}` → 全量集合；集合不一致 409
- `POST /items/{item_id}/cover` multipart → 单项（图片即时生效）
- `HEAD|GET /items/{item_id}/cover/media/{original|preview}` → ETag + Range

## 4. 前端

- `frontend/cover-templates.js`：卡片标记、模型、名称/提示词码点裁剪、排序移动、保存状态。
- `app.js`：`coverTemplateBlockMarkup()` 挂载；`bindCoverTemplateEvents()` 绑定保存/复制/上传/删除/新增/拖拽；
  `ensureCoverTemplateEditsSaved()` 接入 `ensureCoverEditsSaved()`（阶段切换与站内导航）；`beforeunload` 原生提示。
- 保存失败自动重试 2 次（1s、2s），仍失败保留草稿并显示「保存失败，点击重试」；每次保存生成一个幂等键。
- 样式：`styles.css` 的 `/* 临时功能（2026-10-27）… */` 段。

## 5. 验证（已完成）

- 后端单元：`2095 passed, 8 skipped`（新增模板合同测试 13 项）。
- 后端集成（真实 Postgres）：`7 passed`（创建/保存/幂等/上传/媒体/排序/删除/隔离 + 频道删除级联）。
- 迁移：`0043 → 0044` 实测把旧行复制为「模板 1」，图片字节一致；`downgrade 0044` 后新表消失、旧行仍在。
- 前端：`npm run check` 全绿，253 项通过（新增/改写模板单测 15 项）。
- 视觉：真实样式渲染两卡片（有图/空模板）静态截图检查通过。

## 6. 删除此功能（清单）

1. 删后端包 `backend/src/tubeflow_test/cover_templates/`。
2. 删迁移 `0044_cover_template_items.py`；线上已升级则先 `alembic downgrade 0043`（只删新表，旧表保留）。
3. `adapters/persistence/schema.py`：删 `_cover_template_models` 的 import 与 del 项（若已无其他表）。
4. `api/app.py`：删 3 个 import 与 `create_cover_templates_router(...)` 注册块。
5. `adapters/persistence/channel_deletion.py`：删两张模板表的 import、计数与删除语句，以及 `cover_templates=` 参数。
6. `identity_project/ports.py`：删 `cover_templates` 字段；`identity_project/router.py`：删对应响应键。
7. 删前端 `frontend/cover-templates.js` 与 `frontend/cover-templates.test.js`。
8. `app.js`：删 import、`state` 的 7 个 `coverTemplate*` 字段与重置行、`coverTemplateBlockMarkup()` 调用、
   `// ---------------- 临时功能：频道级多模板` 整块、`bindCoverTemplateEvents()` 调用、`ensureCoverEditsSaved` 里的守卫、
   `beforeunload` 监听；保留「更多设置」折叠容器（永久改造）。
9. `frontend/api-client.js`：删 8 个模板方法。
10. `styles.css`：删 `2026-10-27` 模板段。
11. 测试：删 `tests/unit/test_cover_templates_api.py`、`tests/integration/test_cover_templates_postgres.py`；
    删除 `test_channel_deletion*` 中模板种子与断言。
12. 旧表 `channel_cover_templates` 可单独降级清理（`0041` downgrade 不可用，因为链上还有 0042/0043；需手工 DROP）。

## 7. 构建与上线

- **构建已完成**：最终镜像 `sha256:f6e7703e3912…`（修正版，含折叠容器函数修复）；首版 `3fa6ee93…` 因缺少 5 个辅助函数导致第 10 步 ReferenceError，已弃用。完整构建耗时约 16m46s，修正版利用缓存重建。
- 镜像校验：`app.js` / `cover-templates.js` / `api-client.js` md5 与本地一致；`alembic heads = 0044`；模块可导入；ffmpeg 7.1.5 正常。
- 构建网络：部署机访问 deb.debian.org / pypi.org 不稳定（IPv6 不可达 + IPv4 超时），前两次失败；
  最终在 `backend/Dockerfile` 中改用阿里云 apt 源与清华 PyPI 源，并强制 IPv4、加重试。该改动只影响构建来源。
- **已上线（2026-10-27 11:28，11:44 修正）**：`pg_dump` 备份 → `migrate 0044`（旧表 2 行迁成 2 条「模板 1」）→
  compose 切 `f6e7703e…` → web/worker healthy；`health 200`，`app.js` 8716 行、`readCoverMoreSetting` 存在，模板接口 401。
  回滚镜像 `958ec52fc5dd…` + `alembic downgrade 0043`。
- 记录：`runtime/operations/cover-templates-v2-20261027/README.md`。
- 上线后人工验证：卡片新增/删除、拖拽排序、保存/放弃/取消三种分支、复制文本先保存、复制图片粘到微信/Word、关闭页面的原生提示。
