# source: .../application/project_director.py
# L108-120（审计型冲突白名单 / _blocking_conflicts）

# 2026-10-16：计时投影无解触发自动相邻合并的审计型冲突码（monotone-projection-v1 配套）。
SHOT_TIMING_CONVERGED_CONFLICT = "shot_timing_converged"
# 审计型冲突：如实随候选存档与展示，但不阻断播种、不触发 REVIEW_REQUIRED。
_AUDIT_ONLY_CONFLICT_CODES = frozenset({SHOT_TIMING_CONVERGED_CONFLICT})


def _blocking_conflicts(
    conflicts: tuple[ProjectDirectorConflict, ...],
) -> tuple[ProjectDirectorConflict, ...]:
    return tuple(item for item in conflicts if item.code not in _AUDIT_ONLY_CONFLICT_CODES)


# L1836-1888（关键：final_revision 构造与 ValueError→group_diversity_invalid 兜底都在 if not blocking 内）
                                source_to_new[value]
                                for value in conflict.shot_ids
                                if value in source_to_new
                            )
                        ),
                    )
                )
        ambiguous = any(
            current.changes_from(previous) < 2
            for previous, current in zip(new_shots, new_shots[1:], strict=False)
        )
        # 审计型冲突（shot_timing_converged）不阻断、不触发 REVIEW_REQUIRED。
        blocking = _blocking_conflicts(tuple(conflicts))
        review_state = ReviewState.REQUIRED if blocking or ambiguous else ReviewState.NOT_REQUIRED
        final_revision: DirectorPlanRevision | None = None
        if (
            new_shots
            and len(new_shots) <= DIRECTOR_GROUP_SHOT_LIMIT
            and not missing
            and not blocking
        ):
            try:
                final_revision = DirectorPlanRevision(
                    revision_id=self._ids.new(),
                    owner_id=revision.owner_id,
                    project_id=revision.project_id,
                    group_id=group.group_id,
                    revision_number=1,
                    kind=DirectorRevisionKind.DRAFT,
                    shots=tuple(new_shots),
                    sentence_links=tuple(new_links),
                    group_sentence_ids=group.sentence_ids,
                    review_state=(ReviewState.REQUIRED if ambiguous else ReviewState.NOT_REQUIRED),
                    continuous_observation_approvals=(),
                    created_at=self._clock.now(),
                )
            except ValueError:
                conflicts.append(
                    ProjectDirectorConflict(
                        "group_diversity_invalid",
                        group.sentence_ids,
                        tuple(item.shot_id for item in new_shots),
                    )
                )
                review_state = ReviewState.REQUIRED
        return ProjectDirectorGroupCandidate(
            candidate_id=self._ids.new(),
            group_id=group.group_id,
            group_sequence=group.sequence,
            shots=tuple(new_shots),
            sentence_links=tuple(new_links),
            source_shot_links=tuple(source_shot_links),
            conflicts=tuple(conflicts),
