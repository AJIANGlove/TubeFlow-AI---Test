export const narrationFailure = (row) => ["FAILED", "RECOVERY_REQUIRED"].includes(row?.public_status);

export function narrationRows(narration, failuresOnly = false) {
  const rows = Array.isArray(narration?.sentence_statuses) ? narration.sentence_statuses : [];
  return failuresOnly ? rows.filter(narrationFailure) : rows;
}

export function retryableNarrationRows(narration) {
  return narrationRows(narration).filter((row) => row.allowed_actions?.includes("RETRY_INVALID_OUTPUT"));
}

export function narrationIsRunning(narration) {
  return ["QUEUED", "RUNNING"].includes(narration?.public_status)
    || narrationRows(narration).some((row) => ["QUEUED", "RUNNING"].includes(row.public_status));
}

export const NARRATION_RETRY_EVIDENCE = "用户确认重新生成本地输出质检未通过的句子配音。";
