# 3a：长项目场景多样性只读核查（2026-11-07，待实施方执行）

依据：USER_DECISIONS §53 第 3 项；优化评审第 5 项（`PROJECT_DIRECTOR_PLAN_CHAPTER_LIMIT = 24`，全局规划每章只有一个 `visual_beat` 与 `setting`）。

**目的**：在真实长项目上看「同一章节、同一场景连续占了多少组、多少秒」，再由用户观看最长的那一段，判断是否重复到需要改。本步只读，不调用 Provider、不写库、不改代码与配置。

## 1. 口径

- **章节**：该项目最近一次 `SUCCEEDED` 前置导演运行的全局规划（`GLOBAL_PLAN` 单元的 `plan_document`，复用时沿 `reused_from_unit_id` 追溯）。
- **组归属**：组内已配音句子（`authoritative_narration_memberships`）按句子 ID 对应章节（导演句子 ID 与配音句子 ID 由同一 `authoritative_sentence_id` 生成）；一组跨两章时取句子多的一章，平局取前一章。
- **连续段**：相邻组主章节相同即为同一段；按时长排序给出前 3 段。
- **场景文本**：各组当前已应用导演修订（`storyboard_director_pointers.applied_revision_id`）的镜头 `scene`；「不同场景文本数」按小写、合并空白后逐字比较，**措辞不同的同一地点会被算成不同**，因此只作参考，以用户目视为准。
- **时间**：按配音时间轴（组 `start_ms`/`end_ms`）。`video_blocked_groups` 为 0 时与成片时间一致；不为 0 时须注明。

## 2. 步骤（部署机）

1. 只读选项目：

   ```
   docker cp scene_variety_check.py tubeflowai-test-web-1:/tmp/
   docker exec -e MODE=candidates tubeflowai-test-web-1 python /tmp/scene_variety_check.py
   ```

   输出最近完成、配音句数 ≥150 的 AUTO 项目（至多 10 个）及其导演运行的提示词版本。取第一个**不在保护名单**、且单元提示词为 `project-director-unit-v9` 或 `-v10` 的项目。没有符合条件的项目时停下汇报，不放宽条件。

2. 只读导出并计算：

   ```
   docker exec -e MODE=export -e PROJECT=<project_id> -e OUT=/tmp/scene_variety \
       tubeflowai-test-web-1 python /tmp/scene_variety_check.py
   docker cp tubeflowai-test-web-1:/tmp/scene_variety ./results
   ```

   产出 `export.json`（章节与分组原始数据）、`summary.json`、`groups.csv`。

3. 结果放本目录 `results/`；`summary.json` 的 `longest_runs[0].narration_time` 就是交用户观看的时段。

## 3. 汇报

- 项目 ID、标题、配音句数、组数、成片时长；导演运行 ID 与提示词版本。
- 章节数、是否达到 24 的上限；每章句数、秒数、组数（最小/中位/最大）。
- 最长 3 段：章节序号、`visual_beat`、`setting`、组号范围、时段、秒数、镜头数、不同场景文本数。
- 若成片在部署机上：给出最长一段在成片中的起止时刻，交用户观看（3b）。

## 4. 未验证

- `analyze` 部分已在 Claude 云端工作区用合成数据跑通（含跨章组的平局规则、场景文本归一化）。
- 数据库查询部分**未运行**（云端工作区没有数据库与 SQLAlchemy），列名按仓库 ORM 模型核对过；首次运行如报错，如实记录原始异常，不改库。
