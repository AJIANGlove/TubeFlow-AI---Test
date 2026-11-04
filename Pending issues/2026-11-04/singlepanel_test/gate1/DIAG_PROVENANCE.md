# Gate 1 diagnostics (crop-grid-v2) — provenance

Read-only diagnostics for the 109 Gate 1 failures recorded in
`Pending issues/2026-11-04/singlepanel_test/gate1/gate1_results.jsonl`
(Gate 1 verdict: 406 passed / 515 = 78.835%, threshold 90% → FAIL).

No deployment, no provider call, no code/threshold/test change. The diagnostics only
re-read the storyboard objects already stored in MinIO.

## Script

| item | value |
| --- | --- |
| source branch | `claude/gate1-diagnostics`, head `c859079261eaf9288ea6ab15d0757e84875cfa8a` |
| file | `scripts/gate1_diagnose.py` (checked out from that branch, unmodified) |
| blob | `5996370259aae5337d691754bb8b03f0d1e33098` (local `git hash-object` == branch blob) |
| checks helper | `scripts/gate1_diag_check.py`, blob `74a3d2ebb1710d84273d84c031c577cd41f72437` (written for this run) |

## Environment

| item | value |
| --- | --- |
| host | `edy@192.168.110.200`, work dir `/home/edy/pr4-check/gate1-diag` |
| image | `tubeflow-test:single-panel-v2-20261104`, id `908d98f6f3ac`, built 2026-11-04 15:29:51 +0800 |
| code check | image carries PR #4 fix `2a0b33f`: `panel_images.py:557` = `return float(array("f", region.tobytes())[0])`; `_V2_INSET=3`, `_V2_RADIUS_MIN=16` |
| network | `tubeflowai-test_default` |
| env | `--env-file /home/edy/pr4-check/minio.env` (4 keys: `TUBEFLOW_MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET`) |

## Command

```
docker run --rm --name gate1diag \
  --network tubeflowai-test_default \
  --env-file /home/edy/pr4-check/minio.env \
  -v /home/edy/pr4-check/gate1-diag:/work -w /work \
  tubeflow-test:single-panel-v2-20261104 \
  python gate1_diagnose.py --results gate1_results.jsonl --out diag --passed-sample 36 --originals 15
```

Runtime: started 16:08, all 145 generations traced and written within the same minute
(no failure, container exited by itself). Console output = `diag_run.log`.

## Output inventory

| path | files | bytes |
| --- | --- | --- |
| `diag/trace.jsonl` | 1 | 103,470 |
| `diag/previews/` | 145 | 15,534,408 |
| `diag/sheets/` | 14 | 14,150,169 |
| `diag/originals/` | 15 | 38,522,896 |
| `diag/checks.json` | 1 | 3,839 |
| total | 176 | 68,314,782 |

All 174 JPEGs decode with `PIL.Image.verify()`. Preview dims: 836×470 ×132, 1376×768 ×12,
835×470 ×1 (half of the source storyboard). Originals: 1672×941 ×12, 2752×1536 ×1,
1672×940 ×1, 1671×941 ×1. Sheets: 2508 px wide, 3 columns, 12 previews per full page.

Sheet pages (14): `failed_shot_panel_gutter_ambiguous_01`, `failed_shot_panel_gutter_not_found_01..06`,
`failed_shot_panel_layout_mismatch_01..04`, `passed_sample_01..03`.

## Consistency checks (from `diag/checks.json`)

- `trace.jsonl` rows 145 = 109 `failed` + 36 `passed` sample.
- Every one of the 109 Gate 1 failed rows is present; no duplicate and no unknown `generation_id`;
  no passed row outside the fixed sample (`random.Random(46).sample(passed, 36)`).
- `gate1_status → status`: `failed→failed` 109, `passed→passed` 36 — 0 mismatches.
- `gate1_failure_code → failure.code`: 42/42 `shot_panel_layout_mismatch`, 66/66
  `shot_panel_gutter_not_found`, 1/1 `shot_panel_gutter_ambiguous` — 0 mismatches, and the
  independent cross-check against the original Gate 1 row's `failure_code` is also 0 mismatches.
- `source_bytes_sha_mismatch`: 0 records (all 515 Gate 1 rows carry `expected_sha256`; every
  fetched object matched).
- Every traced id has a preview; every preview/original maps back to a traced id; all 15
  originals are failed rows.
