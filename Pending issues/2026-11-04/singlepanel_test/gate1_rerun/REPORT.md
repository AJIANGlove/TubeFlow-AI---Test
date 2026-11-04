# Gate 1 rerun — crop-grid-v2 after USER_DECISIONS §47 (2026-11-04)

Read-only offline crop dry run. No project created, no image/video/LLM provider call:
every record re-reads the storyboard bytes already stored in MinIO.

## 1. Population (frozen)

`status='SUCCEEDED'` **and** `input_json->>'prompt'` contains
`Do not draw any readable text anywhere in the image` (the rule added in `d5c1a12`).
Query: `baseline.sql` in this directory; result `baseline.json`
(`captured_at` 2026-11-04T09:29:11Z, `total` 548, 10 keys per row, same shape as the
first-round baseline).

| project | title | storyboards |
| --- | --- | ---: |
| `d725172f-bffa-4795-9554-8761a197fa08` | The Wind That Decides Whether India Eats | 152 |
| `765dca11-b2fe-4e48-bc3d-9c0695f91263` | How Did Sailors Survive Scurvy and Storms at Sea | 147 |
| `a0e52ea0-0ef7-4a95-9c76-d87c947c38a0` | 조선시대 기생이라면, 어디까지 올라갈 수 있을까? | 122 |
| `146ae08c-8412-49de-acd6-6ddaae2eb201` | Egypt Is Just a River With a Flag | 112 |
| `fa3a0b30-9222-4ca4-a475-3bfd6cc57220` | 사장님 없는 무인 아이스크림 가게, 정말 돈이 될까? | 13 |
| `31e00d87-5332-4b60-88e9-8b6dd8d475e7` | “과일에 설탕만 입히면 된다더니…” 탕후루 가게의 숨은 인건비 | 2 |
| **total** | | **548** |

Differences from the first round (515): `17d2b9e9` (89 sheets, 2026-08-31, pre-`d5c1a12`)
is out of scope by the predicate; `a0e52ea0` (122 sheets generated 15:33–15:56 on
2026-11-04, after the first baseline was taken) is in scope.
Re-checked at 17:37:55: still 548, `added_since_freeze = 0` — no drift during the run.

## 2. Run

| item | value |
| --- | --- |
| image | `tubeflow-test:crop-grid-v2-rev-20261104`, id `238bd9ead5f3`, built 2026-11-04 17:32 |
| code | merged main `1707d85`; in-image `panel_images.py` LF-normalised sha256 `5b6cfa16…` == git blob of `HEAD:backend/src/.../panel_images.py`; `_V2_WIDE_FRACTION=0.25`, `_V2_MIN_CELL_FRACTION=0.6`, `_V2_FRAMED_MAX=48`, `_V2_WHITE_MIN=240.0`; `_v2_wide_separator` present, `_assert_no_interior_gutter` absent |
| command | `python gate1_crop_dryrun.py --baseline baseline.json --out gate1_results.jsonl` |
| env | `--network tubeflowai-test_default --env-file /home/edy/pr4-check/minio.env` (4 MinIO keys) |
| derivation | `derive_shot_panels(..., derivation={"version": "crop-grid-v2"}, layout_version="grid-v1", aspect_ratio="16:9")` |
| log | `gate1_run.log` |

## 3. Result

| total | passed | failed | pass rate | threshold | verdict |
| ---: | ---: | ---: | ---: | ---: | --- |
| 548 | 539 | 9 | **98.3577%** | ≥ 90% | **PASS** |

Failure codes: `shot_panel_gutter_not_found` 9 (only code). No `shot_panel_layout_mismatch`
(that check was removed by §47), no `shot_panel_crop_unsafe`, no `shot_panel_source_invalid`,
no `source_bytes_sha_mismatch`.

By project:

| project | total | passed | failed | rate |
| --- | ---: | ---: | ---: | ---: |
| `765dca11` | 147 | 147 | 0 | 100% |
| `d725172f` | 152 | 152 | 0 | 100% |
| `fa3a0b30` | 13 | 13 | 0 | 100% |
| `31e00d87` | 2 | 2 | 0 | 100% |
| `146ae08c` | 112 | 108 | 4 | 96.43% |
| `a0e52ea0` | 122 | 117 | 5 | 95.90% |

By shot count:

| shots | total | passed | failed |
| ---: | ---: | ---: | ---: |
| 1 | 33 | 33 | 0 |
| 2 | 129 | 129 | 0 |
| 3 | 162 | 159 | 3 |
| 4 | 123 | 118 | 5 |
| 5 | 62 | 61 | 1 |
| 6 | 39 | 39 | 0 |

The 9 failures (all fail in the widened ±25% cell search, i.e. no gutter band that runs
the whole length was found):

| generation | project | shots | axis |
| --- | --- | ---: | --- |
| `57f29042…` | `146ae08c` | 5 | x |
| `99e738ce…` | `146ae08c` | 4 | x |
| `b13bc354…` | `146ae08c` | 4 | x |
| `dd83c6f5…` | `146ae08c` | 3 | x |
| `f051c1b8…` | `a0e52ea0` | 4 | y |
| `110d018d…` | `a0e52ea0` | 3 | x |
| `05dbf56a…` | `a0e52ea0` | 4 | x |
| `0886fd57…` | `a0e52ea0` | 4 | y |
| `2ae249f2…` | `a0e52ea0` | 3 | x |

`99e738ce` and `57f29042` are the two sheets classified in
`CROP_GRID_V2_GATE1_REVIEW_20261104.md` §2 as "not the requested layout" (correctly rejected).

## 4. Diagnostics

`python gate1_diagnose.py --results gate1_results.jsonl --out diag --passed-sample 36 --originals 15`
(36 of 539 passed sampled with `random.Random(46)`; originals capped by the 9 failures, so 9
files instead of 15).

| check | value |
| --- | --- |
| `diag/trace.jsonl` rows | 45 = 9 failed + 36 passed sample |
| trace `status` == Gate 1 `status` | 45/45, **0 mismatches** |
| trace `failure.code` == Gate 1 `failure_code` | 9/9, 0 mismatches (cross-checked against the raw result row as well) |
| `source_bytes_sha_mismatch` | 0 (548/548 rows carry `expected_sha256`) |
| previews / sheets / originals | 45 / 4 / 9 files |
| diag bytes | 36,418,369 (59 files) + `checks.json` |
| sheet pages | `failed_shot_panel_gutter_not_found_01`, `passed_sample_01..03` |

## 5. Scope

Gate 1 only. No deploy, no switch change, no project created, no provider call, `d725172f`
untouched. The visual review of the 36 passed samples belongs to the reviewer
(§43 asks for it separately).
