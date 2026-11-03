# 多格故事板同框：存量普查报告（2026-10-20）

## 0. 结论

- 覆盖全部 **1384 个当前组视频 / 17 个项目**（只读，未改任何数据）。
- `leak` **144 组（10.4%）**，`suspect` 88 组，`clear` 1152 组；16/17 个项目都有命中。
- 判据（v2）：任一帧同时命中 1/3 与 2/3 竖线，或 ≥2 帧命中稳定格线，或 1 帧格线 + ≥2 帧文字带 → `leak`；仅 1 帧格线或仅文字带 → `suspect`（人工复核，不自动拦截）。
- 人工抽检：5 个 `leak` 全部属实（3 格并排 / 2 格分屏 / 2×2 四分格）；1 个 `suspect` 是剧情内标题文字，属误报，未升级为 leak。

## 1. 分项目结果

| 项目 | 组数 | leak | 占比 | suspect |
| --- | --- | --- | --- | --- |
| 새벽 3시에 머리를? 24시간 미용실이 야간에 대박 나는 이유 `8df22ac8` | 78 | 21 | 26.9% | 9 |
| 한국 포차의 숨겨진 경제학 `a5367080` | 133 | 16 | 12.0% | 2 |
| 야채 가게 장사 뒤에 숨은 진짜 비밀 `5c6fce56` | 121 | 16 | 13.2% | 5 |
| When Did Vikings Start Preserving Fish b `aec5d588` | 84 | 15 | 17.9% | 7 |
| 재료비만 80%? 붕어빵을 팔수록 손해 보는 진짜 이유 `1142cd90` | 85 | 14 | 16.5% | 6 |
| When Did Vikings Start Preserving Fish b `ca6f47d6` | 87 | 13 | 14.9% | 5 |
| How Did Ancient Women Deal With Pregnanc `1a1056b7` | 109 | 10 | 9.2% | 6 |
| When Did Ancient Humans Start Hiding in  `3206fa93` | 91 | 9 | 9.9% | 2 |
| The Psychology of Cats Turning Away When `b497a6b5` | 50 | 5 | 10.0% | 2 |
| When Did Ancient Humans Start Hiding in  `17d2b9e9` | 88 | 5 | 5.7% | 17 |
| The Bizarre Marriage Rituals of the Nors `608cf263` | 87 | 5 | 5.8% | 4 |
| What Did Vikings Actually Do for Fun on  `3ce9a585` | 102 | 4 | 3.9% | 12 |
| POV: 1998년, 당신은 금반지를 내놓고 가족의 미래를 지킬 수 있을 `d43cee7a` | 54 | 4 | 7.4% | 0 |
| When Did Ancient Humans Start Hiding in  `d80b2be7` | 91 | 3 | 3.3% | 6 |
| 세대별 '부자'의 기준 — 쌀 열 가마에서 강남 아파트까지 `2333d455` | 67 | 2 | 3.0% | 3 |
| POV: 1997년 대기업 사원인 당신, 어느 날 구조조정 명단에서 내  `f2125c75` | 55 | 2 | 3.6% | 2 |
| H3 正式节点验收 — 洞穴避雨与合作 — 20260918 `456c0fad` | 2 | 0 | 0.0% | 0 |

## 2. 建议重跑清单

- 共 **144 组**：`runtime/operations/multi_panel_leak_20261020/inventory_20261020/rerun_recommended.csv`（含项目、组号、attempt、命中秒、格线位置）。
- 按项目：When Did Vikings Start Pre **28** 组；새벽 3시에 머리를? 24시간 미용실이 야간에  **21** 组；한국 포차의 숨겨진 경제학 **16** 组；야채 가게 장사 뒤에 숨은 진짜 비밀 **16** 组；When Did Ancient Humans St **14** 组；재료비만 80%? 붕어빵을 팔수록 손해 보는 진 **14** 组；How Did Ancient Women Deal **10** 组；The Psychology of Cats Tur **5** 组；The Bizarre Marriage Ritua **5** 组；What Did Vikings Actually  **4** 组；POV: 1998년, 당신은 금반지를 내놓고 가 **4** 组；When Did Ancient Humans St **3** 组；세대별 '부자'의 기준 — 쌀 열 가마에서 강남 **2** 组；POV: 1997년 대기업 사원인 당신, 어느  **2** 组
- 不含 `suspect` 组（需人工确认）与历史 attempt。
- 成本：按 HD 12.5–19.5 分钟/组，144 组约 **30–47 引擎小时**；三台并行约 10–16 小时墙钟（不含重试与失败重跑）。

## 2bis. 版式分布（决定修复方向）

- 2 格（1/2）：**86** 组
- 仅单帧或文字：**24** 组
- 3 格（1/3+2/3）：**24** 组
- 水平分割/四分格：**10** 组

2 格分屏占多数，说明「把整张多格图当一帧」与格子数量无关；
即使把 3 格改成 2 格、或去掉信息图那一格，仍会分屏。

## 3. 方法与局限

- 经 MinIO **S3 API** 读取每个组当前版本的原始视频 → ffmpeg 1fps 解码 → 等距取 8 帧（>12 秒取 10 帧）。
- 分辨率无关：1056×608 与 1600×896 共用同一组归一化阈值；归档失败样本两种分辨率都命中。
- 已知漏检：格线被人物遮挡、<1 秒闪现场景；已知误报：字幕/标题文字（只进 `suspect`）。
- 只覆盖当前版本；历史 attempt 与已删除对象未覆盖。

## 4. 复现

```
# 部署机 web 容器内（只读）
python /tmp/mpl_survey.py /tmp/mpl_groups.csv 2000 3
```
