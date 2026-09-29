export type DingTalkAccessState = "pending_configuration" | "ready";

export interface DingTalkIdentityStatus {
  access_state: DingTalkAccessState;
  can_manage_system: boolean;
}


/** 表示免登码交换失败时可直接展示给教师的安全中文提示。 */
export class DingTalkIdentityExchangeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DingTalkIdentityExchangeError";
  }
}


/** 向后端提交一次性免登码并读取最小访问状态；浏览器自动处理会话 Cookie。 */
export async function exchangeDingTalkAuthCode(auth_code: string): Promise<DingTalkIdentityStatus> {
  const response = await fetch("/api/v1/auth/dingtalk/exchange", {
    body: JSON.stringify({ auth_code }),
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
  const payload: unknown = await response.json().catch(() => undefined);

  if (!response.ok) {
    throw new DingTalkIdentityExchangeError(read_safe_error_message(payload));
  }
  if (!is_identity_status(payload)) {
    throw new DingTalkIdentityExchangeError("身份验证响应异常，请从钉钉工作台重新打开系统。");
  }
  return payload;
}


/** 从后端安全错误结构读取中文提示，不展示未知响应内容。 */
function read_safe_error_message(payload: unknown): string {
  if (is_record(payload) && typeof payload.detail === "string" && payload.detail.length > 0) {
    return payload.detail;
  }
  return "身份验证暂时不可用，请稍后重试。";
}


/** 验证后端返回的最小访问状态，不信任未校验的 JSON 字段。 */
function is_identity_status(payload: unknown): payload is DingTalkIdentityStatus {
  return (
    is_record(payload) &&
    (payload.access_state === "pending_configuration" || payload.access_state === "ready") &&
    typeof payload.can_manage_system === "boolean"
  );
}


/** 判断未知 JSON 值是否可安全读取为对象字段。 */
function is_record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
