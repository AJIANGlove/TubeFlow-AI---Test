# source: .../api/storyboards.py L208-243（PUT draft 保存后：仅当 revision.may_apply 才按原因码自动续跑）
        )
        links = tuple(
            ShotSentenceLink(item.shot_id, sentence_id)
            for item in body.shots
            for sentence_id in item.sentence_ids
        )
        revision = call(
            lambda: director.edit_draft(
                owner_id=account.id,
                group_id=group_id,
                shots=shots,
                links=links,
                expected_draft_revision_id=body.expected_draft_revision_id,
                source_candidate_id=body.source_candidate_id,
                continuous_observation_approvals=tuple(
                    ContinuousObservationApproval(
                        item.previous_shot_id, item.next_shot_id, item.reason
                    )
                    for item in body.continuous_observation_approvals
                ),
            )
        )
        # §20 修正保存即续跑：草稿事务已提交；仅当本次草稿可应用且该项目 AUTO
        # 运行正因此原因暂停时，按原因码守卫自动恢复（授权轮次 +1），其余不动。
        if revision.may_apply:
            pipeline = getattr(request.app.state, "auto_pipeline_service", None)
            if pipeline is not None:
                pipeline.resume_if_paused_for(
                    account.id,
                    revision.project_id,
                    reason_code=REASON_DIRECTOR_VALIDATION_FAILED,
                )
        return {
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            "review_state": revision.review_state,
