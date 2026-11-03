"""封面提示词服务：意图创建（幂等）与串行两步 + 并行两版生成编排。

照 `application/storyboards.py` 的意图/幂等/终态模式。生成编排（在
CoverPromptGenerationWorkflow 的活动内执行）：

0. 参考约束解析（确定性、零 Provider）：style 文本 + 项目微调合成 ref_text，
   `parse_ref_text_byChild` 拆 byChild；dim_line_invalid → COVER_REF_TEXT_INVALID
   稳定失败（计额/模型调用前拒绝，create 与 generate 双侧执行同一判定）；
   角色绑定（v5 §1.3）在约束解析后生效：参考 provided 的 subject:: 全 5 子项
   屏蔽出拼接，首卡身份/外貌逐字进入两子项，绑定块/出场角色块注入任务单与评委；
1. 封面文字：body 提供 cover_text 时跳过（候选记为该单条）；否则文字调用
   （温度 0.9，标题 + 文案全文 + brief）→ validate_cover_text_candidates；
   调用失败或全部为空 → 回退标题（记 warning，不阻断）；
2. 匹配画面：两版并行（温度 0.8/1.0）→ validate_covers → splice 参考子项逐字
   拼接 → 装配（prompt_body + 绘字段）→ needsRetry 或审计问题时一次定向整改
   （仅「条数更多或同条数审计问题更少」才替换）；单版失败跳过记 warning，
   全失败抛错落 FAILED；
3. 评委（两版成功时，温度 0.2）→ validate_verdict；失败本地择优
   （条数 → 审计问题数）；最优版 <112 分记低分警告不阻断；
4. 最优版落 cover_prompt_generations（候选/默认与所用文字/三文本/警告/评委）
   并切 projects 当前指针。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from tubeflow_test.application.cover_ports import (
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    COVER_TERMINAL_STATUSES,
    CoverCharacterCard,
    CoverPromptGeneration,
    CoverRepository,
)
from tubeflow_test.application.cover_project import (
    COVER_PROJECT_NOT_FROZEN,
    COVER_REF_TEXT_INVALID,
    CoverProjectContext,
    CoverProjectContextPort,
    dims_from_storage,
)
from tubeflow_test.application.cover_providers import CoverPromptProvider
from tubeflow_test.application.ports import Clock, IdentifierFactory
from tubeflow_test.domain.cover import (
    BRIEF_MAX,
    COVER_TEXT_TEMPERATURE,
    COVER_VARIANT_TEMPS,
    JUDGE_TEMPERATURE,
    PROMPT_MAX,
    CharacterCardBinding,
    assemble_final_prompt,
    assemble_prompt_body,
    audit_cover_prompts,
    bind_character_cards,
    build_cover_prompt_messages,
    build_cover_text_messages,
    build_judge_messages,
    normalize_cover_text,
    parse_ref_text_by_child,
    provided_child_ids,
    ref_rule_suppressions,
    splice_ref_children,
    validate_cover_text_candidates,
    validate_covers,
    validate_verdict,
)
from tubeflow_test.domain.errors import DomainError

COVER_JUDGE_LOW_SCORE_THRESHOLD = 112  # 总分 140 的 80%

# 面板/出图共用的提示词文本稳定失败码。
COVER_PROMPT_EMPTY = "cover_prompt_empty"
COVER_PROMPT_TOO_LONG = "cover_prompt_too_long"


def _not_found() -> DomainError:
    return DomainError("not_found", "Cover resource was not found")


def validate_prompt_text(text: object) -> str:
    """面板编辑与出图取文本的共用校验：trim、非空、≤PROMPT_MAX 码点。"""
    value = ("" if text is None else str(text)).strip()
    if not value:
        raise DomainError(COVER_PROMPT_EMPTY, "Cover prompt text is empty")
    if len(value) > PROMPT_MAX:
        raise DomainError(
            COVER_PROMPT_TOO_LONG, "Cover prompt text exceeds the 4000 code point limit"
        )
    return value


def combined_ref_text(style_text: str, style_override_text: str | None) -> str:
    """风格文本 + 项目微调合成参考约束文本（微调按备注行语义附加）。"""
    base = (style_text or "").strip()
    override = (style_override_text or "").strip()
    if base and override:
        return f"{base}\n{override}"
    return base or override


@dataclass(frozen=True, slots=True)
class _RefConstraints:
    ref_text: str
    ref_dims: list[str]
    by_child: dict[str, Any]
    provided: list[str]
    rules: frozenset[str]
    invalid: bool


def resolve_ref_constraints(
    style_text: str, style_dims: object, style_override_text: str | None
) -> _RefConstraints:
    """参考约束解析（确定性，create 与 generate 共用同一判定）。"""
    ref_text = combined_ref_text(style_text, style_override_text)
    ref_dims = dims_from_storage(style_dims)
    if not ref_text:
        return _RefConstraints("", ref_dims, {}, [], frozenset(), False)
    parsed = parse_ref_text_by_child(ref_text, ref_dims)
    if not parsed["ok"]:
        if parsed["reason"] == "dim_line_invalid":
            return _RefConstraints(ref_text, ref_dims, {}, [], frozenset(), True)
        # empty 之外的非 ok 只剩 dim_line_invalid；空文本已在上方短路。
        return _RefConstraints("", ref_dims, {}, [], frozenset(), False)
    by_child = dict(parsed["by_child"])
    provided = provided_child_ids(ref_dims, by_child)
    return _RefConstraints(
        ref_text, ref_dims, by_child, provided, frozenset(ref_rule_suppressions(ref_dims)), False
    )


def _apply_character_binding(
    constraints: _RefConstraints, cards: tuple[CharacterCardBinding, ...]
) -> _RefConstraints:
    """绑定生效时的参考约束变换（v5 §1.3 屏蔽规则与优先级）；无卡原样返回（零回归）。"""
    if not cards:
        return constraints
    dims, by_child = bind_character_cards(constraints.ref_dims, constraints.by_child, cards)
    return _RefConstraints(
        constraints.ref_text,
        dims,
        by_child,
        provided_child_ids(dims, by_child),
        frozenset(ref_rule_suppressions(dims)),
        constraints.invalid,
    )


def _bindings_from_lineage(lineage: object) -> tuple[CharacterCardBinding, ...]:
    """character_cards_used 血缘 → 绑定卡；identity 为空的条目不参与绑定（防御）。"""
    items = lineage if isinstance(lineage, list) else []
    return tuple(
        CharacterCardBinding(
            str(item.get("identity_text") or ""),
            str(item.get("appearance_text") or ""),
        )
        for item in items
        if isinstance(item, dict) and str(item.get("identity_text") or "").strip()
    )


def _character_card_lineage(cards: tuple[CoverCharacterCard, ...]) -> list[dict[str, Any]]:
    """绑定卡 → character_cards_used 血缘快照（v5 §1.1，create 时落库）。"""
    return [
        {
            "card_id": str(card.id),
            "character_image_id": str(card.character_image_id),
            "identity_text": card.identity_text,
            "appearance_text": card.appearance_text,
        }
        for card in cards
    ]


@dataclass(frozen=True, slots=True)
class _Candidate:
    prompt_body: str
    prompt: str
    cover_count: int
    issue_count: int
    dim_issues: tuple[dict[str, Any], ...]
    audit_issues: tuple[dict[str, Any], ...]

    def better_than(self, other: _Candidate) -> bool:
        """整改替换判据：条数更多，或同条数审计问题更少。"""
        if self.cover_count != other.cover_count:
            return self.cover_count > other.cover_count
        return self.issue_count < other.issue_count


class CoverPromptService:
    def __init__(
        self,
        repo: CoverRepository,
        projects: CoverProjectContextPort,
        provider: CoverPromptProvider,
        ids: IdentifierFactory,
        clock: Clock,
        *,
        model: str,
    ) -> None:
        self._repo, self._projects, self._provider = repo, projects, provider
        self._ids, self._clock, self._model = ids, clock, model

    def create(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        brief: str = "",
        style_override_text: str | None = None,
        style_source: str | None = None,
        style_profile_id: UUID | None = None,
        cover_text: str | None = None,
        character_card_ids: list[UUID] | tuple[UUID, ...] | None = None,
        idempotency_key: str,
    ) -> CoverPromptGeneration:
        context = self._frozen_context(owner_id, project_id)
        brief_text = (brief or "").strip()
        if len(brief_text) > BRIEF_MAX:
            raise DomainError(
                "cover_brief_too_long", "Cover brief exceeds the 500 code point limit"
            )
        override = (style_override_text or "").strip() or None
        source, profile_id, style_text, style_dims = self._resolve_style(
            owner_id, context, style_source, style_profile_id
        )
        constraints = resolve_ref_constraints(style_text, style_dims, override)
        if constraints.invalid:
            raise DomainError(
                COVER_REF_TEXT_INVALID, "参考约束文本按勾选维度回读失败，请修正风格文本。"
            )
        cards = self._resolve_character_cards(owner_id, context, character_card_ids)
        candidates: list[dict[str, Any]] = []
        default = ""
        used = ""
        warnings: list[str] = []
        if cover_text is not None:
            used, warnings = normalize_cover_text(cover_text, context.title)
            default = used
            candidates = [{"text": used}]
        generation = CoverPromptGeneration(
            self._ids.new(),
            owner_id,
            project_id,
            0,
            source,
            profile_id,
            style_text,
            style_dims,
            override,
            brief_text,
            candidates,
            default,
            used,
            "",
            "",
            "",
            context.title,
            hashlib.sha256(context.frozen_script.encode()).hexdigest()
            if context.frozen_script is not None
            else "",
            warnings,
            None,
            self._model,
            {"cover_text": used if cover_text is not None else None},
            idempotency_key,
            COVER_STATUS_INTENT_PERSISTED,
            None,
            None,
            self._clock.now(),
            character_cards_used=_character_card_lineage(cards),
        )
        return self._repo.create_prompt_generation(generation)

    async def generate(self, owner_id: UUID, generation_id: UUID) -> CoverPromptGeneration:
        """生成编排（活动内执行）；DomainError 落 FAILED 终态，其余异常交由 Temporal 重试。"""
        generation = self._repo.get_prompt_generation(owner_id, generation_id)
        if generation is None:
            raise _not_found()
        if generation.status in COVER_TERMINAL_STATUSES:
            return generation
        context = self._projects.project_cover_context(owner_id, generation.project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            return self._repo.mark_prompt_generation_failed(
                owner_id, generation_id, "generate", COVER_PROJECT_NOT_FROZEN
            )
        constraints = _apply_character_binding(
            resolve_ref_constraints(
                generation.style_text_used,
                generation.style_dims_used,
                generation.style_override_text,
            ),
            _bindings_from_lineage(generation.character_cards_used),
        )
        if constraints.invalid:
            return self._repo.mark_prompt_generation_failed(
                owner_id, generation_id, "ref_text", COVER_REF_TEXT_INVALID
            )
        cards = _bindings_from_lineage(generation.character_cards_used)
        warnings = list(generation.warnings)
        candidates, default, used = await self._cover_texts(generation, context, warnings)
        outcomes, failure = await self._variants(
            generation, context, constraints, used, warnings, cards
        )
        if not outcomes:
            code = failure.code if failure is not None else "cover_prompt_generation_failed"
            return self._repo.mark_prompt_generation_failed(
                owner_id, generation_id, "generate", code
            )
        best_index, judge_record = await self._judge(
            outcomes, generation.title_used, warnings, cards
        )
        winner = outcomes[best_index]
        prompt_body = winner.prompt_body
        prompt_system = assemble_final_prompt(prompt_body, default)
        completed = self._repo.complete_prompt_generation(
            replace(
                generation,
                status=COVER_STATUS_SUCCEEDED,
                cover_text_candidates=[{"text": text} for text in candidates],
                cover_text_default=default,
                cover_text_used=used,
                prompt_body=prompt_body,
                prompt_system=prompt_system,
                prompt_current=prompt_system,
                warnings=warnings,
                judge=judge_record,
            )
        )
        return self._repo.set_current_prompt_generation(
            owner_id, generation.project_id, completed.id
        )

    def view(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverPromptGeneration | None, tuple[CoverPromptGeneration, ...]]:
        self._frozen_context(owner_id, project_id)
        return (
            self._repo.current_prompt_generation_for_project(owner_id, project_id),
            self._repo.list_prompt_generations_for_project(owner_id, project_id),
        )

    def edit_prompt_current(
        self, owner_id: UUID, generation_id: UUID, prompt_current: str
    ) -> CoverPromptGeneration:
        text = validate_prompt_text(prompt_current)
        generation = self._guarded_generation(owner_id, generation_id)
        return self._repo.update_prompt_current(owner_id, generation.id, text)

    def reset_to_system(self, owner_id: UUID, generation_id: UUID) -> CoverPromptGeneration:
        generation = self._guarded_generation(owner_id, generation_id)
        return self._repo.update_prompt_current(
            owner_id, generation.id, generation.prompt_system
        )

    def choose_cover_text(
        self, owner_id: UUID, generation_id: UUID, cover_text_choice: str
    ) -> CoverPromptGeneration:
        """换候选重装配：prompt_current = prompt_body + 绘字段(所选候选)，纯本地零 Provider。"""
        generation = self._guarded_generation(owner_id, generation_id)
        choice, _warnings = normalize_cover_text(cover_text_choice, generation.title_used)
        known = {str(item.get("text", "")) for item in generation.cover_text_candidates}
        if choice not in known:
            raise DomainError("invalid_cover_candidate", "Cover text candidate is unavailable")
        return self._repo.update_prompt_current(
            owner_id, generation.id, assemble_final_prompt(generation.prompt_body, choice)
        )

    def _guarded_generation(self, owner_id: UUID, generation_id: UUID) -> CoverPromptGeneration:
        generation = self._repo.get_prompt_generation(owner_id, generation_id)
        if generation is None:
            raise _not_found()
        self._frozen_context(owner_id, generation.project_id)
        return generation

    async def _cover_texts(
        self,
        generation: CoverPromptGeneration,
        context: CoverProjectContext,
        warnings: list[str],
    ) -> tuple[list[str], str, str]:
        """串行第一步：封面文字；cover_text 已给跳过调用，失败回退标题不阻断。"""
        if generation.cover_text_used:
            return (
                [str(item.get("text", "")) for item in generation.cover_text_candidates],
                generation.cover_text_default,
                generation.cover_text_used,
            )
        assert context.frozen_script is not None
        try:
            result = await asyncio.to_thread(
                self._provider.complete,
                build_cover_text_messages(
                    generation.title_used,
                    context.frozen_script,
                    generation.brief,
                    context.locale,
                ),
                temperature=COVER_TEXT_TEMPERATURE,
            )
            validated = validate_cover_text_candidates(result.payload, generation.title_used)
        except Exception:
            # 文字调用失败（含 4xx/5xx/超时/网络）：回退标题，不阻断画面生成。
            validated = validate_cover_text_candidates({}, generation.title_used)
        warnings.extend(validated["warnings"])
        candidates = [str(text) for text in validated["candidates"]]
        default = candidates[validated["recommended"] - 1]
        return candidates, default, default

    async def _variants(
        self,
        generation: CoverPromptGeneration,
        context: CoverProjectContext,
        constraints: _RefConstraints,
        cover_text_used: str,
        warnings: list[str],
        cards: tuple[CharacterCardBinding, ...],
    ) -> tuple[list[_Candidate], DomainError | None]:
        """串行第二步：两版并行 + 各版一次定向整改；单版失败跳过记 warning。"""
        assert context.frozen_script is not None
        results = await asyncio.gather(
            *(
                self._variant(
                    generation.title_used,
                    context.frozen_script,
                    generation.brief,
                    constraints,
                    cover_text_used,
                    temperature,
                    cards,
                )
                for temperature in COVER_VARIANT_TEMPS
            ),
            return_exceptions=True,
        )
        outcomes: list[_Candidate] = []
        failure: DomainError | None = None
        failed = 0
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failed += 1
                if failure is None and isinstance(result, DomainError):
                    failure = result
                continue
            if result.cover_count == 0:
                failed += 1
                continue
            outcomes.append(result)
        if failed:
            warnings.append("cover_variant_failed")
        return outcomes, failure

    async def _variant(
        self,
        title: str,
        script: str,
        brief: str,
        constraints: _RefConstraints,
        cover_text_used: str,
        temperature: float,
        cards: tuple[CharacterCardBinding, ...],
    ) -> _Candidate:
        messages = build_cover_prompt_messages(
            title,
            cover_text_used,
            script,
            brief,
            constraints.ref_text,
            constraints.ref_dims,
            constraints.provided,
            character_cards=cards,
        )
        first = await asyncio.to_thread(
            self._provider.complete, messages, temperature=temperature
        )
        candidate = self._build_candidate(first.payload, constraints, cover_text_used)
        # needsRetry（条数不足/维度缺失）或审计问题时一次定向整改。
        needs_repair = candidate.cover_count == 0 or candidate.issue_count > 0
        if not needs_repair:
            return candidate
        directive = _repair_directive(candidate)
        retry_messages = build_cover_prompt_messages(
            title,
            cover_text_used,
            script,
            brief,
            constraints.ref_text,
            constraints.ref_dims,
            constraints.provided,
            extra_directive=directive,
            character_cards=cards,
        )
        second = await asyncio.to_thread(
            self._provider.complete, retry_messages, temperature=temperature
        )
        repaired = self._build_candidate(second.payload, constraints, cover_text_used)
        return repaired if repaired.better_than(candidate) else candidate

    def _build_candidate(
        self,
        payload: object,
        constraints: _RefConstraints,
        cover_text_used: str,
    ) -> _Candidate:
        validated = validate_covers(payload, "16:9", constraints.provided)
        covers = validated["covers"]
        dim_issues = tuple(dict(issue) for issue in validated["dimIssues"])
        if not covers:
            return _Candidate("", "", 0, len(dim_issues), dim_issues, ())
        spliced = splice_ref_children(
            covers[0]["dims"], constraints.by_child, constraints.ref_dims
        )
        prompt_body = assemble_prompt_body(spliced, skip=constraints.rules)
        prompt = assemble_final_prompt(prompt_body, cover_text_used)
        audit = audit_cover_prompts([{"rank": 1, "prompt": prompt}])
        audit_issues = tuple(dict(issue) for issue in audit["issues"])
        return _Candidate(
            prompt_body,
            prompt,
            1,
            len(dim_issues) + len(audit_issues),
            dim_issues,
            audit_issues,
        )

    async def _judge(
        self,
        outcomes: list[_Candidate],
        title: str,
        warnings: list[str],
        cards: tuple[CharacterCardBinding, ...],
    ) -> tuple[int, dict[str, Any] | None]:
        """评委（两版成功时）；失败本地择优（条数 → 审计问题数）。"""
        if len(outcomes) < 2:
            return 0, None
        versions = [
            {
                "version": index + 1,
                "covers": [{"rank": 1, "forTitle": title, "prompt": item.prompt}],
            }
            for index, item in enumerate(outcomes)
        ]
        try:
            result = await asyncio.to_thread(
                self._provider.complete,
                build_judge_messages(versions, title, character_cards=cards),
                temperature=JUDGE_TEMPERATURE,
            )
            verdict = validate_verdict(result.payload, len(outcomes))
            if not verdict["verdicts"]:
                raise DomainError("cover_judge_response_invalid", "评委结果无有效评分。")
        except Exception:
            warnings.append("cover_judge_fallback_local")
            return _local_best(outcomes), None
        best_index = int(verdict["best"])
        best_total = next(
            (
                item["total"]
                for item in verdict["verdicts"]
                if item["version"] == best_index + 1
            ),
            None,
        )
        if best_total is not None and best_total < COVER_JUDGE_LOW_SCORE_THRESHOLD:
            warnings.append("cover_judge_low_score")
        return best_index, {
            "best": best_index + 1,
            "verdicts": verdict["verdicts"],
            "advice": verdict["advice"],
        }

    def _frozen_context(self, owner_id: UUID, project_id: UUID) -> CoverProjectContext:
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )
        return context

    def _resolve_style(
        self,
        owner_id: UUID,
        context: CoverProjectContext,
        requested: str | None,
        style_profile_id: UUID | None = None,
    ) -> tuple[str, UUID | None, str, dict[str, Any]]:
        if requested not in (None, "channel", "project", "none"):
            raise DomainError("cover_style_source_invalid", "Cover style source is invalid")
        if style_profile_id is not None:
            # 按版本生成（v5 §5）：仅频道来源有效；版本须属本频道，
            # 使用该版本文本而非频道当前指针文本，血缘照记该版本 id。
            if requested not in (None, "channel"):
                raise DomainError(
                    "cover_style_source_invalid",
                    "Cover style profile selection requires the channel style source",
                )
            selected = self._repo.get_style_profile(owner_id, style_profile_id)
            if selected is None or selected.channel_id != context.channel_id:
                raise DomainError("invalid_cover_candidate", "Cover style profile is unavailable")
            return "channel_profile", selected.id, selected.edited_text, dict(selected.dims)
        if requested == "none":
            return "none", None, "", {}
        if requested in (None, "channel"):
            profile = self._repo.current_style_profile_for_channel(
                owner_id, context.channel_id
            )
            if profile is not None:
                return (
                    "channel_profile",
                    profile.id,
                    profile.edited_text,
                    dict(profile.dims),
                )
            if requested == "channel":
                raise DomainError(
                    "cover_style_source_unavailable", "Channel cover style is unavailable"
                )
        draft = self._repo.get_style_draft_for_project(owner_id, context.project_id)
        if draft is not None:
            return "project_draft", None, draft.edited_text, dict(draft.dims)
        if requested == "project":
            raise DomainError(
                "cover_style_source_unavailable", "Project cover style draft is unavailable"
            )
        return "none", None, "", {}

    def _resolve_character_cards(
        self,
        owner_id: UUID,
        context: CoverProjectContext,
        requested: list[UUID] | tuple[UUID, ...] | None,
    ) -> tuple[CoverCharacterCard, ...]:
        """角色绑定解析（v5 §1.4/§9.4）。

        None → 默认 = 项目快照角色图 ∩ 已有卡，按快照顺序；显式 [] → 不绑定；
        显式列表 → 按入参顺序（去重），缺失/跨属主/跨频道报 not_found。
        identity 为空的卡视为无卡（照「快照角色无卡时不绑定」语义），不参与绑定
        与血缘。
        """
        if requested is None:
            snapshot_ids = context.snapshot_character_image_ids
            if not snapshot_ids:
                return ()
            by_image = {
                card.character_image_id: card
                for card in self._repo.list_character_cards_for_channel(
                    owner_id, context.channel_id
                )
            }
            cards = [by_image[image_id] for image_id in snapshot_ids if image_id in by_image]
        else:
            unique_ids = tuple(dict.fromkeys(requested))
            cards = list(self._repo.get_character_cards_by_ids(owner_id, unique_ids))
            if len(cards) != len(unique_ids) or any(
                card.channel_id != context.channel_id for card in cards
            ):
                raise _not_found()
        return tuple(card for card in cards if card.identity_text.strip())

    def set_current(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration:
        """提示词版本切换（v5 §3）：仅本项目 SUCCEEDED 候选（仓储复核）。"""
        self._frozen_context(owner_id, project_id)
        return self._repo.set_current_prompt_generation(owner_id, project_id, generation_id)


def _local_best(outcomes: list[_Candidate]) -> int:
    return max(
        range(len(outcomes)),
        key=lambda index: (outcomes[index].cover_count, -outcomes[index].issue_count),
    )


def _repair_directive(candidate: _Candidate) -> str:
    """定向整改要求：由首版的契约缺失与审计问题派生（动态按 1 条输出）。"""
    parts: list[str] = []
    if candidate.cover_count == 0:
        parts.append(
            "上一版未输出任何有效封面，必须正好输出 1 条含完整 dims 八个维度的封面。"
        )
    else:
        missing = [
            str(dim)
            for issue in candidate.dim_issues
            for dim in issue.get("dims", [])
        ]
        if missing:
            parts.append(f"上一版缺失维度：{'、'.join(missing)}，必须补齐八个维度。")
        for issue in candidate.audit_issues:
            word = issue.get("word")
            if word:
                parts.append(f"上一版包含违禁内容「{word}」，必须移除并改写相关描述。")
    return "".join(parts) or "上一版未通过生成契约校验，请按任务单重新输出 1 条封面。"
