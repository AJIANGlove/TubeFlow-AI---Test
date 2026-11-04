# H3 工作流与参考图接线事实（只读取证）

采集时间：2026-11-04T04:31Z–2026-11-04T04:45Z（UTC）
范围：步骤 1 与步骤 2 出现的两个 workflow 身份：
- `h3.tubeflow.storyboard-to-video@1.0.0`（candidate 池）
- `h3.community.reference-images-to-video@1.1.0-mvp`（legacy 池）

来源：
- 引擎容器 `kinestack-h3-clones-h3-engine-new-{1,2,3}-1`，镜像 `sha256:88c0df22…`，`H3_WORKFLOW_PROFILE=candidate-hd`。三台文件的 sha256 完全一致。
- GPU 节点 `2cop10ldfhkcmhwx` / `ob6kefgwgyqkpy33` 上的 ComfyUI 实现代码；`8kdu1z040qlsjb9v` 的 SSH 在 `kex_exchange_identification` 阶段失败，未读取。
- 应用侧构造代码在仓库 `backend/src/tubeflow_test/video_generation/`，仅用于解释 `binding_key`/`ordinal` 的来源。

所有行号均为 1-based；引用文件均已复制到本目录。未在节点写入任何文件。

---

## 1. `h3.tubeflow.storyboard-to-video@1.0.0`

### 1.1 文件与身份

- `manifest.json:3-4`：
  ```json
  "workflow_id": "h3.tubeflow.storyboard-to-video",
  "workflow_version": "1.0.0",
  ```
- `workflow.json:46-76` 是唯一的 `MiniMaxH3AudioConditioningT8` 节点 `condition`。
- `workflow.json:73-74`：
  ```json
  "strict_prompt_tags": true,
  "ref_image_size": "match",
  ```
- `manifest.json:96`：`"max_reference_images": 9`。
- `manifest.json:57-63` 的绑定：
  ```json
  {
    "source": "references.ordered_images",
    "target": {
      "node_id": "condition",
      "input_name": "ref_images"
    },
    "value_type": "staged_asset"
  }
  ```

### 1.2 (a) 参考图数量与上限

分为四层，均为独立事实：

1. 本项目实际：`fa3a0b30_routing.json` 的 13 条 `groups[]` 全部为 `reference_count: 2`，`binding_key` 为 `reference_image_1`、`reference_image_2`；`request_json.references` 在 13/13 条中不存在，`references_source` 为 `input_snapshot_json.logical_request.references`。
2. G1 生产单格：`g1_single_panel_runs.json` 的 `2026-11-03_production_single_panel` 三条均为 `reference_count: 6`，`binding_key` 为 `reference_image_1` 至 `reference_image_6`。
3. G1 独立实验：`g1_single_panel_runs.json` 的 `2026-10-30_independent_experiment` 三条均为 6 张参考，按 `reference_names_in_order` / `reference_hashes_in_order` 排列。
4. 工作流/节点上限：
   - manifest：`manifest.json:96` → `max_reference_images: 9`；manifest 中没有 `min_reference_images` 键（`not found`）。
   - 节点 Autogrow：`_node_code/nodes.py:51`：
     ```python
     io.Autogrow.Input("ref_images", optional=True, template=io.Autogrow.TemplatePrefix(input=io.Image.Input("ref_image"), prefix="ref_image_", min=0, max=9)),
     ```
   - 节点运行时硬校验：`_node_code/conditioning.py:165-166`：
     ```python
     if len(ref_image_values) > 9 or len(ref_video_values) > 3 or len(ref_audio_values) > 3:
         raise ValueError("MiniMax H3 reference limits are 9 pictures, 3 videos, and 3 standalone audios")
     ```
   - 应用层：`backend/src/tubeflow_test/video_generation/domain.py:171` 要求 1–8 张角色图；`:186-191` 要求镜头参考 ≤6 且角色+镜头参考总数 ≤9：
     ```python
     if not 1 <= len(self.character_images) <= 8:
         raise VideoRuleViolation("a group video requires 1 to 8 character images")
     ...
     if len(self.character_images) + len(orders) > 9:
         raise VideoRuleViolation("video reference image limit exceeded")
     ```
   - 节点对 `Ref2VA` 至少 1 张参考的要求：`_node_code/conditioning.py:100-101`：
     ```python
     if need_refs and not has_refs:
         raise ValueError(f"{requested.upper()} requires at least one reference media input")
     ```

### 1.3 (b) `binding_key` / `ordinal` 如何接到 graph 节点

- 引擎渲染在 `engine_h3_workflows.py:192-198`：
  ```python
  for item in sorted(request.staged_assets, key=lambda value: value.ordinal):
      node_id = f"reference_{item.ordinal}"
      workflow[node_id] = {
          "class_type": "LoadImage",
          "inputs": {"image": staged_files[str(item.staged_asset_id)]},
      }
      workflow["condition"]["inputs"][f"ref_images.ref_image_{item.ordinal - 1}"] = [node_id, 0]
  ```
  事实：
  - 排序键是 `ordinal`，不是 `binding_key`；`binding_key` 在该文件里 **0 次出现**（`grep -n "binding_key" engine_h3_workflows.py` 无输出）。
  - graph 节点 id 为 `reference_<ordinal>`，节点类型 `LoadImage`，输入图片是 staged 文件。
  - `condition` 的输入键为 `ref_images.ref_image_<ordinal-1>`，即 **零基**：ordinal 1 → `ref_image_0`，ordinal 6 → `ref_image_5`。
- manifest 的绑定只声明 `references.ordered_images` → `condition.ref_images`（`manifest.json:57-63`），不区分参考类型。
- 应用侧 `binding_key`/`ordinal` 的构造在 `backend/src/tubeflow_test/video_generation/domain.py:198-233`：角色按 1..N 生成 `reference_image_N`；若为单格策略，逐个追加 `SHOT_PANEL`；否则在最后追加一张 `STORYBOARD`。`_cn()` 在 `:244-245`，只覆盖 一..九。
- 是否 storyboard-specific：
  - `engine_h3_workflows.py` 中 `storyboard` 只出现在 workflow id 常量与 profile 名称：`:21` `TUBEFLOW_STORYBOARD_WORKFLOW_ID`、`:29`/`:31` profile 列表、`:157` namespace 判断；没有按 `STORYBOARD`/`SHOT_PANEL`/`binding_key` 分支的代码（`grep -nE "STORYBOARD|SHOT_PANEL|binding_key|reference_kind"` 仅命中上述 workflow id 行）。
  - `workflow.json` 中 `storyboard` 只出现在 `_meta.title`（`:49`）；没有名为 storyboard 的节点或输入。
  - 因此：graph 层没有故事板专用输入；所有参考都走同一组 `ref_images.ref_image_N`。应用层按 `domain.py:223-232` 的顺序把整张故事板放在最后一个 ordinal（composite 策略）或改用逐格 `SHOT_PANEL`（single-panel 策略），但该类型信息不进入引擎请求的 `references` 字段（该字段只有 `binding_key`/`ordinal`/`asset_sha256`，见 `orchestration.py:3012-3019`）。

### 1.4 (c) `ref_image_size` 与画幅处理

- 工作流值：`workflow.json:74` → `"ref_image_size": "match"`。
- 引擎赋值：`engine_h3_workflows.py:189-191`：
  ```python
  workflow["condition"]["inputs"]["ref_image_size"] = (
      request.parameters.reference_image_size or "match"
  )
  ```
- 应用请求的 `parameters` 不包含 `reference_image_size`：`backend/src/tubeflow_test/video_generation/orchestration.py:3020-3025` 只有 `duration_seconds`、`aspect_ratio`、`resolution_preset`、`seed`。因此本次请求实际使用工作流/引擎默认 `"match"`。
- 节点实现：`_node_code/conditioning.py:109-117`：
  ```python
  def _resize_reference_image(image, width: int, height: int, ref_image_size: str):
      h, w = int(image.shape[1]), int(image.shape[2])
      if ref_image_size == "match":
          scale = min(1.0, math.sqrt((width * height) / (w * h)))
      else:
          scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
      target_width = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
      target_height = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
      return resize_image(image[:1], target_width, target_height), target_width, target_height
  ```
- 常量：`_node_code/core.py:14` `CANVAS_MULTIPLE = 32`；`:18` `REF_IMAGE_SHORT_EDGE = 2048`。
- `resize_image`：`_node_code/core.py:64-69`：
  ```python
  def resize_image(image: torch.Tensor, width: int, height: int, crop: str = "disabled") -> torch.Tensor:
      if image.ndim != 4:
          raise ValueError(f"Expected IMAGE [B,H,W,C], got {tuple(image.shape)}")
      samples = image[..., :3].movedim(-1, 1)
      samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
      return samples.movedim(1, -1)
  ```
- `common_upscale`：`_node_code/comfy_utils.py:1105-1137`。关键分支：
  ```python
  if crop == "center":
      ...
      s = samples.narrow(-2, y, old_height - y * 2).narrow(-1, x, old_width - x * 2)
  else:
      s = samples
  ...
  elif upscale_method == "lanczos":
      out = lanczos(s, width, height)
  ```
  `_node_code/comfy_utils.py:1095-1103` 的 `lanczos` 直接 `image.resize((width, height), resample=Image.Resampling.LANCZOS)`。
- 因此：`ref_image_size="match"` 时，参考图按源图宽高比乘同一 `scale` 缩到目标宽高，`scale` 上限 1.0（只缩不放）；目标宽高按 32 像素取整。`crop="disabled"` 不进入 `crop == "center"` 分支，不裁剪、不补边；整张图被 `lanczos` 重采样到目标宽高。由于目标宽高由源图宽高比计算，只有 32 像素取整造成的轻微非等比量化偏差；代码没有把参考图强制到输出画布（16:9 时为 1056×608）的宽高比。`max` 时 `scale = min(1.0, 2048 / min(w,h))`，同样只按短边上限缩小、不裁剪、不补边。
- 数值示例（仅算式，不是运行结果）：输出 1056×608、源图 1600×1200 时，`scale = min(1, sqrt(643072/1920000)) = 0.5787`，`target_width = 928`、`target_height = 704`（32 的整数倍），宽高比 1.318 与源图 1.333 的差异来自 32 像素取整；整图被重采样到 928×704，没有裁剪或补边。

### 1.5 (d) `<Picture N>` / `@图N` 与 `strict_prompt_tags`

- 应用生成中文标签：`backend/src/tubeflow_test/video_generation/domain.py:200-233` 使用 `f"@图{_cn(index)}"` / `f"@图{_cn(ordinal)}"`；`_cn()` 在 `:244-245` 只覆盖 一..九。
- 引擎翻译：`engine_h3_workflows.py:228-233`：
  ```python
  def translate_references(prompt: str, count: int) -> str:
      labels = "一二三四五六七八九"
      translated = prompt
      for ordinal in range(1, count + 1):
          translated = translated.replace(f"@图{labels[ordinal - 1]}", f"<Picture {ordinal}>")
      return translated
  ```
  调用点 `:181`：`translate_references(request.prompt, len(request.staged_assets))`。只替换 1..count 的 `@图` 中文标签；不处理 `<Image N>` 或英文别名。
- 节点解析：`_node_code/prompt_tags.py:7-12` 定义两个正则：
  ```python
  MEDIA_TAG_RE = re.compile(
      r"<\s*(Image|Picture|Video|Audio)\s*(\d+)\s*>|"
      r"(?<![\w<])(Image|Picture|Video|Audio)\s*#?\s*(\d+)\b(?!\s*>)",
      re.IGNORECASE,
  )
  OFFICIAL_TAG_RE = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>", re.IGNORECASE)
  ```
  `canonicalize_media_tags`（`:15-22`）把 `<Image N>`、`Picture N`、`Image#N` 等统一成 `<Picture N>` / `<Video N>` / `<Audio N>`。
  `prepare_prompt`（`:25-51`）用 `OFFICIAL_TAG_RE` 逐条检查 ordinal；越界时生成 warning（`:43-48`）；`strict=True` 且有 warning 时抛错（`:49-50`）：
  ```python
  if strict and warnings:
      raise ValueError("MiniMax H3 prompt media tag validation failed: " + "; ".join(warnings))
  ```
- `strict_prompt_tags` 的来源：`_node_code/nodes.py:44` 默认 `True`；本工作流 `workflow.json:73` 显式 `true`。
- 图片计数来源：`_node_code/conditioning.py:205-217` 遍历 `ref_image_values` 并在 `:217` 追加 `picture_labels.append(f"ref_image_{index}")`；`:291` 生成 `counts = {"pictures": len(picture_labels), ...}`；`:292-298` 调用 `prepare_prompt(..., strict=strict_prompt_tags)`。因此 tag 的合法 ordinal 上限等于实际接线的图片数（本工作流为 `ref_image_0..N-1` 对应的 1..N）。
- 节点不识别 `@图N`：`prompt_tags.py` 的正则只含 `Image|Picture|Video|Audio`，不含 `@图`。若 `@图N` 未被引擎替换（例如 N 超出 `len(staged_assets)`），节点不会把它当作媒体标签，也不会触发 strict 校验。

---

## 2. `h3.community.reference-images-to-video@1.1.0-mvp`

### 2.1 文件与身份

- `manifest.json:3-4`：
  ```json
  "workflow_id": "h3.community.reference-images-to-video",
  "workflow_version": "1.1.0-mvp",
  ```
- `workflow.json:9` 的 `condition` 节点：
  ```json
  "condition": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"clip": ["text_encoder", 0], "vae": ["video_vae", 0], "audio_vae": ["audio_vae", 0], "prompt": "", "width": 1056, "height": 608, "length": 124, "ref_image_size": "match"}}
  ```
- `manifest.json:30`：`"max_reference_images": 9`。
- `manifest.json:22` 的绑定：
  ```json
  { "source": "references.ordered_images", "target": { "node_id": "condition", "input_name": "ref_images" }, "value_type": "staged_asset" }
  ```

### 2.2 (a) 参考图数量与上限

- 本项目实际：13/13 当前组视频均使用 candidate workflow（`fa3a0b30_routing.json` 的 `request_json_workflow.workflow_id` 全为 `h3.tubeflow.storyboard-to-video`），没有 attempt 使用本 legacy workflow；本项目中该 workflow 的实际参考数为 `not found`（0 条 attempt）。
- manifest：`manifest.json:30` → `max_reference_images: 9`；无 `min_reference_images` 键（`not found`）。
- 节点 Autogrow：`_node_code/legacy_nodes_minimax_h3.py:265-268`：
  ```python
  io.Autogrow.Input("ref_images", optional=True,
      template=io.Autogrow.TemplatePrefix(
          input=io.Image.Input("ref_image", tooltip="Reference image (downscaled to 2048 short edge if larger, never upscaled)"),
          prefix="ref_image_", min=0, max=9)),
  ```
- 应用层上限同 1.2 第 4 条（`domain.py:171`、`:186-191`）：1–8 角色、镜头参考 ≤6、总数 ≤9。

### 2.3 (b) `binding_key` / `ordinal` 如何接到 graph 节点

- 引擎接线与 workflow 无关：`engine_h3_workflows.py:192-198` 同样按 `ordinal` 注入 `reference_<ordinal>` 的 `LoadImage`，并写 `condition.inputs["ref_images.ref_image_<ordinal-1>"]`（零基）。
- manifest 绑定：`manifest.json:22` → `references.ordered_images` 到 `condition.ref_images`。
- 节点侧：`_node_code/legacy_nodes_minimax_h3.py:293` 直接遍历 `(ref_images or {}).values()`，没有按 `binding_key` 分支，也没有显式排序；`reference_hashes`/`binding_key` 不参与节点逻辑。节点文档 `:241-247` 说明参考按 images → videos → audio 的固定类型顺序呈现，`<Picture i>` 的 i 是每种类型内的 1-based ordinal。
- 是否 storyboard-specific：`legacy_nodes_minimax_h3.py` 中 `storyboard` 0 次命中；`workflow.json` 中没有 storyboard 节点/输入。graph 层没有故事板专用输入，所有图片参考走同一 `ref_images.ref_image_N`。

### 2.4 (c) `ref_image_size` 与画幅处理

- 工作流值：`workflow.json:9` → `"ref_image_size": "match"`。
- 引擎赋值：`engine_h3_workflows.py:189-191` 同一逻辑（请求未带该参数时用 `"match"`）。
- 节点实现：`_node_code/legacy_nodes_minimax_h3.py:293-304`：
  ```python
  for img in (ref_images or {}).values():
      ...
      if ref_image_size == "match":
          # aspect-preserving scale (down only) to the generation's pixel area
          scale = min(1.0, math.sqrt((width * height) / (w * h)))
      else:
          scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
      tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
      th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
      resized = _resize(img[:1], tw, th, "disabled")
  ```
- 常量：`legacy_nodes_minimax_h3.py:26` `CANVAS_MULTIPLE = 32`；`:29` `REF_IMAGE_SHORT_EDGE = 2048`。
- `_resize`：`legacy_nodes_minimax_h3.py:64-68`：
  ```python
  def _resize(image, width, height, crop):
      # image [B, H, W, C] -> [B, height, width, 3]
      samples = image[..., :3].movedim(-1, 1)
      samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
      return samples.movedim(1, -1)
  ```
- `common_upscale` 同一实现：`_node_code/comfy_utils.py:1105-1137`；`crop="disabled"` 不进 center-crop 分支，整图 `lanczos` 重采样到目标宽高，不裁剪、不补边。行为与 1.4 相同：按源图宽高比缩小（只缩不放），32 像素取整；没有强制到输出画布宽高比。
- 节点 schema 的 tooltip 直接写明：`legacy_nodes_minimax_h3.py:263-264`：
  ```python
  io.Combo.Input("ref_image_size", options=["match", "max"], default="match",
      tooltip="Reference image sizing. 'match' scales each ref (down only, keeping aspect) to the generation's pixel area; 'max' uses the reference pipeline's 2048px short edge for best identity fidelity. Reference tokens ride through every sampling step, so 'max' can be several times slower."),
  ```

### 2.5 (d) `<Picture N>` / `@图N` 与 `strict_prompt_tags`

- 引擎翻译：`engine_h3_workflows.py:228-233` 同样把 `@图N` 替换为 `<Picture N>`（N ≤ `len(staged_assets)`）。
- 本 workflow 的节点 `MiniMaxH3ReferenceToVideo` 没有 `strict_prompt_tags` 输入；`legacy_nodes_minimax_h3.py` 中 `strict_prompt_tags` **0 次命中**（`not found`）。节点没有 `prompt_tags.py` 的 canonicalize/越界校验逻辑；`prompt` 未经标签解析直接进入 `clip.tokenize(prompt, minimax_ref_items=ref_items)`（`:351-352`）。因此该 workflow 的 tag 解析/严格校验：**not found**；只有引擎侧 `@图N` → `<Picture N>` 的替换事实。
- 节点文档仍声明 prompt 使用 `<Picture i>` / `<Video k>` / `<Audio j>`（`legacy_nodes_minimax_h3.py:245-252`），但代码中没有对标签 ordinal 做边界检查。

---

## 3. 应用侧与引擎侧的边界事实

- `request_json.references` 在 staging 后被替换为 `staged_assets`：`backend/src/tubeflow_test/video_generation/orchestration.py:2059-2075`。归档 attempt 的 `request_json` 里没有 `references`；本报告按 `input_snapshot_json.logical_request.references` 取数，并在 `fa3a0b30_routing.json` / `g1_single_panel_runs.json` 标注 `references_source`。
- 逻辑请求的 `references` 由 `_request()` 生成：`orchestration.py:3012-3019`，字段仅 `binding_key`、`ordinal`、`asset_sha256`。
- 引擎只使用 `ordinal` 和 `staged_asset_id`；`binding_key` 在 `engine_h3_workflows.py` 中 0 次出现。
- 本项目的 13 条当前视频全部为 `h3.tubeflow.storyboard-to-video@1.0.0`，节点 `h3-new-2` / `h3-new-3`；`h3_project_workflow_routes` 的 `profile=candidate`、`reference_strategy=composite`、`reference_derivation_json=null`（`fa3a0b30_routing.json` 的 `route`）。
- 引擎侧 `engine_h3_workflows.py:27-33` 的 `PROFILE_WORKFLOWS` 中，`candidate` 与 `candidate-hd` 两个 profile 的清单都包含 `h3-tubeflow-storyboard`；三个引擎容器的实际 `H3_WORKFLOW_PROFILE` 为 `candidate-hd`。
- G1 生产三条为 `single_panel_v1`、`derivation={"version":"crop-title54-v1"}`、6 张参考、节点 `h3-new-2`/`h3-new-2`/`h3-new-3`（`g1_single_panel_runs.json`）。
- G1 独立实验三条为直接 ComfyUI API 图，`condition.class_type=MiniMaxH3AudioConditioningT8`，`ref_images.ref_image_0..5`，6 个 `LoadImage` 节点 `reference_1..6`，`ref_image_size=match`，`strict_prompt_tags=true`，`width=1056`、`height=608`、`length=362`；实验记录中没有 TubeFlow/KineStack workflow_id/version（`not found`）。

## 4. 明确 not found

- `h3.tubeflow.storyboard-to-video@1.0.0` manifest 的 `min_reference_images`：not found。
- `h3.community.reference-images-to-video@1.1.0-mvp` 节点/工作流中的 `strict_prompt_tags`：not found（0 次命中）。
- `h3.community.reference-images-to-video@1.1.0-mvp` 在本项目中的实际 attempt / 参考数：not found（0 条 attempt 使用该 workflow）。
- G1 2026-10-30 三条实验的 `workflow_id` / `workflow_version` / `binding_key`：not found（记录为直接 ComfyUI API 图 + `reference_names_in_order`，无这些字段）。
- GPU 节点 `8kdu1z040qlsjb9v` 上的实现代码：not found（SSH `kex_exchange_identification` 失败）。
- legacy 三台引擎容器的 `/capabilities` 响应：not found（`kinestack_control` 内 DNS 解析失败；容器不存在）。
