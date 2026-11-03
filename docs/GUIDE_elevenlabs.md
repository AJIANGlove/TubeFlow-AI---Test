# ElevenLabs 配音功能实施指南

> 面向新会话实施者。本文档是实现级说明:模块→职责→关键判定矩阵→坑。
> 事故史细节不展开,指向 `PLAN_elv_dub_fixes.md`(2026-09 灭池)与
> `PLAN_elv_fixes_phase2.md`(2026-10 三连灭池/内容审核)。
> 文档编写基准:2026-10-12 代码状态。所有 key/账号均以脱敏形态出现。

---

## 1. 总览与模块索引

本项目用 ElevenLabs(下称 EL)做**角色配音(TTS)**:按「角色×语言」绑定 EL 音色,
配音阶段对绑定 EL 的角色逐行调 TTS,产物与 CosyVoice 路径完全同构(下游混音/合成无感知)。

| 模块 | 职责 |
|---|---|
| `GUI/elv_config.py` | key 池(Redis)+ 统一读取器 + 失败处置判定 + 配额读取 |
| `GUI/dubbing.py` | EL SDK 封装:合成/重试/时长护栏/并发闸/模型配置/IVC 克隆管理/音色库拉取 |
| `GUI/dub_elv_runner.py` | EL 直配分组配音器(逐行;主路径与 i18n 共用) |
| `GUI/dub_dispatch.py` | 按语言音色分发(EL vs CosyVoice batch_remote vs 未绑定) |
| `GUI/role_voice.py` | 角色音色绑定数据模型(主语言槽位/派生槽位/B 版/互斥) |
| `GUI/elv_voice_bind_check.py` | 绑定前音色可用性校验 + 公共库音色自动 add |
| `GUI/elv_voice_lib.py` + `GUI/elv_voice_panel.js` | 音色库本地缓存只读访问 + 面板选择器 |
| `GUI/_ops_fetch_elv_voice_library.py` | 音色库抓取脚本(源头在线验证剔除不可用音色) |
| `GUI/voice_admin.py` + 音色管理页 | IVC 克隆音色管理(克隆/删除/列表) |
| `GUI/main_pipeline/stage_runner.py` | 主路径编排:dub 阶段、断点跳过、号池熔断暂停 |
| `GUI/lang_pipeline/dub_step.py` / `station.py` | 派生语言(i18n)dub 阶段:行级 resume、坏行重配轮、推断式完成 |
| `GUI/elv_key_setup.html` | `/elv-key` 号池管理页 |

---

## 2. Key 池与配额(`GUI/elv_config.py`)

### 2.1 存储与读取优先级

- Redis hash `config:elevenlabs_key_pool`(field=kid,value=JSON `{key,label,added_at,
  status,status_at,last_error}`);事件 list `config:elevenlabs_key_events`(cap 50);
  存量单 key `config:elevenlabs_api_key`;env `ELEVENLABS_API_KEY`。
- `get_api_key()` 优先级:**池内首个 active(按 added_at 顺序)> Redis 单 key > env > 空**。
  进程内 5s 缓存防行级高频打 Redis;Redis 不可达静默回退(fail-open)。
- **兜底短路** `_fallback_key_blocked`:兜底值(单 key/env)与池内已标记
  (exhausted/invalid)key 相同 → 视为无 key(防死 key 兜底再发出去刷屏)。
  ⚠️ 遗留坑:该短路只挡"池内已标记"的 key;池清空重建后 env 里的历史死 key 不在池中
  就挡不住(2026-10-09 的 401 刷屏即此),正式修复待排期(见 PLAN_elv_fixes_phase2 §3.2)。

### 2.2 状态机

`active`(可用)/ `exhausted`(额度耗尽或欠费,充值/月度重置后页面「启用」复活)/
`invalid`(key 真失效)/ `disabled`(手动停用)。标记动作幂等,多进程并发安全
(无"当前指针",标记即天然完成切换)。

### 2.3 失败处置判定矩阵(核心,`classify_key_failure`)

**只有确认是"账号病"才标 key;请求级瞬态永不标 key。** 判定无副作用(401 会免费复核一次):

| 入参 | 判定 | 说明 |
|---|---|---|
| 402 | `exhausted` | 欠费,语义确定,不查订阅 |
| 401 + 复核亦拒(401/403) | `invalid` | key 真失效 |
| 401 + 复核可读 | `exhausted` | EL 额度耗尽(quota_exceeded)也返 401;订阅接口仍可读 = key 活着 |
| 401 + 复核不可达(网络/SDK) | `transient` | 宁可不标也不错标 |
| 403 / 409 / 5xx / 无状态码 | `transient` | 请求级:403 实证为内容审核(content_against_policy)/音色操作并发;409=音色增删并发(already_running) |

复核用 `verify_key()`(订阅接口 `user.subscription.get`,免费)。`rotate_on_failure`
接受 `_prechecked=` 传入已判定结果,避免二次复核;transient → 不标不切只记事件。

### 2.4 事件日志与配额观测

- 事件:rotate/transient 等写入 `config:elevenlabs_key_events`,同 key 同内容 60s 去重
  (防刷屏冲掉 cap 50 历史),`pool_events()` 读。
- 配额:`quota_snapshot()`(当前 key)/ `quota_of_key(kid)`(单成员,401 顺手标 invalid、
  用量≥上限顺手标 exhausted);返回 `character_count/character_limit/tier/
  voice_slots_used/voice_limit`。
- EL 套餐参考:Creator 字符 131,000/月、并发 5、音色槽位 30;Free 并发 2 等
  (以 EL 官网 Concurrency 表为准)。

---

## 3. 音色体系

### 3.1 音色可用性的第一性原理(2026-09 实证)

**音色可用 = 在该账户的音色列表里**(GET /v2/voices)。公共库音色未 add 进账户时
GET 400 / TTS 404。号池多 key 时同一音色须在**所有 active 账户**内(轮换后 404)。

### 3.2 音色库(面板选音色)

- 本地缓存 `models/elv_voice_library/index.json`(.gitignore 覆盖,不入库);
  `elv_voice_lib.py` 只读加载,**按文件 mtime 自动失效重载——替换文件免重启生效**。
- 抓取:`python GUI/_ops_fetch_elv_voice_library.py`(shared-voices 翻页+汉化);
  源头过滤:preview_url 指向 `api.us.elevenlabs.io`+`payload=` 的疑似私有分享(custom)
  条目,逐个免费 GET 验证,确认 400/404 才剔除(零误伤,曾有"custom 链接但公开可用"
  的反例 Lee)。存量清理用 `_ops_elv_voice_lib_prune.py`(同一判定,原子替换 index.json)。
- **私有分享音色(custom, payload 里 voice_source=custom + workspace_id)对其它账户
  永远不可用**,无法 add,只能换绑。

### 3.3 角色音色绑定(`GUI/role_voice.py`)

- 主语言(`MAIN_LANG="en"`)绑定写在角色顶层字段(voice_provider/voice_id 等);
  派生语言写在 `role["voice_bindings_by_lang"][lang]`;`effective_binding(role, lang)`
  派生无槽位时**回落主语言绑定**(source=fallback_main)。
- kind ∈ `public`(公共音色库 CosyVoice)/ `elevenlabs` / `local_ref`(本地参考音)。
  互斥:本地参考音与音色库绑定不可共存(write_binding 强制)。
- B 版绑定 `b_binding`(每角色全局一条,仅公共音色库,无双语言 fallback)。

### 3.4 绑定可用性校验 + 自动 add(`GUI/elv_voice_bind_check.py`)

绑定端点写盘前调 `ensure_voice_available(voice_id, name)`:
1. 号池全部 active key 逐个免费 GET `/v1/voices/{id}` —— 全可用 → 放行;
2. 有缺 → shared-voices 反查 `public_owner_id`(仅公共库可反查到);
3. 反查到 → 自动 `POST /v1/voices/add/{owner}/{id}` 到**全部缺失账户**并复核;
   槽位满/失败 → 明确原因拒绝(400 请换绑);
4. 反查不到(私有/克隆)→ 拒绝;校验本身不可达(无 key/网络/限流)→ **fail-open 放行**。
- 面板「仅显示当前账户可用」开关用 `account_voice_ids_cached`(60s 短缓存)。
- ops 同源:`_ops_elv_add_voices.py`(批量把项目所需音色 add 进指定账户)。

### 3.5 IVC 克隆音色管理

`dubbing.py: clone_voice / delete_voice / list_cloned_voices` + 音色管理页
(voice_admin)。注意:**克隆音色是账户私有**,换账户即 404(与 3.1 同一原理),
跨账户迁移只能重新克隆或改用库音色。

---

## 4. 配音引擎

### 4.1 按语言分发(`GUI/dub_dispatch.py`)

`provider_for_owner(owner, lang)`:kind=elevenlabs → EL 直配;public/local_ref →
batch_remote(自部署 CosyVoice,远端机器池);未绑定 → missing(缺 ref 语义)。
speaker 路径不分发(仍走 batch_remote ref_zip)。

### 4.2 EL 直配 runner(`GUI/dub_elv_runner.py` `run_elv_group`)

一组 = (集 × owner) 的若干行,逐行串行调 `dubbing.synthesize_with_duration_guard`:

- **文本口径**:`resolve_dub_text`(dub_text 优先,回退 translation)+ `speakable_text`
  (剥前导/尾随省略号);空 → 行标 skipped。
- **行级 resume skip**:done 且 mp3 存在的行不重配(Phase 1 修复;「重配选中」显式
  入参才覆盖重做)。
- **stitching 门控**:`previous_request_ids` 仅在模型支持时传
  (`dubbing.model_supports_stitching`;**eleven_v3 不支持,传了服务端 400**);
  换 key/失败/skip 断链重置。
- **行级失败处置**(Phase 2,见 §2.3 矩阵):401/402 复核确认 → 轮换 key 续跑;
  403/409/5xx/网络 → 行内退避重试(2s/5s/15s,≤3 次)不动号池;`content_against_policy`
  → 确定性直接标行不重试;404 voice_not_found → 标行不换 key;组内连续 ≥5 行瞬态耗尽
  → 终止本组(`group_halt`,不动号池);池耗尽 → 剩余行标 error,组结果带
  `pool_exhausted=True`(上层熔断:配音阶段暂停/终止,不再自动重跑)。
- **时长护栏**:`expected_max_dur_sec = max(60s, 原时长×6)`;超阈值疑似 v3 runaway →
  自动降级 FALLBACK 模型重发一次(v2 降级不传 stitching)。
- 落盘注入:主路径 `dub_batch_remote_runner._persist_row_result`;i18n 路径
  `lang_pipeline.dub_step._persist_i18n_row_result`;产物字段完全一致
  (dub_audio_rel/dub_status/dub_audio_duration/stretch_suggested/dub_request_id)。

### 4.3 模型与并发(`GUI/dubbing.py`)

- 模型:默认 `eleven_multilingual_v2`;env `ELEVENLABS_TTS_MODEL` 覆盖。
  **一致性注意**:已配成片若用 v3 配的,同项目重配时应临时切回 v3,避免同片音色混用。
  单请求字符上限:v3 5,000 / multilingual_v2 10,000 / flash 40,000。
- 重试:`synthesize_with_retry` 对 429/5xx/timeout 指数退避(首次+2 次);
  重试耗尽且当前模型 v3 → 剥 audio tags 降级 v2 再试一次。
- **并发闸** `_tts_global_sem`:默认 4(Creator=5 留 1 余量);`ELEVENLABS_TTS_CONCURRENCY`
  可覆盖(合法正整数)。⚠️ **信号量每进程独立**:多 dub worker/多机共享同一批账号时并发
  会叠加,须显式调低。
- 残次质检 `classify_dub_audio`(ffmpeg_util):静音/过短/失控 → 标 error 交重配轮。

### 4.4 流水线编排

- 主路径(`main_pipeline/stage_runner.py`):断点跳过双条件(集 ∈ completed_slugs +
  `_gt_dub` 文件复核);`run_dub_batch_remote` 汇总各组;`pool_exhausted` →
  `ElvPoolExhaustedError` → dub 阶段立即 paused(不消耗 content 轮次)。
- 派生语言(`lang_pipeline/`):station 每次按 workspace 推断已完成集
  (`io_helpers.infer_completed_slugs_per_stage`,全部行 done 即完成)整集跳过;
  未完成集第 1 轮配其全部待配行、后续轮只重配坏行(`_scan_bad_rows`,轮数
  `DUB_ROW_RETRY_ROUNDS`);flat 模式(LP_DUB_PLAN_CONCURRENCY>0)整语言拍平连续池。
  ⚠️ 遗留:lp dub 阶段记录在外部补配/推断完成后不会自动 reconcile 成 done
  (看板 pill 误钉「处理中」,2026-10-10 实证),存量用 `_ops_lp_dub_stage_reconcile.py`
  对账,正式修复待排期(PLAN_elv_fixes_phase2 §3.1)。

---

## 5. 观测与落证

- 行级日志:runner warning 带 `slug/row/HTTP 状态/body 截断 300 字`;worker console 日志
  里 httpx 逐条记录 EL 请求与状态码(排查的第一现场:`logs/worker-*-console.log`)。
- `dub_request_id` 回写到每行(EL 侧对账凭证);`dub_error` 含 `str(e)` 全量(含 body)。
- key 事件:`config:elevenlabs_key_events`(60s 去重);`last_error` 截断 500 字保 body。
- 号池页 `/elv-key`:配额条、状态、事件、启用/停用/删除/添加。

---

## 6. ops 工具链(`GUI/_ops_*`,均为独立进程、默认 dry-run、不动生产服务)

| 脚本 | 用途 | 用法要点 |
|---|---|---|
| `_ops_elv_redub.py` | 主语言配音补救(串行限速+熔断+v3 不传 stitching+集级 validate_dub 后写 completed_slugs) | `--apply/--only-slug/--limit-rows/--force`;自带预检与锁 |
| `_ops_elv_redub_i18n.py` | 派生语言配音补救(同策略;`--key-suffix` 定 key;403/409 行级瞬态不碰池;内容审核单列;403 误标 key 自动复活) | 同上;不写流水线状态(lp 推断跳过) |
| `_ops_elv_add_voices.py` | 把项目所需公共库音色 add 进指定号池账户 | `--apply`;反查 owner→add→复核 |
| `_ops_scan_bad_voice_bindings.py` | 全项目 EL 绑定排雷(逐账户免费 GET,出不可用清单) | 只读,随时重跑 |
| `_ops_elv_voice_lib_prune.py` | 音色库剔除当前账户不可用的私有分享音色 | dry-run→--apply 原子替换 |
| `_ops_lp_dub_stage_reconcile.py` | lp dub 阶段计数滞留对账(工位复核全达标才补写 done) | dry-run→--apply |

---

## 7. 坑与注意事项(现状级清单)

1. **音色可用性 = 账户列表成员**;换 key/换账户前先在目标账户 add 或重绑。
2. **私有分享(custom)音色跨账户永远 404**,且无法 add——只能换绑公共库音色。
3. **403 ≠ 账号死**:可能是内容审核(content_against_policy,音色×文本组合级,
   确定性)或音色操作并发瞬态;处置见 §2.3/§4.2——别标 key。违禁台词需改措辞或换音色。
4. **401 ≠ key 无效**:多为额度耗尽(quota_exceeded),订阅接口仍可读;充值/月度重置后
   页面「启用」复活。真无效是订阅接口也 401/403。
5. **v3 不支持 request stitching**(传了 400);v2/Flash 支持。
6. **并发上限按套餐**(Creator=5),且并发闸是每进程的——多 worker 部署叠加时要调低
   `ELEVENLABS_TTS_CONCURRENCY`。
7. **多 key 时同一音色要 add 进所有 active 账户**,否则轮换后 404。
8. 遗留待修(见 PLAN_elv_fixes_phase2 §3):池空 env 兜底可能发出历史死 key;
   lp dub 阶段记录计数滞留需 reconcile。
9. EL 请求级字符上限注意超长行(v2 10,000/请求);runner 单行不会超限,无需处理。

---

## 8. 其它项目接入 EL 配音的最小步骤(接入检查清单)

1. **号池**:`/elv-key` 页加 key(或 .env `ELEVENLABS_API_KEY` 兜底);点「刷新配额」
   确认 tier/余量;多 key 注意全部 active 账户都要装音色(见第 3 步)。
2. **音色绑定**:角色管理 → 选 EL 音色库音色绑定(主语言/派生语言槽位);
   绑定时校验+自动 add 已内置(§3.4);绑定私有音色会被拒——换公共库音色。
3. **配音**:一键全流程或配音阶段自动按绑定分发到 EL;失败看 `dub_error` 与
  `logs/worker-*-console.log`。
4. **对账**:配额条 + 事件页 + `dub_request_id`;需要补配用 `_ops_elv_redub*.py`
   (先 dry-run 核计划,再冒烟 1 集,再全量)。

---

## 9. 新系统（tubeflow_test）v4 切换合同（2026-10-26，USER_DECISIONS §35）

本节记录新后端（`backend/src/tubeflow_test/`）的现行 EL 合同,与上文旧系统（GUI/*）描述区分。

1. **模型**:`eleven_v4`（env `TUBEFLOW_ELEVENLABS_TTS_MODEL`）。v4 支持
   `previous_request_ids` 拼接链（≤3 条,官方 stitching 指南示例即用 v4;v3 不支持）;
   单请求 10,000 字符（同 v2）;计费与 v2 相同（$0.08/千字符）。
2. **voice_settings**（env 三元组,全空则**不发送该字段**、EL 按音色存储默认值合成）:
   - `TUBEFLOW_ELEVENLABS_STABILITY` 0–1 连续（默认 0.5,越低情感幅度越大）
   - `TUBEFLOW_ELEVENLABS_SIMILARITY_BOOST` 0–1 连续（默认 0.75）
   - `TUBEFLOW_ELEVENLABS_USE_SPEAKER_BOOST` bool（默认 true;IVC 适用,延迟略增）
   - 现行生产值:**1 / 1 / true**（用户 2026-10-26 部署前指定「相似度和创造力都设 1」;此前三档试听曾选 0.5/0.5,同日改定 1/1。实测参数对语速无影响,仅改演绎质感）。
3. **v4 不支持**:`speed`/`style` 滑块、SSML break 标签。语速由模型演绎决定,
   不能用 speed 参数调节（用户实测判断其致语速/音色异常,已否决）。
4. **`clone_strength` 不发送**:仅适用于 v4 微调的专业克隆（PVC）;本系统均为 IVC。
5. **audio tags**:v4 支持但当前**不使用**（用户选定裸跑路线;导演标注设想已作废,
   如未来需要另行立项）。
6. **一致性风险（知情接受）**:全局切换不做项目级模型冻结;既有项目手动重配单句会
   混入 v4（与 v2 旧句音色可闻不一致）。既有项目存量不动、不重配。
7. **回退**:`.env` 清空三个 voice_settings 变量 + `TUBEFLOW_ELEVENLABS_TTS_MODEL`
   改回 `eleven_multilingual_v2` + 回滚镜像/compose,即恢复原行为（未配置时
   适配器不发送 voice_settings,v2 路径逐字节等价于切换前）。
8. 实证数据:`runtime/operations/voice_ab_test_20261026/`（v2/v4 裸跑/v4 标签/
   参考原声对照）与 `runtime/operations/voice_v4_param_sweep_20261026/`（三档扫描）。
