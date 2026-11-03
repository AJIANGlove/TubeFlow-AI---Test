# 封面页频道模板（临时功能）2026-10-21

> **已被 2026-10-27 多模板版取代**：现行结构、接口与删除清单见
> `docs/TEMP_COVER_TEMPLATES_20261027.md`。本文仅保留 V1 历史。

> 状态：已实现（本地代码 + 测试），未部署。
> 定位：第 10 步封面页顶部的「封面模板 / 提示词模板」，频道级、所有项目共用，**不参与**参考图解析、提示词生成或出图。用户明确该功能随时可能删除，因此独立成包、独立成表、独立前端模块。

## 1. 已确认的行为边界

| 项 | 结论 |
|---|---|
| 位置 | 4 张状态卡与「更多设置」折叠容器之间；仅项目详情第 10 步 |
| 封面模板 | 频道唯一 1 张；上传即压缩并保留原图；重新上传直接覆盖，无历史、无删除入口 |
| 展示 | 缩略图用压缩图；点击放大看原图；只显示原图尺寸与原图大小（如 `1920×1080 · 1.4MB`） |
| 复制图片 | 复制原图；`execCommand('copy')` 选中图片优先，失败则打开原图并提示右键「复制图片」（局域网 HTTP 无法写剪贴板位图） |
| 提示词模板 | 固定 A/B/C 三槽、多行、20000 字符上限、单框复制、不填入其它输入框 |
| 自动保存 | 停止输入 600ms + 失焦 + 切页强制落库；整块一次 PUT；后写覆盖；清空也落库 |
| 权限 | 频道所有者账号（`owner_id` 隔离），非本人一律 404 |
| 锁定态 | 项目未冻结时整页只显示锁定提示，模板区不出现 |
| 频道删除 | 同一事务内删除模板行（两张图在 BYTEA 内，随行消失），删除摘要新增 `cover_templates` 计数 |
| 项目删除 | 不涉及（模板不含项目维度） |

## 2. 数据结构

表 `channel_cover_templates`（迁移 `0041`，`backend/migrations/versions/0041_channel_cover_templates.py`）：

- `channel_id` UUID PK，FK `channels.id` `ON DELETE RESTRICT`
- 原图：`cover_original` BYTEA + `media_type` / `format` / `width` / `height` / `byte_size` / `sha256`
- 压缩图：`cover_preview*` 同上（WebP，长边 1280，质量 82，不裁剪、不放大、保留透明）
- `prompt_a` / `prompt_b` / `prompt_c` TEXT NOT NULL DEFAULT ''
- `updated_at` TIMESTAMPTZ NOT NULL DEFAULT now()

## 3. 接口

全部在 `/identity/channels/{channel_id}/cover-templates` 下，按频道归属校验：

- `GET /` → `{channel_id, cover|null, prompts:{a,b,c}, updated_at}`
- `PUT /prompts` body `{a,b,c}`（extra=forbid）→ 同上视图
- `POST /cover` multipart `file`（jpg/jpeg/png/webp，≤10MB，≤4000 万像素）→ 201 + 视图
- `HEAD|GET /cover/media/{variant}`，`variant ∈ original|preview`，ETag + Range，私有缓存

上传校验顺序：扩展名 → 大小 → 可解码 → 像素上限；压缩失败不回退原图冒充成功。

## 4. 前端

- 模块：`frontend/cover-templates.js`（纯函数 + 字符串模板，不碰 DOM/网络）
- 挂载：`app.js` 的 `renderCoverStageView` 一处调用 `coverTemplateBlockMarkup()`；事件在 `bindCoverTemplateEvents()`
- 样式：`styles.css` 的 `/* 临时功能（2026-10-21）… */` 段
- 构建：`scripts/build.mjs` 文件清单已加入 `frontend/cover-templates.js`

## 5. 永久改造（不属于临时功能，删除功能时保留）

「更多设置」折叠容器：把锚点条 + 风格来源 / 参考图与勾选 / 生成输入 / 出场角色卡 / 提示词 收进一个默认折叠的容器；封面图区块保持原位置（第 6 位）。折叠状态存 `localStorage`，键 `tubeflow:cover-more-settings:<projectId>`，值 `open|closed`；点锚点自动展开并滚动。实现：`cover-view.js` 的 `coverMoreSettingsKey/coverMoreSettingsOpen`、`cover-templates.js` 的 `coverMoreSettingsMarkup`、`app.js` 的 `setCoverMoreOpen/bindCoverMoreSettings`。

## 6. 删除此功能（清单）

1. 删除后端包：`backend/src/tubeflow_test/cover_templates/`（6 个文件）。
2. 删除迁移文件 `backend/migrations/versions/0041_channel_cover_templates.py`；若线上已升级，先执行
   `alembic downgrade 0040`（会 DROP 表 `channel_cover_templates`）。
3. `backend/src/tubeflow_test/adapters/persistence/schema.py`：删 `_cover_template_models` 的 import 与 del 项。
4. `backend/src/tubeflow_test/api/app.py`：删 3 个 import 与 `create_cover_templates_router(...)` 注册块。
5. `backend/src/tubeflow_test/adapters/persistence/channel_deletion.py`：删 `ChannelCoverTemplateRow` import、`template_count` 查询、`delete(...)` 与 `cover_templates=` 参数。
6. `backend/src/tubeflow_test/identity_project/ports.py`：删 `cover_templates: int = 0` 字段；
   `identity_project/router.py`：删 `"cover_templates": summary.cover_templates`。
7. 删除前端模块 `frontend/cover-templates.js` 与测试 `frontend/cover-templates.test.js`。
8. `app.js`：删 `cover-templates.js` import、`state` 里 7 个 `coverTemplate*` 字段与重置行、`coverTemplateBlockMarkup` 调用、`// ---------------- 临时功能…` 整块函数、`bindCoverTemplateEvents()` 调用、`pagehide/visibilitychange` 的模板 flush 监听、`renderCoverStageView` 的焦点恢复可保留（与功能无关）。
9. `frontend/api-client.js`：删 `channelCoverTemplates / saveChannelCoverTemplatePrompts / uploadChannelCoverTemplate / channelCoverTemplateMediaUrl / channelCoverTemplateOriginalBlob`。
10. `styles.css`：删 `/* 临时功能（2026-10-21）… */` 段（`cover-template*` 与 `.cover-template-lightbox[hidden]`）。
11. `scripts/build.mjs` 文件清单删 `frontend/cover-templates.js`；`package.json` 的 `lint/typecheck` 删对应 `node --check`。
12. 后端测试：删 `tests/unit/test_cover_templates_api.py` 与 `tests/integration/test_cover_templates_postgres.py`；`test_channel_deletion.py` 删 `cover_templates` 断言与 `template_counts`；`test_channel_deletion_postgres.py` 删模板种子与断言。
13. `alembic_version` 回到 `0040`，确认 `channel_cover_templates` 不存在。

## 7. 上线记录（2026-10-22，已完成）

- 做法：以现役镜像 `tubeflow-test:purge-constraint-20261020`（`a126bc91…`）为基础，把「运行中容器的完整 site-packages（含 10-21/10-22 全部 overlay）+ 新包与改动文件 + 前端 4 文件 + 0041 迁移」烘焙成新镜像，未重装依赖。
- 新镜像：`sha256:907880e6f1a4d378169cea9d822d400b26c3dbb1478bc697eff36a83fa209009`
- compose 备份：`docker-compose.yml.bak-cover-templates-20261022`；三处 image（web/worker/migrate）已切新 sha。
- 迁移：`alembic current` = `0041 (head)`；表 19 列与模型一致。
- 校验：web/worker healthy、`/health/live` 200、启动日志无异常；`/app.js` 407608 B（含 `publishProjectFromStage` 3 处、`coverTemplateBlockMarkup` 2 处）；`/frontend/cover-templates.js` 200 `text/javascript` 8330 B；`/frontend/api-client.js` 14824 B、`/frontend/cover-view.js` 26974 B（均为新版本）；未登录访问模板接口 401；首页可渲染登录表单。
- 今日 S6 overlay 全部保留（worker.py `66a45eba…`、sqlalchemy_repository.py `a6cc0836…`、database.py `929f4469…`、video_batch.py `fa67f554…`）。
- 踩坑（务必记住）：镜像里前端模块实际服务目录是 `/app/frontend/frontend/`，而 `/app/frontend/` 是静态根；**新增或修改模块必须同时覆盖两个目录**，否则浏览器会拿到 `index.html` 或旧版模块。迁移文件也必须在构建时 COPY 进 `/app/migrations/versions/`，否则 `alembic upgrade` 无动作。
- 运维记录：`runtime/operations/cover-templates-20261022/README.md`（部署机同路径）。

### 部署步骤（归档，实际执行版）
1. 在部署机独立目录 `runtime/build/cover-templates-20261022/` 下：
   - `docker cp` 运行中容器的 `site-packages/tubeflow_test` 作为基线（保留全部 overlay，删 `__pycache__`）；
   - 覆盖新包与 5 个改动文件、前端 5 个文件（根目录 + `/app/frontend/frontend/` 各一份）、0041 迁移文件。
2. `FROM tubeflow-test:purge-constraint-20261020` + 4 条 `COPY` 构建新镜像（秒级，无网络依赖）。
3. 备份 compose，三处 image 切新 sha。
4. `docker compose run --rm --no-deps migrate`（应用 0041）。
5. `docker compose up -d --no-build web worker` 重建容器。
6. 校验：healthy、`/health/live`、静态模块 200 `text/javascript`、接口 401、`alembic current`、标记数量、今日 overlay md5。
7. 回滚：恢复 compose 备份 → `up -d --no-build`；如需降库，用新镜像执行 `alembic downgrade 0040`（旧镜像没有 0041 文件）。
