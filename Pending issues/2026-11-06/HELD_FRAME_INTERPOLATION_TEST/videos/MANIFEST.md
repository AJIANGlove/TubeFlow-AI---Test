# B 的并排视频清单（不入库，只记哈希与位置）

仓库 `.gitignore:41` 有 `*.mp4`，因此这些视频**没有**被提交；本地与部署机各留一份，内容一致（MD5 不适用，用 sha256 核对）。

| 文件 | 字节 | sha256 |
| --- | ---: | --- |
| `03_90c4a4e7_sbs.mp4` | 2,760,083 | `97bdb13014c1406171e6e9fc1aad7f93021aded6f6872a6720242b035d812eb4` |
| `07_7460885c_sbs.mp4` | 4,153,470 | `180257f491538f5b29fb0c2e7d8ff7cb5dce12790e3fa979633ca3ab412bbc50` |
| `07_fc6e97b8_sbs.mp4` | 4,164,415 | `3fbbefdfa07acf7a6c9552920415a8d60dd5982647f55376e4a409085f15ac93` |
| `07_fc6e97b8_sbs_slow.mp4` | 5,114,057 | `cb3764e1d4f9364d7bd87eb8e83de872b8832a2f13fd9f0f4a9bb1084f72d9b0` |
| `129_8f749d4e_sbs.mp4` | 7,082,441 | `96774ebff4c1da3ad8bfc7bbedb0c98ac5cae2e57e50d98cef4c8a8f32875164` |
| `12_0cc7c89c_sbs.mp4` | 3,619,388 | `0abe83fdcf7efbce16eced72ea9701eec9a3b4fdf90cdde99a7e4092201109d4` |
| `12_3ca4653d_sbs.mp4` | 4,524,806 | `749bc941d6b5dfb2a2cc533d44aec8cb436e6cc6e0747e062c1f28169a5e5dbd` |
| `12_3ca4653d_sbs_slow.mp4` | 6,103,870 | `16afb1c772c136c80de6ea8b8f5742e9137177a82905cf574536762ddb396395` |

合计 37,521,530 B ≈ 35.8 MiB。

- 本机：`Pending issues/2026-11-06/HELD_FRAME_INTERPOLATION_TEST/videos/`
- 部署机：`/home/edy/held_frame_20261106/out/`（含三臂中间文件 `*_B0/B1/B2.mp4` 与样片 `samples/`）
- 画面顺序：左 B0（原片重编码）| 中 B1（mpdecimate+minterpolate mci）| 右 B2（mpdecimate+minterpolate blend）；各缩至 704 宽后 hstack。
