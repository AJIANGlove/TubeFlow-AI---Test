# source: /app/frontend/app.js L5655-5668（恢复按钮：任何 2xx 都弹“已恢复自动执行”）
    <section class="panel"><div class="panel-head"><div><h2>全链路进度</h2><p>计数与状态全部来自后端权威 workflow-summary；用时按后端阶段时间戳展示，秒级刷新。</p></div>${totalMarkup}</div><div class="panel-body"><div class="sentence-list">${rowsMarkup}</div></div></section>
    ${productionComplete ? `${defectGroupsNotice ? `<div class="notice-card amber"><strong>缺陷分组标注</strong><p>${escapeHtml(defectGroupsNotice)}</p></div>` : ""}${composition?.video_asset_id && allows(composition, "VIEW_MEDIA") ? `<figure class="media-preview mt-14"><video id="autoProgressVideo" controls preload="metadata"></video><figcaption>当前正式成片 · 受保护媒体</figcaption></figure>` : `<div class="notice-card mt-14"><strong>成片暂不可播放</strong><p>后端返回当前成片并允许 VIEW_MEDIA 时自动开放播放。</p></div>`}` : ""}`;

  $("#refreshAutoProgress")?.addEventListener("click", refreshAndRenderCurrentStage);
  $("#publishProjectFromStage")?.addEventListener("click", () => void publishCurrentProject());
  $("#resumeAutoPipeline")?.addEventListener("click", () => runManualAction(
    () => api.autoPipelineResume(state.project.id),
    "已恢复自动执行；相关对象预算已按规则重置。",
  ));
  $("#autoExportButton")?.addEventListener("click", downloadFormalExport);
  $("#autoGoSubmit")?.addEventListener("click", () => {
    state.activeStage = "narration_settings";
    renderProjectWorkspace();
  });
