// STEP 02 分句预检的展示层视图模型。
// 只放不依赖 DOM 的纯函数：预检响应汇总与行模型推导，便于 node --test 覆盖；
// app.js 负责渲染与事件绑定。估算时长仅供冻结前提示，不参与任何生产计算。

export function summarizePreflight(result) {
  const sentences = Array.isArray(result?.sentences) ? result.sentences : [];
  const separators = sentences.filter((sentence) => sentence?.is_separator === true).length;
  const overLimit = sentences.filter(
    (sentence) => sentence?.estimated_over_limit === true
  ).length;
  return {
    total: sentences.length,
    narratable: sentences.length - separators,
    separators,
    overLimit,
  };
}

export function previewRows(result) {
  const sentences = Array.isArray(result?.sentences) ? result.sentences : [];
  return sentences.map((sentence) => {
    if (sentence?.is_separator === true) {
      return {
        kind: "separator",
        sequence: Number(sentence.sequence) || 0,
        text: String(sentence.text || ""),
      };
    }
    const estimated = Number(sentence?.estimated_duration_ms);
    return {
      kind: "sentence",
      sequence: Number(sentence?.sequence) || 0,
      text: String(sentence?.text || ""),
      estimatedMs: Number.isFinite(estimated) ? estimated : null,
      overLimit: sentence?.estimated_over_limit === true,
    };
  });
}

export function confirmWarningText(summary) {
  if (!summary || summary.overLimit < 1) return "";
  return `分句预检：${summary.overLimit} 句估算配音可能超过 15 秒，对应分组将无法生成视频；建议返回 STEP 02 调整文案后再确认。`;
}

// 文案草稿实时统计（第 02 步统计条）：字符数按 UTF-16 长度（与封面字数计数一致），
// 句数按非空行估算；分句权威结果仍以后端预检为准。
export function scriptDraftCounts(script) {
  const text = String(script ?? "");
  return {
    chars: text.length,
    lines: text.split("\n").filter((line) => line.trim()).length,
  };
}
