# 视频人物 IP 不一致问题与排查证据

日期：2026-11-03  
范围：只读排查，未修改代码、未部署、未重新生成视频、未调用外部 Provider。

## 1. 问题现象

项目故事板中的人物 IP 正确，但部分故事板生成视频后，人物身份、发型、服装和人物数量发生变化。

本次以截图所示项目的第 11 组为核心样本，直接核对了生产数据库记录、角色参考图、当前故事板、实际发送给 H3 的请求以及当前视频关键帧。

## 2. 核对对象

- 项目 ID：`31e00d87-5332-4b60-88e9-8b6dd8d475e7`
- 项目标题：`“과일에 설탕만 입히면 된다더니…” 탕후루 가게의 숨은 인걸비`
- 权威配音分组数：147
- 分组序号：11
- 分组 ID：`c42821e5-397b-512d-b2f0-efee9383473e`
- 当前视频 attempt：`3c8e4259-f2d8-4c19-8777-39f93abaa548`
- `intent_sequence`：0
- 当前状态：`ARCHIVED`
- H3 workflow：`h3.tubeflow.storyboard-to-video`
- capability：`video.reference_images_to_video`
- 时长：15 秒

## 3. 角色图和故事板证据

### 3.1 角色参考图

- asset ID：`7523c0ef-c2db-43f2-8ba3-d8a777ddcd5b`
- SHA-256：`ef4afa3b005a72393c9c036ff31c43387334ee3e86bf34785aee339797cc3bca`
- 尺寸：1672 × 941
- 视觉内容：长卷发、戴眼镜、穿米色西装的年轻女性。

### 3.2 当前故事板

- asset ID：`b2c31775-5f00-57c2-9c53-c76fc9ae8802`
- SHA-256：`e101dd58ceee2ad652e650948391309c6c044826f02fd8e229c567020d6b21b5`
- 尺寸：1672 × 941
- 视觉内容：同一位长卷发、戴眼镜、穿米色西装的女性，位于糖葫芦店场景中。

**结论：第 11 组的角色图和故事板本身没有画错人物。**

## 4. 实际视频提示词中的冲突

当前 attempt 的 `request_json.prompt` 中，主体定义写入了以下与图片不符的内容：

```text
- (S1): A hand-drawn 2D-animated shop owner character with natural semi-realistic proportions, calm round face with softly defined features, short dark hair, wearing a simple warm-toned apron over a muted work shirt, standing as the self-employed store owner inside the tanghulu shop.
- (S2): A second hand-drawn 2D-animated character representing a franchise store operator, natural semi-realistic proportions, tidy short hair, wearing a neat branded uniform-style shirt with a plain apron, with a slightly tense, businesslike expression.
```

后续 `retention_analysis` 和镜头描述又继续要求保留上述短发、围裙、工作服、制服以及双人物构图。

这些描述与两张参考图存在两层直接冲突：

1. 参考图人物是长卷发、眼镜、米色西装，提示词却要求深色短发、围裙和工作服。
2. 故事板主体是一位人物，提示词却新增第二名加盟店员并要求双人镜头。

## 5. 当前视频证据

- 视频 asset ID：`bfa40f57-5f6c-4a7c-b155-c8b6f4a5ce1b`
- 视频 SHA-256：`9fba8f9223ca4caebbfd97929e4dc556c78cc7afaad58621cf92a5c58732207f`
- 已提取约 2 秒处的视频关键帧。
- 关键帧中出现两名短发、穿围裙的人物，而不是参考图中的长卷发、眼镜、米色西装女性。

视频结果与错误提示词中的人物组合一致，与角色图和故事板中的原始 IP 不一致。

## 6. H3 参考图提交证据

当前 attempt 的 `request_json.staged_assets` 包含两个条目：

```text
ordinal 1 → binding_key = reference_image_1
            staged_asset_id = 04da238c-ab84-4813-b3ea-7988a23cbcc5

ordinal 2 → binding_key = reference_image_2
            staged_asset_id = 2e3ff459-b4e1-492b-83a0-138e961fe11b
```

代码中的引用顺序是确定性排列：

1. 角色图片依次生成 `reference_image_N` 和 `@图N`；
2. 故事板或单格图片排在角色图片之后。

相关代码：`backend/src/tubeflow_test/video_generation/domain.py` 的 `GroupVideoGeneration.references`。

当前 attempt 的输入快照中，角色资产和故事板资产的 ID、SHA-256 均与第 3 节核对的正确图片一致。因此，现有记录一致指向“角色图为第 1 张、正确故事板为第 2 张”，**没有发现 `@人物` 错绑、角色图与故事板顺序颠倒或项目资产接错的证据。**

限制：归档后暂存对象已经清理，无法在本轮事后重新读取远端暂存字节，再做一次独立的逐字节校验；本结论依据的是持久化输入快照、请求记录、哈希和确定性绑定代码，不将其扩大表述为对远端暂存字节的重新验证。

## 7. 为什么会产生错误的人物描述

视频提示词编译器的输入合同只有：

- 角色图片标签；
- 故事板标签；
- 导演镜头文本；
- 镜头时间范围；
- 时长、画幅和视觉风格；
- 策略及幂等键。

`VideoPromptCompilerInput` 没有图片字节、图片 URL 或图片视觉描述字段。DeepSeek 适配器调用 `/chat/completions` 时，也将用户载荷作为纯文本 JSON 字符串发送，没有提交多模态图片内容。

相关代码：

- `backend/src/tubeflow_test/video_generation/orchestration.py`：`_compiler_input`
- `backend/src/tubeflow_test/video_prompt/domain.py`：`VideoPromptCompilerInput`
- `backend/src/tubeflow_test/adapters/deepseek_video_prompt.py`：`DeepSeekVideoPromptProvider.compile`

第 11 组实际 `compiler_input` 也只有 `@图一`、`@图二`、导演文本和风格文字，没有可供编译器识别人物外观的图片内容。但系统提示词要求编译器写出人物五官、发型、体型、服装等主体定义，由此产生了没有图像依据的人物描述。

**需要区分：这是错误提示词的形成机制，不等于 H3 没收到图片。**不能再把“编译器看不到图片”表述为“视频生成链路没有参考图片”。

## 8. 当前能够确认的根因

**直接问题是：同一个 H3 视频生成请求中，参考图片与文字提示词发生了严重的人物身份和人物数量冲突；最终视频没有保持参考图中的 IP，而是呈现了错误提示词所描述的人物。**

错误提示词的上游形成机制是：文本编译器在没有图片视觉输入的情况下，被要求生成具体人物外观和主体定义。

当前记录不支持把问题归因于故事板画错、角色图错误或 `@人物` 图片顺序接错。

## 9. 为什么错误视频会被放行

当前视频归档 QC 检查媒体技术规格、媒体完整性和持续黑屏等技术指标，没有对视频关键帧与角色图、故事板进行人物身份或 IP 一致性检查。

因此，即使视频已经换人，只要媒体技术检查通过，仍可进入 `ARCHIVED` 并成为当前视频。

相关代码：`backend/src/tubeflow_test/video_generation/orchestration.py` 的归档与媒体 QC 路径。

## 10. 尚不能据此定责的部分

以下两种机制在本样本中无法仅凭最终结果进一步区分：

1. 冲突文字在 H3 生成中占据主导，压过了正确参考图；
2. H3 对参考图人物身份的保持能力不足，即使存在参考图仍发生漂移。

要区分二者，需要在相同图片、相同节点和相同 seed 下，仅修改冲突文字进行受控对照实验。本轮未执行真实生成，因此不把其中任何一种推测写成已证实事实。

## 11. 本地证据副本

本轮只读复制到工作区的证据文件：

- `tmp/ip-group11-character.png`：角色参考图
- `tmp/ip-group11-storyboard.png`：当前故事板
- `tmp/ip-group11-video.mp4`：当前 H3 原始视频
- `tmp/ip-group11-video-frame.png`：约 2 秒处的视频关键帧

## 12. 一句话结论

**第 11 组不是“没有传对参考图”，而是“正确参考图与错误人物提示词同时进入视频生成请求，最终人物一致性失守，且现有技术 QC 没有识别并拦截这一问题”。**
