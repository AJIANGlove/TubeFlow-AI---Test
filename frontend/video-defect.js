// 带病交付（deliver_with_defect）缺陷标注的展示模型（纯函数，便于 node --test 覆盖）。
// 数据全部来自后端权威字段：组视频视图的 delivered_with_defect / content_defect，
// 与 workflow-summary 的 auto_pipeline.defect_groups；前端不自行推算或伪造缺陷事实。

export function deliveredWithDefect(video) {
  return video?.delivered_with_defect === true;
}

// 黑屏时段列表：取自 content_defect.findings 的 start/end 秒，一位小数（与后端 QC 证据同精度）。
export function defectWindows(video) {
  const findings = Array.isArray(video?.content_defect?.findings) ? video.content_defect.findings : [];
  return findings
    .map((finding) => {
      const start = Number(finding?.start_second);
      const end = Number(finding?.end_second);
      return Number.isFinite(start) && Number.isFinite(end) ? `${start.toFixed(1)}s–${end.toFixed(1)}s` : null;
    })
    .filter(Boolean);
}

// STEP 08 播放器上方警示条文案；非带病交付返回空串（调用方据此不渲染）。
// 无 findings 时不拼时段，只如实说明存在画面缺陷。
export function groupVideoDefectNotice(video) {
  if (!deliveredWithDefect(video)) return "";
  const windows = defectWindows(video);
  const detail = windows.length ? `：${windows.join("、")} 大面积黑屏` : "";
  return `该组视频存在画面缺陷${detail}（已自动修复 6 次未果，按你的决策标注交付）；可在历史版本重生成或切换。`;
}

// 成片缺陷分组提示（workflow-summary 的 auto_pipeline.defect_groups）；无缺陷返回空串。
export function compositionDefectNotice(summary) {
  const groups = Array.isArray(summary?.auto_pipeline?.defect_groups)
    ? summary.auto_pipeline.defect_groups
    : [];
  if (!groups.length) return "";
  const sequences = groups
    .map((item) => Number(item?.group_sequence))
    .filter((n) => Number.isInteger(n) && n > 0);
  const suffix = sequences.length ? `（第 ${sequences.join("、")} 组）` : "";
  return `本片包含 ${groups.length} 个标注缺陷分组${suffix}。`;
}
