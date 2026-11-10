# 第 1 轮：只读导出（2026-11-09）

**只读**：数据库查询全部在 READ ONLY 事务里；对象存储只 GET；不调用任何 Provider；不写库、不写桶、不改配置。

## 1. 导出对象

| 项目 | 内容 |
| --- | --- |
| `2280f7c6-f0c7-439a-8d82-71c52b588cbf`（Maya，163 组） | 全部组当前视频尝试的提示词与编译输入；重点组 115、116、117、119、125、127、128 的图片与故事板请求 |
| `93d717fa-e23c-4ee6-86dc-f5558088003b`（E2E，9 组） | 同上；重点组 3 |

重点组对应 2026-11-09 测过的镜头：116 船上、119 湖岸与蓄水池、125 城市俯瞰、127 湖岸月景、128 丛林遗迹；115、117 为船上镜头前后组；E2E 第 3 组为背景完全静止的人物面部镜头。

## 2. 步骤（部署机）

```
docker cp motion_round1_export.py tubeflowai-test-web-1:/tmp/
docker exec -e OUT=/tmp/motion_round1 tubeflowai-test-web-1 python /tmp/motion_round1_export.py
docker cp tubeflowai-test-web-1:/tmp/motion_round1 ./results
docker exec tubeflowai-test-web-1 rm -rf /tmp/motion_round1 /tmp/motion_round1_export.py
```

## 3. 产出（放本目录 `results/`）

- `prompts.json`：每组的 seed、节点、策略、编译模型、是否修复过、编译后完整提示词、编译器输入（导演镜头的 scene / subject_action_outcome / description 等、镜头时间段、画风文本）、运动指标。
- `focus/<项目前8位>_g<组号>/`：`character_N.jpg`、`panel_shotN.jpg`、`storyboard.jpg`（JPEG 副本，长边 ≤1600 px；原图 sha256 与尺寸在 `images.json`）、`storyboard_input.json`（故事板请求，URL 与长串二进制已去除）、`prompt.txt`。
- `manifest.json`：每个项目的组数、有当前视频的组数、缺失的组、策略种类、错误清单。

## 4. 注意

- 脚本首次运行若报错：记录原始异常后停下，不改库、不改查询口径。
- 项目 `2280f7c6` 若已被删除（P0 删除问题处置中），`manifest.json` 会记 `project_not_found`，照实汇报。
- 提交前检查 `results/` 里没有 URL、密钥或带签名的链接：`grep -rIn "https\?://" results/` 应无输出。
