# 封面图生成 v6 增量实施计划（垫图转正 + 升 a）

状态：已批准（2026-10-17 用户指令「按推荐执行」）。
基线：`COVER_GENERATION_PLAN_20260920.md`（v4）+ `COVER_GENERATION_PLAN_V5_20260921.md`（v5）+ §18.12（2026-10-15 优化）。
实证依据：`runtime/operations/cover_ab_spike_20261016.md` 与产物（开发机 `tmp/cover_exp/spike_out/`）：grsai nano-banana-pro + 风格锚图近乎逐风格复刻参考（G4_s2）；同模型纯文本基线漂移为插画风（G5）；lingkeai 文本路线当日降级且历史两次实证失败。

## 1. 范围

1. 垫图转正：当前提示词所用风格档案（频道风格版本或项目草稿）带参考图时，出图经 **grsai 锚图通道**——参考图上传 grsai 文件服务换取 URL，随任务以 `urls` 传入（复用 `adapters/grsai_grid_sheet.py` 的合同知识，封面适配器自包含实现）。模型固定 `nano-banana-pro`（可配置），不设故障转移（封面是手动功能，失败如实落码由用户重试）。
2. 路由策略（确定性）：风格档案有参考图 → 锚图通道；风格来源为 none 或档案无参考图 → lingkeai 文本出图（现状不变）。开关 `cover_image_anchor_enabled` 默认开；锚图通道未配置（grsai 未配置）而路由需要它 → 稳定失败码 `cover_image_anchor_not_configured`，不静默降级到文本通道。
3. 产物归一化：锚图通道产出尺寸以模型原生为准（nano-banana 2K 实测 2752×1536）；归档前经 Pillow 确定性缩放为 2560×1440。QC 链：先校验原始下载字节（哈希与 Provider 一致、可解码、格式），再归一化，再对归一化产物执行既有 QC（2560×1440/格式/可解码）。留痕：`expected_sha256`/`downloaded_sha256` = 原始下载哈希与大小语义不变（`downloaded_size` 存归档对象大小，供媒体完整性校验），`object_sha256` = 归一化归档哈希。
4. 绘字段、视觉复审、版本不可变与当前指针、复审作用于归档后图像——全部不变。
5. 升 a：角色卡解析任务单不再提取"画风"（卡只锁身份、头身比例、面部特征、标志性元素）；既有卡文本由用户手动重解析或编辑清理，代码不回填。
6. 幂等：锚图出图 canonical 请求哈希追加通道与参考图哈希（仅锚图路径追加该字段，文本路径哈希与现状逐字节一致）；同幂等键跨通道复用报 `idempotency_conflict`。

## 2. 明确不做（用户已逐项确认否决）

- G2（gpt-image-2.5 锚图支持）不重试；实验 1（lingkeai 文本 A/B）不补跑；spike 图不做复审模型交叉评分。
- 复审"任意版本展示/按建议整改重生成"两入口本轮未批准，不实施。
- 封面仍不纳入 AUTO 链路（§18.2 不变）；角色图 AI 生成/编辑能力不新增（§4.2 不变）。

## 3. 技术设计

- 端口：`CoverAnchorImageProvider`（独立 Protocol，`generate(*, prompt, references)`），既有 `CoverImageProvider.generate(*, prompt)` 签名不变，lingkeai 适配器与全部 fake 零改动。
- 适配器：`adapters/cover_image_grsai.py`（自包含：上传 token → 上传 → submit → 轮询 → 下载；全部错误映射为封面既有稳定码族 + 新增 `cover_image_provider_rejected`；任何失败都是 DomainError → 服务落 FAILED，Temporal 不重试，不产生重复提交；轮询预算 420 秒超时落 `cover_image_provider_timeout`）。
- QC：`CoverImageQc` 新增 `inspect_and_normalize(path, expected_sha256)`（Pillow）；文本路径仍走原 `inspect`。
- 服务：`CoverImageService` 构造新增可选 `anchor_provider` 与开关；`create` 时解析参考图（风格档案/项目草稿 → ref_image → 对象字节哈希）写入 canonical 与 parameters（`provider`、`reference_sha256`）；`generate` 按路由策略选通道；锚图路径走归一化 QC。
- 装配（`cover_runtime.py`）：grsai 复用故事板配置（`storyboard_provider_kind=="grsai"` 时用其 url/token），未配置则注入显式失败 stub。
- 迁移：无（parameters JSON 承载通道与参考图血缘）。

## 4. 测试（全部假 Provider/假 HTTP，零真实调用）

- 适配器：上传+提交+轮询+下载顺利路径；429/401/402/审核拒绝/超时/无任务 ID/下载失败的稳定码映射；轮询超时。
- 服务：锚图路由（有/无参考图、开关关、未配置稳定失败）、归一化（2752×1536 → 2560×1440、双哈希留痕、媒体完整性校验通过）、复审在归一化后执行、幂等与跨通道冲突、终态重放零重复调用。
- 领域：升 a 任务单文本断言更新；既有封面测试全绿。
- 回归：后端全量 pytest（单元+PG/MinIO 隔离集成）、ruff、mypy、npm run check。

## 5. 部署与验收

- 按 §21/§5：门禁核查无活动任务 → 构建镜像 → 两端 compose 改钉 → up -d（无迁移）→ 健康检查。
- 真实路径验收：用户页面手动「生成封面图」（或另行授权），验证锚图通道产物风格贴合参考图、复审记录随版本展示；逐次记录实际调用次数。
