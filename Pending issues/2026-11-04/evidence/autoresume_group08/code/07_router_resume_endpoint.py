# source: .../identity_project/router.py L672-690（resume 路由，200 返回 kick 前的 run 快照）
    @router.post("/projects/{project_id}/auto-pipeline/resume")
    def resume_auto_pipeline(
        project_id: UUID,
        request: Request,
        account: Account = Depends(actor),
    ) -> dict[str, Any]:
        # §15.9：仅 PAUSED 可恢复；恢复 = 新一轮授权（预算轮次 +1）并立即推进。
        pipeline: AutoPipelineService | None = getattr(
            request.app.state, "auto_pipeline_service", None
        )
        if pipeline is None:
            raise _http(
                DomainError("auto_pipeline_unavailable", "Auto pipeline is unavailable"), 503
            )
        run = _call(lambda: pipeline.resume(account.id, project_id))
        return {
            "state": run.state.value,
            "reason_code": run.reason_code,
            "authorization_round": run.authorization_round,
