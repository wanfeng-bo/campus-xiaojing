import { afterEach, describe, expect, it, vi } from "vitest";

const {
  dingtalk_core,
  register_dingtalk_platforms,
  request_auth_code,
} = vi.hoisted(() => ({
  dingtalk_core: { env: { appType: "WEB" } },
  register_dingtalk_platforms: vi.fn(),
  request_auth_code: vi.fn(),
}));

vi.mock("dingtalk-jsapi/entry/union", () => {
  register_dingtalk_platforms();
  return { default: dingtalk_core };
});

vi.mock("dingtalk-jsapi/api/union/requestAuthCode", () => ({
  default: { requestAuthCode$: request_auth_code },
  requestAuthCode$: request_auth_code,
}));

import {
  DingTalkAuthCodeError,
  requestDingTalkAuthCode,
} from "../src/app/dingtalk";
import {
  exchangeDingTalkAuthCode,
} from "../src/app/identity_api";


function stub_public_dingtalk_configuration(): void {
  vi.stubEnv("VITE_DINGTALK_CORP_ID", "ding-test-corp");
  vi.stubEnv("VITE_DINGTALK_CLIENT_ID", "ding-test-client");
}


afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  const runtime_window = window as unknown as Record<string, unknown>;
  delete runtime_window["__useNativeSDK"];
  delete runtime_window["weex"];
  window.localStorage.clear();
  window.sessionStorage.clear();
  Object.defineProperty(dingtalk_core.env, "appType", {
    configurable: true,
    enumerable: true,
    value: "WEB",
    writable: true,
  });
  request_auth_code.mockReset();
});


describe("钉钉 H5 身份入口", () => {
  it("调用免登能力前加载钉钉多端平台桥", () => {
    expect(register_dingtalk_platforms).toHaveBeenCalledTimes(1);
  });

  it("未配置企业标识时拒绝提供浏览器备用登录", async () => {
    vi.stubEnv("VITE_DINGTALK_CORP_ID", "");
    vi.stubEnv("VITE_DINGTALK_CLIENT_ID", "ding-test-client");

    await expect(requestDingTalkAuthCode()).rejects.toThrow("系统尚未配置钉钉企业标识，请联系系统管理员。");
    expect(request_auth_code).not.toHaveBeenCalled();
  });

  it("未配置应用 Client ID 时在调用 JSAPI 前失败关闭", async () => {
    vi.stubEnv("VITE_DINGTALK_CORP_ID", "ding-test-corp");
    vi.stubEnv("VITE_DINGTALK_CLIENT_ID", " ");

    await expect(requestDingTalkAuthCode()).rejects.toThrow(
      "系统尚未配置钉钉应用标识，请联系系统管理员。",
    );
    expect(request_auth_code).not.toHaveBeenCalled();
  });

  it("默认导出不可调用时仍通过同模块命名导出获取一次性免登码", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockResolvedValue({ code: "one-time-code" });

    await expect(requestDingTalkAuthCode()).resolves.toBe("one-time-code");
    expect(request_auth_code).toHaveBeenCalledWith(
      expect.objectContaining({
        corpId: "ding-test-corp",
        clientId: "ding-test-client",
        success: expect.any(Function),
        fail: expect.any(Function),
      }),
    );
  });

  it("兼容只通过 success 回调返回免登码的原生运行时", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockImplementation(
      ({ success }: { success?: (result: { code: string }) => void }) => {
        success?.({ code: "one-time-code" });
        return undefined;
      },
    );

    await expect(requestDingTalkAuthCode()).resolves.toBe("one-time-code");
    expect(request_auth_code).toHaveBeenCalledWith(
      expect.objectContaining({
        corpId: "ding-test-corp",
        clientId: "ding-test-client",
        success: expect.any(Function),
        fail: expect.any(Function),
      }),
    );
  });

  it("Promise fulfilled 空结果时不抛原生 TypeError且只输出固定诊断", async () => {
    stub_public_dingtalk_configuration();
    vi.stubGlobal("navigator", { userAgent: "DingTalk iPhone" });
    request_auth_code.mockResolvedValue(undefined);

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-FULFILLED-S-UNDEFINED-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain("TypeError");
  });

  it("自定义 thenable 的 then getter 只读取一次", async () => {
    stub_public_dingtalk_configuration();
    let then_getter_reads = 0;
    request_auth_code.mockReturnValue({
      get then() {
        then_getter_reads += 1;
        return (fulfill: (value: { code: string }) => void): void => {
          fulfill({ code: "one-time-code" });
        };
      },
    });

    await expect(requestDingTalkAuthCode()).resolves.toBe("one-time-code");
    expect(then_getter_reads).toBe(1);
  });

  it("then getter 抛错时只输出固定同步异常诊断", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockReturnValue({
      get then(): never {
        throw new Error("private then detail");
      },
    });

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T0-O-SYNC_THROW-S-ERROR-X-NA-C1-P-AFTER_CALL",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private then detail",
    );
  });

  it("API 调用同步抛错时区分尚未返回的调用阶段", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockImplementation(() => {
      throw new Error("private call detail");
    });

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T0-O-SYNC_THROW-S-ERROR-X-NA-C1-P-BEFORE_CALL",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private call detail",
    );
  });

  it("公开 appType getter 抛错时使用 OTHER 固定分类", async () => {
    stub_public_dingtalk_configuration();
    Object.defineProperty(dingtalk_core.env, "appType", {
      configurable: true,
      get(): never {
        throw new Error("private app type detail");
      },
    });
    request_auth_code.mockRejectedValue(new Error("private provider detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-OTHER-T1-O-REJECTED-S-ERROR-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private app type detail",
    );
  });

  it("原生模式 getter 抛错时失败关闭为 PACKAGE", async () => {
    stub_public_dingtalk_configuration();
    Object.defineProperty(window, "__useNativeSDK", {
      configurable: true,
      get(): never {
        throw new Error("private native getter detail");
      },
    });
    request_auth_code.mockRejectedValue(new Error("private provider detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-REJECTED-S-ERROR-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private native getter detail",
    );
  });

  it("WEEX 全局 getter 抛错时使用固定检查异常分类", async () => {
    stub_public_dingtalk_configuration();
    dingtalk_core.env.appType = "WEEX";
    Object.defineProperty(window, "weex", {
      configurable: true,
      get(): never {
        throw new Error("private weex getter detail");
      },
    });
    request_auth_code.mockRejectedValue(new Error("private provider detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEEX-T1-O-REJECTED-S-ERROR-X-CHECK_ERROR",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private weex getter detail",
    );
  });

  it("原生 SDK 使用 truthy 开关时诊断模式与 SDK 分支一致", async () => {
    stub_public_dingtalk_configuration();
    vi.stubGlobal("__useNativeSDK", 1);
    request_auth_code.mockRejectedValue(new Error("private native detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-NATIVE-A-WEB-T1-O-REJECTED-S-ERROR-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private native detail",
    );
  });

  it("WEEX 桥拒绝时只输出应用类型和执行模块能力", async () => {
    stub_public_dingtalk_configuration();
    dingtalk_core.env.appType = "WEEX";
    vi.stubGlobal("navigator", { userAgent: "DingTalk iPhone" });
    vi.stubGlobal("weex", { requireModule: vi.fn(() => undefined) });
    request_auth_code.mockRejectedValue(new TypeError("private bridge detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEEX-T1-O-REJECTED-S-ERROR-X0",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private bridge detail",
    );
  });

  it("WEEX 模块检查 getter 抛错时只输出固定检查异常分类", async () => {
    stub_public_dingtalk_configuration();
    dingtalk_core.env.appType = "WEEX";
    vi.stubGlobal("navigator", { userAgent: "DingTalk iPhone" });
    vi.stubGlobal("weex", {
      get requireModule(): never {
        throw new Error("private getter detail");
      },
    });
    request_auth_code.mockRejectedValue(new TypeError("private bridge detail"));

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEEX-T1-O-REJECTED-S-ERROR-X-CHECK_ERROR",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private getter detail",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private bridge detail",
    );
  });

  it("JSAPI 调用失败时不跳转到浏览器备用登录", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockRejectedValue(new Error("outside dingtalk"));
    const original_url = window.location.href;

    await expect(requestDingTalkAuthCode()).rejects.toThrow(
      "钉钉身份验证失败，请升级钉钉客户端后从工作台重新打开系统。",
    );

    expect(window.location.href).toBe(original_url);
  });

  it("JSAPI 拒绝时只显示安全诊断码而不回显供应商错误内容", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockRejectedValue({
      errorCode: "52013",
      errorMessage: "sensitive provider detail ding-test-corp",
    });

    const rejected_error = await requestDingTalkAuthCode().catch((error: unknown) => error);

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toBe(
      "钉钉身份验证失败，请升级钉钉客户端后从工作台重新打开系统。" +
        "（诊断码：DT-JSAPI-52013）",
    );
    expect((rejected_error as Error).message).not.toContain("sensitive provider detail");
  });

  it("错误码 getter 抛错时回退固定调用诊断", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockRejectedValue({
      get errorCode(): never {
        throw new Error("private error code detail");
      },
    });

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-REJECTED-S-OBJECT-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private error code detail",
    );
  });

  it("Error message getter 抛错时回退固定调用诊断", async () => {
    stub_public_dingtalk_configuration();
    const provider_error = new Error();
    Object.defineProperty(provider_error, "message", {
      configurable: true,
      get(): never {
        throw new Error("private message detail");
      },
    });
    request_auth_code.mockRejectedValue(provider_error);

    const rejected_error = await requestDingTalkAuthCode().catch(
      (error: unknown) => error,
    );

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-REJECTED-S-ERROR-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private message detail",
    );
  });

  it("错误对象的 instanceof 检查抛错时仍返回固定对象分类", async () => {
    stub_public_dingtalk_configuration();
    const hostile_error = new Proxy(
      {},
      {
        getPrototypeOf(): never {
          throw new Error("private prototype detail");
        },
      },
    );
    request_auth_code.mockReturnValue({
      then(
        _fulfill: (value: unknown) => void,
        fail: (error: unknown) => void,
      ): void {
        try {
          fail(hostile_error);
        } catch {
          // 旧实现会让形状检查异常逸出；吞掉它以便测试稳定观察未结算结果。
        }
      },
    });

    const rejected_error = await Promise.race([
      requestDingTalkAuthCode().catch((error: unknown) => error),
      new Promise<string>((resolve) => {
        window.setTimeout(() => resolve("request remained pending"), 20);
      }),
    ]);

    expect(rejected_error).toBeInstanceOf(DingTalkAuthCodeError);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-REJECTED-S-OBJECT-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain(
      "private prototype detail",
    );
  });

  it("同步 fail 回调先结算时晚到 Promise 成功不覆盖错误", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockImplementation(
      ({
        fail,
      }: {
        fail?: (error: { errorCode: string }) => void;
      }) => {
        fail?.({ errorCode: "52013" });
        return Promise.resolve({ code: "late-code" });
      },
    );

    await expect(requestDingTalkAuthCode()).rejects.toThrow("DT-JSAPI-52013");
  });

  it("JSAPI 拒绝的通用 code 字段不回显并退回固定诊断分类", async () => {
    stub_public_dingtalk_configuration();
    vi.stubGlobal("navigator", { userAgent: "unit test browser" });
    request_auth_code.mockRejectedValue({ code: "123456" });

    const rejected_error = await requestDingTalkAuthCode().catch((error: unknown) => error);

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toBe(
      "钉钉身份验证失败，请升级钉钉客户端后从工作台重新打开系统。" +
        "（诊断码：M-PACKAGE-A-WEB-T1-O-REJECTED-S-OBJECT-X-NA-C1-P-AFTER_ATTACH）",
    );
    expect((rejected_error as Error).message).not.toContain("123456");
  });

  it("JSAPI 无错误详情时只显示固定调用诊断", async () => {
    stub_public_dingtalk_configuration();
    vi.stubGlobal("navigator", { userAgent: "TaurusApp DingTalk iPhone" });
    vi.stubGlobal("AlipayJSBridge", { call: vi.fn() });
    vi.stubGlobal("WebViewJavascriptBridge", {
      callHandler: vi.fn(),
      init: vi.fn(),
      secret: "must-not-leak",
    });
    request_auth_code.mockRejectedValue(undefined);

    const rejected_error = await requestDingTalkAuthCode().catch((error: unknown) => error);

    expect(rejected_error).toBeInstanceOf(Error);
    expect((rejected_error as Error).message).toContain(
      "M-PACKAGE-A-WEB-T1-O-REJECTED-S-UNDEFINED-X-NA",
    );
    expect((rejected_error as Error).message).not.toContain("must-not-leak");
    expect((rejected_error as Error).message).not.toContain("DingTalk iPhone");
  });

  it("只向身份交换接口提交一次性免登码且不保存浏览器凭证", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ access_state: "ready", can_manage_system: false }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetch_mock);
    const original_url = window.location.href;
    const console_error = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await expect(exchangeDingTalkAuthCode("one-time-code")).resolves.toEqual({
      access_state: "ready",
      can_manage_system: false,
    });
    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/auth/dingtalk/exchange",
      expect.objectContaining({
        body: JSON.stringify({ auth_code: "one-time-code" }),
        credentials: "same-origin",
        method: "POST",
      }),
    );
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).toBe(original_url);
    expect(console_error).not.toHaveBeenCalled();
    console_error.mockRestore();
  });
});
