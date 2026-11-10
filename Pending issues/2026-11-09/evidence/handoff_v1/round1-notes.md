# 第 1 轮导出：两个需要 Claude 知道的偏差（2026-11-10）

导出本身成功（`results/` 62 个文件、6 667 810 字节、`grep -rIn "https\?://"` 0 命中），`manifest.json` 记录 8 条错误。按交接包要求**未改脚本、未改查询口径、未重跑**。

## 1. 8 条 `media_asset_not_found`（角色参考图没有导出）

| 项 | 事实 |
| --- | --- |
| 影响 | 8 个重点组各缺 `character_1.jpg`（`2280f7c6` g115/116/117/119/125/127/128、`93d717fa` g003） |
| 快照里的 id | `snapshot.character_assets[].id`，如 `2280f7c6` 为 `2c68da16-6270-4ec5-a181-472d9ef6c7b1`、`93d717fa` 为 `7523c0ef-c2db-43f2-8ba3-d8a777ddcd5b` |
| 直接原因 | `motion_round1_export.py::_save_image` 按 `media_assets.id` 查行；这两个 id 在 `media_assets` 命中 **0 行** |
| 根因（只读核实） | 该 id 属于 **`character_images`** 表；该表字段为 `id / owner_id / asset_id / object_key / sha256 / media_type / size_bytes / width / height / channel_id`，即角色图自带 `object_key`，不经 `media_assets` 解析 |
| 未做 | 未改脚本、未改查询、未重跑；面板图（`panel_shot*.jpg`）与故事板（`storyboard.jpg`）均正常导出 |

**建议（留给 Claude 决定）**：v2 包若仍需角色图，把角色分支改为按 `character_images.id` 取 `object_key` 后直接 GET 对象存储；本轮其余产出不受影响。

## 2. 全量归档体积（部署相关，非本轮导出）

合并提交的全量 `git archive` 为 **727 532 227 字节 / 1751 条目**（`Pending issues/` 下已入库的证据 PNG 占绝大部分）。部署构建改用「只取 Dockerfile 消费路径」的归档（865 707 字节 / 326 条目），镜像内容不受影响（5 个改动文件哈希与合并树逐条相同）。详见 `docs/DEPLOYMENT.md` §48。
