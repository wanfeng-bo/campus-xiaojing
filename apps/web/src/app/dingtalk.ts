import dingtalk_core from "dingtalk-jsapi/entry/union";
import {
  requestAuthCode$ as request_auth_code,
} from "dingtalk-jsapi/api/union/requestAuthCode";


type SafeAppType =
  | "MINI_APP"
  | "OTHER"
  | "WEB"
  | "WEBVIEW_IN_MINIAPP"
  | "WEEX";
type SafeOutcome = "FULFILLED" | "PENDING" | "REJECTED" | "SYNC_THROW";
type SafeInvocationPhase =
  | "AFTER_ATTACH"
  | "AFTER_CALL"
  | "AFTER_THEN_READ"
  | "BEFORE_CALL";
type SafeShape = "ERROR" | "NULL" | "OBJECT" | "UNDEFINED" | "VALUE";
type SafeWeexExec = "0" | "1" | "CHECK_ERROR" | "NA";

interface SafeInvocationObservation {
  api_callable: "0" | "1";
  app_type: SafeAppType;
  mode: "NATIVE" | "PACKAGE";
  outcome: SafeOutcome;
  phase: SafeInvocationPhase;
  shape: SafeShape;
  thenable: "0" | "1";
  weex_exec: SafeWeexExec;
}

type SafeThenMethod = (
  this: unknown,
  fulfill: (value: unknown) => void,
  fail: (error: unknown) => void,
) => unknown;


/** 钉钉 H5 容器不可用或未返回有效免登码时的安全错误。 */
export class DingTalkAuthCodeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DingTalkAuthCodeError";
  }
}


/** 通过已加载的钉钉网页应用 JSAPI 申请一次性免登码；不提供浏览器后备登录。 */
export async function requestDingTalkAuthCode(): Promise<string> {
  const corporation_id = import.meta.env.VITE_DINGTALK_CORP_ID?.trim();
  if (corporation_id === undefined || corporation_id.length === 0) {
    throw new DingTalkAuthCodeError("系统尚未配置钉钉企业标识，请联系系统管理员。");
  }

  const client_id = import.meta.env.VITE_DINGTALK_CLIENT_ID?.trim();
  if (client_id === undefined || client_id.length === 0) {
    throw new DingTalkAuthCodeError("系统尚未配置钉钉应用标识，请联系系统管理员。");
  }

  const observation = create_safe_invocation_observation();
  try {
    const result = await invoke_request_auth_code(
      corporation_id,
      client_id,
      observation,
    );
    if (
      !is_record(result) ||
      typeof result.code !== "string" ||
      result.code.length === 0
    ) {
      throw new DingTalkAuthCodeError(
        "未能获取钉钉身份凭证，请从工作台重新打开系统。" +
          `（诊断码：${read_safe_invocation_code(observation)}）`,
      );
    }
    return result.code;
  } catch (error) {
    if (is_dingtalk_auth_code_error(error)) {
      throw error;
    }
    const diagnostic_code = read_safe_diagnostic_code(error, observation);
    throw new DingTalkAuthCodeError(
      `钉钉身份验证失败，请升级钉钉客户端后从工作台重新打开系统。` +
        `（诊断码：${diagnostic_code}）`,
    );
  }
}


/** 创建调用前的固定枚举快照，禁止读取或输出完整 UA、桥对象和供应商数据。 */
function create_safe_invocation_observation(): SafeInvocationObservation {
  const observation: SafeInvocationObservation = {
    api_callable: typeof request_auth_code === "function" ? "1" : "0",
    app_type: "OTHER",
    mode: "PACKAGE",
    outcome: "PENDING",
    phase: "BEFORE_CALL",
    shape: "UNDEFINED",
    thenable: "0",
    weex_exec: "NA",
  };
  const runtime =
    typeof window === "undefined"
      ? undefined
      : (window as unknown as Record<string, unknown>);
  try {
    observation.app_type = read_safe_app_type(dingtalk_core.env?.appType);
  } catch {
    // 固定默认值 OTHER 已覆盖不可信公开 getter 的异常。
  }
  try {
    observation.mode = runtime?.["__useNativeSDK"] ? "NATIVE" : "PACKAGE";
  } catch {
    // 固定默认值 PACKAGE 与 SDK 开关读取失败时的失败关闭语义一致。
  }
  if (observation.app_type === "WEEX") {
    try {
      observation.weex_exec = read_safe_weex_exec(
        observation.app_type,
        runtime?.["weex"],
      );
    } catch {
      observation.weex_exec = "CHECK_ERROR";
    }
  }
  return observation;
}


/** 调用新版 union 免登接口，并将 Promise 与回调两种交付方式归一为一次结算。 */
function invoke_request_auth_code(
  corporation_id: string,
  client_id: string,
  observation: SafeInvocationObservation,
): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const fulfill = (value: unknown): void => {
      if (settled) {
        return;
      }
      settled = true;
      observation.outcome = "FULFILLED";
      observation.shape = read_error_shape(value);
      resolve(value);
    };
    const fail = (error: unknown): void => {
      if (settled) {
        return;
      }
      settled = true;
      observation.outcome = "REJECTED";
      observation.shape = read_error_shape(error);
      reject(error);
    };

    try {
      const returned: unknown = request_auth_code({
        corpId: corporation_id,
        clientId: client_id,
        success: fulfill,
        fail,
      });
      observation.phase = "AFTER_CALL";
      const then_method = read_then_method(returned);
      observation.phase = "AFTER_THEN_READ";
      observation.thenable = then_method === undefined ? "0" : "1";
      if (then_method !== undefined) {
        then_method.call(returned, fulfill, fail);
        observation.phase = "AFTER_ATTACH";
      } else if (returned !== undefined) {
        fulfill(returned);
      }
    } catch (error) {
      if (!settled) {
        settled = true;
        observation.outcome = "SYNC_THROW";
        observation.shape = read_error_shape(error);
        reject(error);
      }
    }
  });
}


/** 只提取钉钉数字错误码；其他错误只使用调用快照中的固定诊断枚举。 */
function read_safe_diagnostic_code(
  error: unknown,
  observation: SafeInvocationObservation,
): string {
  try {
    if (is_record(error)) {
      for (const field of ["errorCode", "error_code", "errcode"]) {
        const value = error[field];
        if (
          (typeof value === "string" || typeof value === "number") &&
          /^\d{1,10}$/.test(String(value))
        ) {
          return `DT-JSAPI-${String(value)}`;
        }
      }
    }

    if (error instanceof Error) {
      const sdk_error_code = error.message.match(
        /\[DINGTALK-JSAPI\]\s+ERROR\s+(\d{3,6}):/,
      )?.[1];
      if (sdk_error_code !== undefined) {
        return `DT-JSAPI-${sdk_error_code}`;
      }
    }
  } catch {
    return read_safe_invocation_code(observation);
  }

  return read_safe_invocation_code(observation);
}


/** 将未知值压缩为固定类型，禁止序列化或显示供应商原始内容。 */
function read_error_shape(error: unknown): SafeShape {
  if (error === undefined) {
    return "UNDEFINED";
  }
  if (error === null) {
    return "NULL";
  }
  try {
    if (error instanceof Error) {
      return "ERROR";
    }
  } catch {
    // 恶意 Proxy 的原型检查异常不得离开固定类型分类边界。
  }
  if (is_record(error)) {
    return "OBJECT";
  }
  return "VALUE";
}


/** 将公开应用类型压缩为白名单值，未知值统一归类为 OTHER。 */
function read_safe_app_type(value: unknown): SafeAppType {
  if (
    value === "MINI_APP" ||
    value === "WEB" ||
    value === "WEBVIEW_IN_MINIAPP" ||
    value === "WEEX"
  ) {
    return value;
  }
  return "OTHER";
}


/** 仅在 WEEX 下检查 nuvajs 执行模块可用性，检查结果不包含模块返回内容。 */
function read_safe_weex_exec(
  app_type: SafeAppType,
  weex_runtime: unknown,
): SafeWeexExec {
  if (app_type !== "WEEX") {
    return "NA";
  }
  if (!is_record(weex_runtime)) {
    return "0";
  }
  try {
    const require_module = weex_runtime.requireModule;
    if (typeof require_module !== "function") {
      return "0";
    }
    const execution_module = require_module.call(weex_runtime, "nuvajs-exec");
    return is_record(execution_module) && typeof execution_module.exec === "function"
      ? "1"
      : "0";
  } catch {
    return "CHECK_ERROR";
  }
}


/** 以固定字段顺序生成调用诊断码，不附带 UA、标识符、错误正文或桥数据。 */
function read_safe_invocation_code(
  observation: SafeInvocationObservation,
): string {
  return (
    `M-${observation.mode}-A-${observation.app_type}` +
    `-T${observation.thenable}-O-${observation.outcome}` +
    `-S-${observation.shape}-X${
      observation.weex_exec === "0" || observation.weex_exec === "1"
        ? observation.weex_exec
        : `-${observation.weex_exec}`
    }-C${observation.api_callable}-P-${observation.phase}`
  );
}


/** 判断未知值是否可按只读记录检查安全错误码字段。 */
function is_record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}


/** 判断未知值是否允许只检查函数成员存在性。 */
function is_object_like(value: unknown): value is object {
  return (
    (typeof value === "object" && value !== null) ||
    typeof value === "function"
  );
}


/** 单次读取并保存 then 方法；属性读取异常由调用方的同步安全边界收口。 */
function read_then_method(value: unknown): SafeThenMethod | undefined {
  if (!is_object_like(value)) {
    return undefined;
  }
  const then_method = (value as { then?: unknown }).then;
  return typeof then_method === "function"
    ? (then_method as SafeThenMethod)
    : undefined;
}


/** 安全识别本地错误类型，恶意 Proxy 的原型检查异常统一视为非本地错误。 */
function is_dingtalk_auth_code_error(
  value: unknown,
): value is DingTalkAuthCodeError {
  try {
    return value instanceof DingTalkAuthCodeError;
  } catch {
    return false;
  }
}
