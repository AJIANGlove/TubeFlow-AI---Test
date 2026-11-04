# source: .../auto_pipeline/service.py
# L280-330（storyboard_drafts 分支：seed → invalid → pause）
        if not facts.storyboards_complete:
            if self._storyboards is None:
                return self._touch(run, phase="storyboard_drafts")
            overview = self._storyboards.overview(owner_id, project_id)

            def invalid_groups(
                items: tuple[StoryboardBatchGroupOverview, ...],
            ) -> list[StoryboardBatchGroupOverview]:
                return [
                    group
                    for group in items
                    if not group.has_applied
                    and (not group.has_draft or group.draft_review_state == "REQUIRED")
                ]

            # 本阶段动作均为本地确定性写入（无外部 AI）：缺失草稿播种 + 既有
            # REQUIRED 草稿修复；AUTO 按 §20 决策为歧义相邻对附加系统审批。
            acted = False
            failure_messages: dict[str, str] = {}
            if any(not group.has_draft and not group.has_applied for group in overview):
                seed_results = self._storyboards.seed_drafts(
                    owner_id, project_id, auto_approve_ambiguous=True
                )
                failure_messages.update(_error_messages(seed_results))
                overview = self._storyboards.overview(owner_id, project_id)
                acted = True
            invalid = invalid_groups(overview)
            if any(
                group.has_draft and group.draft_review_state == "REQUIRED" for group in invalid
            ):
                repair_results = self._storyboards.repair_ambiguous_drafts(owner_id, project_id)
                failure_messages.update(_error_messages(repair_results))
                if repair_results:
                    overview = self._storyboards.overview(owner_id, project_id)
                    invalid = invalid_groups(overview)
                    acted = True
            if invalid:
                details: dict[str, object] = {
                    "group_sequences": [group.group_sequence for group in invalid]
                }
                if failure_messages:
                    details["failure_messages"] = failure_messages
                return self._pause(
                    run,
                    REASON_DIRECTOR_VALIDATION_FAILED,
                    phase="storyboard_drafts",
                    details=details,
                )
            if acted:
                # 本阶段已写入草稿变更；故事板生成留待下一跳。
                return self._touch(run, phase="storyboard_drafts")
# L735-750 (_pause 及其日志行，web/worker 同一实现)
    def _pause(
        self,
        run: AutoPipelineRun,
        reason_code: str,
        *,
        phase: str,
        details: dict[str, object],
    ) -> AutoPipelineRun:
        paused = run.paused(reason_code, self._clock(), phase=phase, details=details)
        logger.warning(
            "auto pipeline paused: project=%s reason=%s phase=%s",
            run.project_id,
            reason_code,
            phase,
        )
        return paused
