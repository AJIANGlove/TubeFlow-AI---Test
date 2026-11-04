# source: /usr/local/lib/python3.12/site-packages/tubeflow_test/auto_pipeline/service.py
# L128-160
        )
        self._sweep_inflight: set[UUID] = set()
        self._sweep_guard = threading.Lock()

    def advance(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        """持锁推进恰好一个动作；无活动运行或非 RUNNING 时不做任何事。"""
        with self._runs.lock_active(owner_id, project_id) as lock:
            run = lock.run
            if run is None or run.state is not AutoPipelineState.RUNNING:
                return run
            facts = self._facts.load(owner_id, project_id)
            if facts.production_mode != "AUTO":
                # 防御：AUTO 运行只可能属于 AUTO 项目；异常时不做动作。
                logger.error(
                    "auto pipeline run %s found for non-AUTO project %s",
                    run.run_id,
                    project_id,
                )
                return run
            updated = self._advance_locked(run, facts)
            if updated is not run:
                lock.save(updated)
            return updated

    def resume(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun:
        """用户恢复 = 新一轮授权：预算轮次 +1、清原因、置 RUNNING 并立即踢 advance。"""
        run = self._runs.resume_paused(owner_id, project_id, now=self._clock())
        self.kick(owner_id, project_id)
        return run

    def resume_if_paused_for(self, owner_id: UUID, project_id: UUID, *, reason_code: str) -> bool:
        """修正保存即续跑（§20）：仅当暂停原因码匹配时才恢复并踢 advance。

# L188-200 (kick)
            return updated

    def kick(self, owner_id: UUID, project_id: UUID) -> None:
        """终态回调处的安全踢点；失败只记日志，绝不破坏主流程。"""
        try:
            self.advance(owner_id, project_id)
        except Exception:
            logger.exception("auto pipeline kick failed for project %s", project_id)

    def _sweep_project(self, owner_id: UUID, project_id: UUID) -> None:
        try:
            self.kick(owner_id, project_id)
        finally:
