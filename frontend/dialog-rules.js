// 站内弹窗的输入校验规则（纯函数，便于 node --test 覆盖；app.js 负责渲染与事件绑定）。

// 重置密码：至少 8 位且两次输入一致；先报长度再报不一致，无问题返回空串。
export function passwordPairIssue(password, confirm) {
  const first = String(password ?? "");
  if (first.length < 8) return "too_short";
  if (first !== String(confirm ?? "")) return "mismatch";
  return "";
}
