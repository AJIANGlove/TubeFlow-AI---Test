#!/usr/bin/env python3
"""§51 抖动 A/B：在 web 容器内提交**一步**手动 H3 尝试（只调用 create()）。

用户 2026-11-06 裁定：第 4 步改走容器内脚本，不走 HTTP。约束与本文件对应关系：

  a) 构造与 backend/src/tubeflow_test/api/app.py 相同的 ManualH3IntentWriter，只用其 create()；
     不调用 DurableGroupVideoOrchestrator.trigger，也不走任何其他写入路径。
  b) owner_id 取自 projects 表中该项目的 owner_id（不硬编码账号）。
  c) HTTP 层 ManualH3Input 的校验在这里逐条补齐（见 validate_input）。
  d) 每次提交前 GET /group-videos/contract，available 必须为 true 才提交。
  e) 一次运行只提交一步：全部参数显式传入；本文件没有 for/while 循环，也没有自动重试。
  f) 提交后用只读 SQL 回读该 attempt 并打印。
  g) 脚本原文与每次运行的输出都存入本目录。

除 writer.create() 之外，本脚本不做任何写入（--dry-run 连 create() 都不调用）。

用法（容器内 /app）：

  python submit_step.py --group-id <uuid> --storyboard-id <uuid> \
      --seed 798160553724158 --arm baseline \
      --idempotency-key jitter-ab-v1-g7-s1-baseline --step-label g7-s1-baseline

  python submit_step.py ... --arm cadence_v1 --prompt-source-attempt-id <uuid> ...

  加 --dry-run：只做校验 + 探活 + 只读查询，不创建尝试。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# --- 生产接线用到的装配件（模块路径与 api/app.py 一致） ---------------------
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
from tubeflow_test.adapters.persistence.video_prompt_diagnostics import (
    SqlVideoPromptDiagnostics,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.adapters import build_h3_archive_adapter
from tubeflow_test.video_generation.persistence import ManualH3IntentWriter
from tubeflow_test.video_prompt.compiler import build_video_prompt_compiler

ARMS = ("baseline", "cadence_v1")
SEED_MAX = 2**53 - 1


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", flush=True)


def fail(message: str, code: int = 2) -> None:
    log(f"REFUSED: {message}")
    raise SystemExit(code)


def validate_input(args: argparse.Namespace) -> None:
    """逐条复刻 api.py::ManualH3Input 的 pydantic 校验（extra="forbid" 由 argparse 保证）。"""
    if args.arm is None:
        if args.prompt_source_attempt_id is not None:
            fail("prompt_source_attempt_id requires experiment_arm")
        return
    if args.arm not in ARMS:
        fail(f"experiment_arm must be one of {ARMS}, got {args.arm!r}")
    if args.seed is None:
        fail("experiment arms require an explicit seed")
    if (args.arm == "cadence_v1") != (args.prompt_source_attempt_id is not None):
        fail("only the cadence_v1 arm takes prompt_source_attempt_id")
    if not (args.prompt_source_attempt_id is None or args.arm == "cadence_v1"):
        fail("baseline arm must not carry prompt_source_attempt_id")
    # Field(ge=0, le=2**53-1)
    if args.seed is not None and not (0 <= args.seed <= SEED_MAX):
        fail(f"seed out of range [0, {SEED_MAX}]: {args.seed}")


def check_contract(url: str) -> dict[str, Any]:
    """d) 提交前探活：GET /group-videos/contract，available 必须为 true。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - 容器内 localhost
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        fail(f"contract probe returned HTTP {error.code}: {body}")
    except Exception as error:  # noqa: BLE001 - 探活失败一律拒绝提交
        fail(f"contract probe failed: {error!r}")
    if status != 200:
        fail(f"contract probe returned HTTP {status}: {body}")
    try:
        contract = json.loads(body)
    except json.JSONDecodeError:
        fail(f"contract probe returned non-JSON: {body[:400]}")
    log(f"contract: {json.dumps(contract, ensure_ascii=False, sort_keys=True)}")
    if not isinstance(contract, dict) or contract.get("available") is not True:
        fail(f"contract is not available: {json.dumps(contract, ensure_ascii=False)}")
    return contract


def resolve_owner_id(sessions: sessionmaker[Session], project_id: UUID) -> UUID:
    """b) owner_id 取 projects 表该项目的 owner_id。"""
    with sessions() as session:
        owner_id = session.scalar(select(ProjectRow.owner_id).where(ProjectRow.id == project_id))
    if owner_id is None:
        fail(f"project not found in projects table: {project_id}")
    log(f"owner_id (from projects): {owner_id}")
    return owner_id


def build_writer(
    settings: Settings,
    database: Database,
    sessions: sessionmaker[Session],
    storage: PrivateObjectStorage,
) -> ManualH3IntentWriter:
    """a) 与 api/app.py:336-379 相同的装配顺序，只保留 writer 需要的部件。"""
    video_asset_store = build_h3_archive_adapter(sessions, storage)
    requested_policy, policy_allowed = single_panel_policy_settings(settings)
    _narration_video_authority, _storyboard_video_authority, loader = canonical_video_authorities(
        settings,
        reference_policy=requested_policy,
        single_panel_allowed=policy_allowed,
        panel_derivatives=ShotPanelAssetService(sessions, storage, video_asset_store),
    )
    narration_authority = SqlStoryboardNarrationAuthority(database.session, storage)
    storyboard_repository = SqlAlchemyStoryboardRepository(
        database.unit_of_work, narration_authority
    )
    video_prompt_compiler = build_video_prompt_compiler(
        settings, SqlVideoPromptDiagnostics(database.unit_of_work).record
    )
    if video_prompt_compiler is None:
        fail("video prompt compiler is not configured (baseline arm cannot compile)")
    log(
        "compiler: strategy="
        f"{settings.video_prompt_strategy} model={settings.video_prompt_provider_model}"
    )
    return ManualH3IntentWriter(
        database.unit_of_work,
        storyboard_repository,
        loader,
        prompt_compiler=video_prompt_compiler,
        intent_group_loader=loader,
    )


def read_back(sessions: sessionmaker[Session], attempt_id: UUID) -> dict[str, Any]:
    """f) 只读回读：该 attempt 的关键列 + 快照里的 experiment / seed。"""
    with sessions() as session:
        row = session.execute(
            text(
                """
                SELECT attempt_id::text AS attempt_id,
                       project_id::text AS project_id,
                       group_id::text AS group_id,
                       generation_id::text AS generation_id,
                       idempotency_key,
                       superseded_attempt_id::text AS superseded_attempt_id,
                       stage,
                       archive_stage,
                       node_id,
                       error_code,
                       budget_round,
                       input_snapshot_json -> 'seed' AS seed,
                       input_snapshot_json -> 'experiment' AS experiment,
                       input_snapshot_json -> 'manual_request' AS manual_request,
                       updated_at::text AS updated_at
                  FROM video_generation_attempts
                 WHERE attempt_id = :attempt_id
                """
            ),
            {"attempt_id": str(attempt_id)},
        ).mappings().one_or_none()
    if row is None:
        return {"found": False, "attempt_id": str(attempt_id)}
    return {"found": True, **{key: row[key] for key in row.keys()}}


def count_active_on_group(sessions: sessionmaker[Session], group_id: UUID) -> list[dict[str, Any]]:
    """同组未终结尝试（同组同一时刻只允许一个）。"""
    with sessions() as session:
        rows = session.execute(
            text(
                """
                SELECT attempt_id::text AS attempt_id, stage, idempotency_key
                  FROM video_generation_attempts
                 WHERE group_id = :group_id
                   AND stage IN ('CREATED','NOT_SUBMITTED','STAGING','SUBMITTING',
                                 'REMOTE_STATE_UNKNOWN','SUBMITTED','RUNNING')
                 ORDER BY updated_at DESC
                """
            ),
            {"group_id": str(group_id)},
        ).mappings().all()
    return [dict(row) for row in rows]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="§51 jitter A/B: submit exactly one manual H3 step")
    parser.add_argument("--project-id", required=True, help="484746c4…")
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--storyboard-id", required=True, help="expected_storyboard_generation_id")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--arm", choices=ARMS, default=None)
    parser.add_argument("--prompt-source-attempt-id", default=None)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--step-label", required=True, help="g7-s1-baseline 之类，仅用于日志")
    parser.add_argument(
        "--contract-url", default="http://localhost:8000/group-videos/contract"
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验，不创建尝试")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log(f"step={args.step_label} idempotency_key={args.idempotency_key}")
    log(
        "args: "
        + json.dumps(
            {
                "project_id": args.project_id,
                "group_id": args.group_id,
                "storyboard_id": args.storyboard_id,
                "seed": args.seed,
                "arm": args.arm,
                "prompt_source_attempt_id": args.prompt_source_attempt_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    # c) HTTP 层校验
    validate_input(args)

    # d) 提交前探活
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
    group_id = UUID(args.group_id)
    storyboard_id = UUID(args.storyboard_id)
    owner_id = resolve_owner_id(sessions, project_id)

    # 只读：同组未终结尝试必须为空
    active = count_active_on_group(sessions, group_id)
    log(f"active attempts on group before submit: {json.dumps(active, ensure_ascii=False)}")
    if active:
        fail(f"group already has a non-terminal attempt: {json.dumps(active, ensure_ascii=False)}")

    writer = build_writer(settings, database, sessions, storage)

    if args.dry_run:
        log("DRY RUN: wiring built, no attempt created")
        return 0

    attempt_id = writer.create(
        owner_id=owner_id,
        group_id=group_id,
        expected_storyboard_generation_id=storyboard_id,
        idempotency_key=args.idempotency_key,
        seed=args.seed,
        experiment_arm=args.arm,
        prompt_source_attempt_id=(
            UUID(args.prompt_source_attempt_id) if args.prompt_source_attempt_id else None
        ),
    )
    log(f"create() returned attempt_id={attempt_id}")

    record = read_back(sessions, attempt_id)
    print("READBACK " + json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
    if not record.get("found"):
        fail(f"attempt {attempt_id} not found after create()", code=4)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except DomainError as error:
        log(f"DOMAIN ERROR code={error.code} message={error.message}")
        raise SystemExit(3) from error
