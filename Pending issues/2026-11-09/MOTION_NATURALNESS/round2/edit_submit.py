#!/usr/bin/env python3
"""§54 画面运动自然度：在 web 容器内提交**一步** edited_v1 手动 H3 尝试（只调用 create()）。

沿用 2026-11-06 §51 的容器内提交方式（Pending issues/2026-11-06/evidence/jitter_ab/submit/
submit_step.py），差别：

  a) 只提交 edited_v1 臂：整段已编辑提示词 + sha256 + 变体标签，来源尝试默认取该组当前视频。
  b) 构造 ManualH3IntentWriter 时 prompt_compiler=None：本脚本**不可能**调用 DeepSeek
     （edited_v1 不编译；任何走到编译的路径都会以 video_prompt_compiler_not_configured 拒绝）。
  c) --dry-run：在**回滚的**会话里做与 create() 相同的只读预取（for_intent=False，面板只在
     内存派生），再按 v4 单格合同全量校验提示词；不创建尝试、不调用任何 Provider。
  d) 一次运行只提交一步：没有循环，没有自动重试；同组有未终结尝试即拒绝。
  e) 提交前探活 /group-videos/contract；提交后只读回读该尝试与该组当前视频指针。

用法（容器内 /app）：

  # 第 1 步（v1 包）：用来源尝试的原文做只读演练（R0），不提交
  python edit_submit.py --project-id <uuid> --group-seq 116 --seed source --source-text \
      --variant-label r0 --idempotency-key motion-r2-g116-r0-dry --step-label g116-r0-dry --dry-run

  # 第 2 轮（v2 包）：提交一个已编辑变体
  python edit_submit.py --project-id <uuid> --group-seq 116 --seed 123 \
      --edited-prompt-file prompts/g116_m1.txt --expected-sha256 <hex> \
      --variant-label m1 --idempotency-key motion-r2-g116-m1-s1 --step-label g116-m1-s1

提示词文件按 UTF-8 原样读取（不去首尾空白、不转换换行）；sha256 不符即拒绝。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SEED_MAX = 2**53 - 1
MAX_PROMPT_CHARS = 20000
LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ACTIVE_STAGES = (
    "CREATED",
    "NOT_SUBMITTED",
    "STAGING",
    "SUBMITTING",
    "REMOTE_STATE_UNKNOWN",
    "SUBMITTED",
    "RUNNING",
)


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", flush=True)


def fail(message: str, code: int = 2) -> None:
    log(f"REFUSED: {message}")
    raise SystemExit(code)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_args(args: argparse.Namespace) -> None:
    """复刻 api.py::ManualH3Input 对 edited_v1 的校验，外加本脚本自己的组合约束。"""
    if (args.group_seq is None) == (args.group_id is None):
        fail("give exactly one of --group-seq / --group-id")
    if LABEL.fullmatch(args.variant_label) is None:
        fail(f"variant label must match {LABEL.pattern}: {args.variant_label!r}")
    if args.seed != "source":
        try:
            seed = int(args.seed)
        except ValueError:
            fail(f"--seed must be an integer or 'source': {args.seed!r}")
        if not 0 <= seed <= SEED_MAX:
            fail(f"seed out of range [0, {SEED_MAX}]: {seed}")
    if args.source_text == (args.edited_prompt_file is not None):
        fail("give exactly one of --source-text / --edited-prompt-file")
    if args.edited_prompt_file is not None:
        if args.expected_sha256 is None or SHA256.fullmatch(args.expected_sha256) is None:
            fail("--edited-prompt-file requires --expected-sha256 (64 lowercase hex)")
    elif args.expected_sha256 is not None and SHA256.fullmatch(args.expected_sha256) is None:
        fail("--expected-sha256 must be 64 lowercase hex")


def load_edited_prompt(args: argparse.Namespace, source_text: str) -> str:
    if args.source_text:
        text = source_text
    else:
        raw = Path(args.edited_prompt_file).read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            fail(f"edited prompt file is not UTF-8: {error}")
    if not 1 <= len(text) <= MAX_PROMPT_CHARS:
        fail(f"edited prompt length {len(text)} outside [1, {MAX_PROMPT_CHARS}]")
    if "\r" in text:
        fail("edited prompt contains a carriage return (file was converted to CRLF?)")
    digest = sha256_text(text)
    log(f"edited prompt: {len(text)} chars sha256={digest}")
    if args.expected_sha256 is not None and digest != args.expected_sha256:
        fail(f"sha256 mismatch: file {digest} != expected {args.expected_sha256}")
    return text


def prompt_diff(before: str, after: str, limit: int = 120) -> list[str]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), "source", "edited", lineterm="", n=0
        )
    )
    extra = [f"... ({len(lines) - limit} more diff lines)"] if len(lines) > limit else []
    return lines[:limit] + extra


def check_contract(url: str) -> None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - 容器内 localhost
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        fail(f"contract probe returned HTTP {error.code}: {body}")
    except Exception as error:  # noqa: BLE001 - 探活失败一律拒绝
        fail(f"contract probe failed: {error!r}")
    if status != 200:
        fail(f"contract probe returned HTTP {status}: {body}")
    try:
        contract = json.loads(body)
    except json.JSONDecodeError:
        fail(f"contract probe returned non-JSON: {body[:400]}")
    log(f"contract: {json.dumps(contract, ensure_ascii=False, sort_keys=True)}")
    if not isinstance(contract, dict) or contract.get("available") is not True:
        fail("contract is not available")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="§54 edited_v1: submit exactly one manual H3 step")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--group-seq", type=int, default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument(
        "--prompt-source-attempt-id",
        default=None,
        help="默认取该组 group_current_videos 的当前尝试",
    )
    parser.add_argument("--seed", required=True, help="整数，或 source（沿用来源尝试的 seed）")
    parser.add_argument("--source-text", action="store_true", help="用来源尝试的原文（R0）")
    parser.add_argument("--edited-prompt-file", default=None)
    parser.add_argument("--expected-sha256", default=None)
    parser.add_argument("--variant-label", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--step-label", required=True)
    parser.add_argument("--contract-url", default="http://localhost:8000/group-videos/contract")
    parser.add_argument("--dry-run", action="store_true", help="只读演练，不创建尝试")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:  # noqa: C901 - 一步一流程，按顺序写出更易审
    args = parse_args(argv)
    validate_args(args)
    log(f"step={args.step_label} key={args.idempotency_key} dry_run={args.dry_run}")

    # 生产装配件只在容器内可用；放在这里导入，便于在别处做参数校验。
    from minio import Minio
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session, sessionmaker

    from tubeflow_test.adapters.object_storage import PrivateObjectStorage
    from tubeflow_test.adapters.persistence.database import Database
    from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
    from tubeflow_test.adapters.persistence.narration import SqlStoryboardNarrationAuthority
    from tubeflow_test.adapters.persistence.production_authority import (
        canonical_video_authorities,
        single_panel_policy_settings,
    )
    from tubeflow_test.adapters.persistence.shot_panel_assets import ShotPanelAssetService
    from tubeflow_test.adapters.persistence.storyboards.repository import (
        SqlAlchemyStoryboardRepository,
    )
    from tubeflow_test.config import Settings
    from tubeflow_test.video_generation.adapters import build_h3_archive_adapter
    from tubeflow_test.video_generation.persistence import ManualH3IntentWriter

    check_contract(args.contract_url)

    settings = Settings()
    database = Database(settings.database_url)
    storage = PrivateObjectStorage(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        ),
        settings.minio_bucket,
    )
    sessions = sessionmaker(database.engine, expire_on_commit=False)
    project_id = UUID(args.project_id)

    def read(sql: str, **params: object) -> list[dict[str, Any]]:
        with sessions() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            rows = session.execute(text(sql), params).mappings().all()
            session.rollback()
        return [dict(row) for row in rows]

    with sessions() as session:
        project = session.execute(
            select(ProjectRow.owner_id, ProjectRow.production_state).where(
                ProjectRow.id == project_id
            )
        ).one_or_none()
        session.rollback()
    if project is None:
        fail(f"project not found: {project_id}")
    owner_id, production_state = project
    log(f"owner_id (from projects): {owner_id}; production_state={production_state}")
    if production_state != "ACTIVE" and not args.dry_run:
        fail(f"project production_state is {production_state}, not ACTIVE")

    if args.group_seq is not None:
        rows = read(
            "select id::text as id, sequence from authoritative_narration_groups "
            "where project_id = cast(:p as uuid) and sequence = :s",
            p=str(project_id),
            s=args.group_seq,
        )
        if len(rows) != 1:
            fail(f"group sequence {args.group_seq} matched {len(rows)} groups")
        group_id = UUID(rows[0]["id"])
    else:
        group_id = UUID(args.group_id)
    pointers = read(
        "select g.sequence, sc.generation_id::text as storyboard_id, "
        "c.attempt_id::text as current_attempt_id "
        "from authoritative_narration_groups g "
        "left join storyboard_current sc on sc.group_id = g.id "
        "left join group_current_videos c on c.group_id = g.id "
        "where g.id = cast(:g as uuid) and g.project_id = cast(:p as uuid)",
        g=str(group_id),
        p=str(project_id),
    )
    if len(pointers) != 1 or pointers[0]["storyboard_id"] is None:
        fail(f"group {group_id} has no current storyboard in project {project_id}: {pointers}")
    storyboard_id = UUID(pointers[0]["storyboard_id"])
    log(f"group: seq={pointers[0]['sequence']} id={group_id} storyboard={storyboard_id}")
    log(f"current video before: {pointers[0]['current_attempt_id']}")

    source_attempt = args.prompt_source_attempt_id or pointers[0]["current_attempt_id"]
    if source_attempt is None:
        fail("no --prompt-source-attempt-id and the group has no current video")
    source_rows = read(
        "select attempt_id::text as attempt_id, group_id::text as group_id, "
        "generation_id::text as generation_id, stage, "
        "input_snapshot_json -> 'video_prompt' ->> 'strategy' as strategy, "
        "input_snapshot_json -> 'video_prompt' ->> 'model' as model, "
        "input_snapshot_json -> 'video_prompt' ->> 'text' as prompt_text, "
        "input_snapshot_json -> 'experiment' as experiment, "
        "request_json -> 'parameters' -> 'seed' as request_seed "
        "from video_generation_attempts where attempt_id = cast(:a as uuid)",
        a=str(source_attempt),
    )
    if len(source_rows) != 1:
        fail(f"prompt source attempt not found: {source_attempt}")
    source = source_rows[0]
    log(
        "prompt source: "
        + json.dumps(
            {key: value for key, value in source.items() if key != "prompt_text"},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )
    source_text = source["prompt_text"]
    if not isinstance(source_text, str):
        fail("prompt source has no video_prompt.text")
    log(f"source prompt: {len(source_text)} chars sha256={sha256_text(source_text)}")
    if args.seed == "source":
        if not isinstance(source["request_seed"], int):
            fail(f"source seed is not an integer: {source['request_seed']!r}")
        seed = int(source["request_seed"])
    else:
        seed = int(args.seed)
    log(f"seed: {seed}")

    edited = load_edited_prompt(args, source_text)
    digest = sha256_text(edited)
    diff = prompt_diff(source_text, edited)
    log(f"diff vs source: {len(diff)} lines" + ("" if diff else " (identical text)"))
    for line in diff:
        print("DIFF " + line, flush=True)

    active = read(
        "select attempt_id::text as attempt_id, stage, idempotency_key "
        "from video_generation_attempts where group_id = cast(:g as uuid) and stage in ("
        + ",".join(f"'{stage}'" for stage in ACTIVE_STAGES)
        + ")",
        g=str(group_id),
    )
    log(f"active attempts on group: {json.dumps(active, ensure_ascii=False)}")
    if active:
        fail("group already has a non-terminal attempt")

    video_asset_store = build_h3_archive_adapter(sessions, storage)
    requested_policy, policy_allowed = single_panel_policy_settings(settings)
    _narration, _storyboard, loader = canonical_video_authorities(
        settings,
        reference_policy=requested_policy,
        single_panel_allowed=policy_allowed,
        panel_derivatives=ShotPanelAssetService(sessions, storage, video_asset_store),
    )
    storyboard_repository = SqlAlchemyStoryboardRepository(
        database.unit_of_work, SqlStoryboardNarrationAuthority(database.session, storage)
    )

    if args.dry_run:

        @contextmanager
        def rollback_uow() -> Iterator[Session]:
            session = sessions()
            try:
                yield session
            finally:
                session.rollback()
                session.close()

        writer = ManualH3IntentWriter(
            rollback_uow,
            storyboard_repository,
            loader,
            prompt_compiler=None,
            intent_group_loader=loader,
        )
        with rollback_uow() as session:
            preview = loader(session, owner_id, group_id)
        if preview.storyboard_generation_id != storyboard_id:
            fail(f"preview storyboard {preview.storyboard_generation_id} != {storyboard_id}")
        policy = preview.reference_policy
        log(
            "preview: reference_policy="
            + json.dumps(
                None
                if policy is None
                else {"strategy": policy.strategy.value, "derivation": policy.derivation_payload},
                sort_keys=True,
            )
            + f" panels={len(preview.shot_panel_images)}"
            + f" reference_input_sha256={preview.reference_input_sha256}"
        )
        compiled = writer._prompt(  # noqa: SLF001 - 与 create() 相同的提示词步骤，只读
            preview,
            owner_id,
            storyboard_id,
            uuid4(),
            args.idempotency_key,
            seed,
            UUID(str(source_attempt)),
            True,
            edited_prompt=edited,
            edited_prompt_sha256=digest,
        )
        print(
            "DRYRUN_OK "
            + json.dumps(
                {
                    "group_seq": pointers[0]["sequence"],
                    "group_id": str(group_id),
                    "storyboard_id": str(storyboard_id),
                    "prompt_source_attempt_id": str(source_attempt),
                    "seed": seed,
                    "strategy": compiled.strategy.value,
                    "model": compiled.model,
                    "receipt": compiled.provider_receipt,
                    "edited_prompt_sha256": digest,
                    "identical_to_source": edited == source_text,
                    "project_production_state": production_state,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    writer = ManualH3IntentWriter(
        database.unit_of_work,
        storyboard_repository,
        loader,
        prompt_compiler=None,
        intent_group_loader=loader,
    )
    attempt_id = writer.create(
        owner_id=owner_id,
        group_id=group_id,
        expected_storyboard_generation_id=storyboard_id,
        idempotency_key=args.idempotency_key,
        seed=seed,
        experiment_arm="edited_v1",
        prompt_source_attempt_id=UUID(str(source_attempt)),
        edited_prompt=edited,
        edited_prompt_sha256=digest,
        variant_label=args.variant_label,
    )
    log(f"create() returned attempt_id={attempt_id}")
    record = read(
        "select attempt_id::text as attempt_id, stage, archive_stage, node_id, error_code, "
        "idempotency_key, input_snapshot_json -> 'seed' as seed, "
        "input_snapshot_json -> 'experiment' as experiment, "
        "input_snapshot_json -> 'video_prompt' ->> 'strategy' as strategy, "
        "md5(request_json ->> 'prompt') as prompt_md5 "
        "from video_generation_attempts where attempt_id = cast(:a as uuid)",
        a=str(attempt_id),
    )
    readback = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    print("READBACK " + readback, flush=True)
    after = read(
        "select attempt_id::text as attempt_id from group_current_videos "
        "where group_id = cast(:g as uuid)",
        g=str(group_id),
    )
    log(f"current video after: {after[0]['attempt_id'] if after else None}")
    if not record:
        fail(f"attempt {attempt_id} not found after create()", code=4)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - 原样记录后以非 0 退出，不重试
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        log(f"ERROR {type(error).__name__} code={code} message={message} text={error}")
        raise SystemExit(3) from error
