// STEP 02 复制文案的纯函数：只决定“复制的文本是什么”和“是否为空”，
// 不依赖 DOM，便于 node --test 覆盖；剪贴板写入与按钮交互由 app.js 负责。

export function hasCopyableText(text) {
  return typeof text === "string" && text.trim().length > 0;
}

export function scriptCopyText(project, editorValue) {
  // 冻结项目以不可变快照为准；快照文案为空时也返回空串，不回退到草稿。
  if (project?.snapshot && typeof project.snapshot.script === "string") {
    return project.snapshot.script;
  }
  // 草稿/编辑态必须读输入框当前值：防抖保存成功前 state.project.script 仍是旧值。
  if (typeof editorValue === "string") return editorValue;
  return typeof project?.script === "string" ? project.script : "";
}
