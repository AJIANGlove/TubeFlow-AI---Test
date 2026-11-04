# source: .../adapters/persistence/auto_pipeline.py L183-217（恢复只改状态与预算轮次，不动任何分组/方案数据）
    def resume_paused(self, owner_id: UUID, project_id: UUID, *, now: datetime) -> AutoPipelineRun:
        """原子恢复：同一事务内校验 PAUSED、项目预算轮次 +1、运行置 RUNNING。"""
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(AutoPipelineRunRow)
                .where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
                )
                .with_for_update()
            )
            if row is None or row.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found")
            current = _run_from_row(row)
            if current.state is not AutoPipelineState.PAUSED:
                raise DomainError(
                    "auto_pipeline_not_paused",
                    "Only a paused auto pipeline can be resumed",
                )
            current_round = session.scalar(
                select(ProjectRow.auto_budget_round).where(ProjectRow.id == project_id)
            )
            if current_round is None:
                raise DomainError("not_found", "Resource not found")
            new_round = int(current_round) + 1
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id)
                .values(auto_budget_round=new_round)
            )
            resumed = current.resumed(new_round, now)
            _write_row(row, resumed)
            session.flush()
            return resumed

