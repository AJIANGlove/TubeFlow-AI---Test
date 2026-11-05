# 封面图 V7：直接上传为不可变版本（2026-10-22）

对应决策：`docs/planning/USER_DECISIONS.md` §34；验收：`FINAL_ACCEPTANCE_CRITERIA.md` §33。
状态：已实现并通过测试，**已于 2026-10-23 上线**（镜像 `sha256:a117a6be…`，`alembic 0042`）。上线记录见 `runtime/operations/cover-uploads-20261023/README.md`。

## 1. 范围

- 第十步「封面图」新增「上传封面图」：本地图片直接成为项目封面图的不可变版本。
- 与 AI 出图版本共用：版本序列、当前指针、版本历史、受保护媒体、导出 ZIP、项目卡片缩略图。
- 不涉及：频道封面模板（临时功能，全频道共用、无版本）、参考图上传、AUTO 链路、导出合同。

## 2. 明确不做

- 不裁剪/编辑、不压缩、不重编码、不剥 EXIF；不做平台合规副本（§18.1 不变）。
- 不调用任何模型：无解析、无提示词、无出图、无复审；不写 outbox。
- 不删除/覆盖版本；不提供独立封面图下载入口。
- 不把上传并入 AUTO；不改变「生成封面图」的提示词依赖与首成功自动当前规则。

## 3. 数据与迁移 `0042`

`cover_image_generations` 同表扩展：

| 变更 | 说明 |
|---|---|
| `source String(16) NOT NULL server_default 'generated'` | `generated` = AI 出图；`upload` = 用户上传；`CHECK source IN ('generated','upload')` |
| `prompt_generation_id`、`model`、`size`、`quality` 可空 | 上传行无提示词血缘；不伪造提示词版本或模型参数 |
| `prompt` 仍非空 | 上传行写空串 |
| 不新增部分唯一索引 | `(owner_id, idempotency_key)` 已足够按项目+sha 去重 |

降级 fail-closed：存在 `source='upload'` 行时拒绝并给出人工处理 SQL（先清空项目指针再删行）；无上传行时恢复四列 NOT NULL 并移除 `source`。

## 4. 校验与写入顺序

`CoverImageService.upload`：

1. 项目存在且 `snapshot_script` 非空（冻结），否则 404 / 409。
2. `verify_character_image(content, max_bytes=25MB, max_pixels=40MP)`：空/超限/不可解码/格式非 png-jpeg-webp 直接 422。
3. 最小尺寸 1280×720；宽高比用整数运算判 16:9 ±1%（`|9w-16h|*100 <= 16h`）。〔2026-11-05 废止：取消最小尺寸与 16:9 限制，见 `USER_DECISIONS.md` §34 第 13 条〕
4. 对象键 `cover-images/uploads/{owner_id}/{project_id}/{sha256}`，原始字节条件写（同键复用，不覆盖）。
5. 落库：`status=SUCCEEDED`、`source='upload'`、`prompt_generation_id=None`、`model/size/quality=None`、`prompt=''`、`parameters={"source_filename": ...}`、`idempotency_key="upload:{project_id}:{sha256}"`、`qc_width/height/mime`、`object_key/object_sha256`、`downloaded_size`。
6. 仓储在项目锁内按 `(owner, project, source='upload', object_sha256)` 去重：命中切当前并返回 `reused=True`；未命中分配 `version=max+1`、插入、无条件切当前；**不写 outbox**。

失败语义：输入校验 422 且不落库不写对象；基础设施失败按现有约定 5xx；上传不产生 `FAILED` 版本。

## 5. 接口

`POST /projects/{project_id}/cover/images/uploads`（multipart `file`）

- 新版本：201，返回封面图视图 + `"reused": false`。
- 复用：200，同一 `generation_id` + `"reused": true`，当前指针指向该版本。
- 视图新增 `source`；上传版本的 `prompt_generation_id/model/size/quality` 为 `null`。
- `GET /projects/{project_id}/cover/images` 的 `allowed_actions` 增加 `UPLOAD_COVER_IMAGE`。

## 6. 前端

- `frontend/api-client.js`：`uploadProjectCoverImage(project, file)`（FormData，不手设 Content-Type）。
- `frontend/cover-view.js`：版本项新增 `source`；空态提示可直接上传图片〔2026-11-05 修订：不再限定 16:9〕。
- `app.js`：面板头次按钮「上传封面图」+ 隐藏 file input；上传中禁用并显示「上传中…」；成功 toast 区分新版本/复用；版本行标注「上传 · 」；项目切换重置上传状态。

## 7. 测试矩阵

- 服务单测：无提示词上传、同字节复用并切回当前、生成版本后版本递增、未冻结、跨账号、格式/大小/像素（2026-11-05 起不再校验最小尺寸与比例）、原字节与 mime、无 outbox。
- 仓储契约（SQLite + 内存）：去重、版本、指针、按项目隔离、活跃项目门禁。
- API：openapi 路径集合、未冻结矩阵、201/200 `reused`、无提示词可用、422 矩阵且无对象、跨账号 404、媒体原字节、`source` 与 allowed_actions。
- 迁移：0042 revision 链、列/可空/check 形状、与 ORM 元数据逐列一致、降级两分支。
- PostgreSQL 集成：真实迁移后落库字段、去重、并发同字节一行、导出 `cover.png` 原字节。
- 前端：api-client multipart；cover-view `source`/空态/wiring；`npm run check`。

## 8. 部署边界

已于 2026-10-23 00:43 上线：先应用 `0042`，再以 `907880e6…` 为基线烘焙 `a117a6be…`（前端模块同时覆盖 `/app/frontend/` 与 `/app/frontend/frontend/`，迁移文件 COPY 进 `/app/migrations/versions/`）。实测 `/health/live` 200、`alembic current=0042`、静态资源 Content-Type 与长度正确、未登录上传接口 401；回滚只需恢复 compose 备份并 `up -d --no-build`，代码回滚不必降库；必须降库时先按 §3 处理上传行。
