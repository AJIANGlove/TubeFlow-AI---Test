# source: .../application/storyboard_batch.py L154-202（seed_drafts：逐组重建 DirectorPlan，ValueError 被吞成 seed error）
    def seed_drafts(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        auto_approve_ambiguous: bool = False,
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        results: list[StoryboardBatchSeedResult] = []
        for candidate in self._candidates(owner_id, project_id):
            group_id = candidate.group_id
            try:
                if self._storyboards.get_draft_revision(owner_id, group_id) is not None:
                    results.append(
                        StoryboardBatchSeedResult(
                            group_id, candidate.group_sequence, "already_draft"
                        )
                    )
                    continue
                if self._storyboards.get_applied_revision(owner_id, group_id) is not None:
                    results.append(
                        StoryboardBatchSeedResult(
                            group_id, candidate.group_sequence, "already_applied"
                        )
                    )
                    continue
                revision = self._director.edit_draft(
                    owner_id=owner_id,
                    group_id=group_id,
                    shots=candidate.shots,
                    links=candidate.sentence_links,
                    expected_draft_revision_id=None,
                    continuous_observation_approvals=(
                        _system_approvals_for(candidate.shots) if auto_approve_ambiguous else ()
                    ),
                    source_candidate_id=candidate.candidate_id,
                )
                results.append(
                    StoryboardBatchSeedResult(
                        group_id,
                        candidate.group_sequence,
                        "seeded",
                        review_state=revision.review_state.value,
                    )
                )
            except (DomainError, ValueError) as exc:
                results.append(
                    StoryboardBatchSeedResult(
                        group_id, candidate.group_sequence, "error", message=str(exc)
                    )
