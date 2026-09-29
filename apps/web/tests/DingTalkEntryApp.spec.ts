import { createApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

const { request_auth_code } = vi.hoisted(() => ({
  request_auth_code: vi.fn(),
}));

vi.mock("dingtalk-jsapi/api/union/requestAuthCode", () => ({
  default: { requestAuthCode$: request_auth_code },
  requestAuthCode$: request_auth_code,
}));

import DingTalkEntryApp from "../src/app/DingTalkEntryApp.vue";


afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  request_auth_code.mockReset();
});


function mount_dingtalk_entry_app(): { host: HTMLDivElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.append(host);

  const app = createApp(DingTalkEntryApp);
  app.mount(host);

  return {
    host,
    unmount: () => {
      app.unmount();
      host.remove();
    },
  };
}


function stub_public_dingtalk_configuration(): void {
  vi.stubEnv("VITE_DINGTALK_CORP_ID", "ding-test-corp");
  vi.stubEnv("VITE_DINGTALK_CLIENT_ID", "ding-test-client");
}


describe("DingTalkEntryApp", () => {
  it.each(["loading", "error", "pending_configuration", "ready"])(
    "%s 状态持续显示个人技术展示测试标记和备案链接",
    async (state) => {
      stub_public_dingtalk_configuration();
      vi.stubEnv("VITE_ICP_FILING_NUMBER", "测试ICP备案号");
      if (state === "loading") request_auth_code.mockReturnValue(new Promise(() => {}));
      else if (state === "error") request_auth_code.mockRejectedValue(new Error("测试错误"));
      else request_auth_code.mockResolvedValue({ code: "test-only-code" });
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
        access_state: state, can_manage_system: false,
      }), { status: 200 })));
      const { host, unmount } = mount_dingtalk_entry_app();
      try {
        await vi.waitFor(() => {
          expect(host.textContent).toContain("个人技术展示 · 测试站");
          expect(host.textContent).toContain("不用于正式学校业务办理");
          expect(host.querySelector('a[href="https://beian.miit.gov.cn/"]')?.textContent)
            .toContain("测试ICP备案号");
          if (state === "error") expect(host.textContent).toContain("重新验证");
          if (state === "ready") expect(host.textContent).toContain("当前暂无正式启用模块");
          if (state === "pending_configuration") expect(host.textContent).toContain("尚未配置系统权限");
        });
      } finally { unmount(); }
    },
  );
  it("配置备案号时在首页显示工信部备案链接", async () => {
    stub_public_dingtalk_configuration();
    vi.stubEnv("VITE_ICP_FILING_NUMBER", "测试ICP备案号");
    request_auth_code.mockResolvedValue({ code: "one-time-code" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ access_state: "ready", can_manage_system: false }),
          { status: 200 },
        ),
      ),
    );

    const { host, unmount } = mount_dingtalk_entry_app();
    try {
      await vi.waitFor(() => {
        const filing_link = host.querySelector<HTMLAnchorElement>(
          'a[href="https://beian.miit.gov.cn/"]',
        );
        expect(filing_link?.textContent).toBe("测试ICP备案号");
        expect(filing_link?.target).toBe("_blank");
        expect(filing_link?.rel).toContain("noopener");
      });
    } finally {
      unmount();
    }
  });

  it("备案号为空时不渲染工信部备案链接", async () => {
    stub_public_dingtalk_configuration();
    vi.stubEnv("VITE_ICP_FILING_NUMBER", "");
    request_auth_code.mockResolvedValue({ code: "one-time-code" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ access_state: "ready", can_manage_system: false }),
          { status: 200 },
        ),
      ),
    );

    const { host, unmount } = mount_dingtalk_entry_app();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain("身份验证完成。");
      });
      expect(
        host.querySelector('a[href="https://beian.miit.gov.cn/"]'),
      ).toBeNull();
    } finally {
      unmount();
    }
  });

  it("未配置角色时显示联系系统管理员提示", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockResolvedValue({ code: "one-time-code" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ access_state: "pending_configuration", can_manage_system: false }),
          { status: 200 },
        ),
      ),
    );

    const { host, unmount } = mount_dingtalk_entry_app();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain(
          "当前钉钉身份尚未配置系统权限，请联系系统管理员。",
        );
      });
    } finally {
      unmount();
    }
  });

  it("重新验证时申请新的免登码，不重复使用失败的 code", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code
      .mockResolvedValueOnce({ code: "expired-code" })
      .mockResolvedValueOnce({ code: "fresh-code" });
    const fetch_mock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "登录凭证已失效，请重新从钉钉工作台进入。" }), {
          status: 401,
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ access_state: "ready", can_manage_system: false }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch_mock);

    const { host, unmount } = mount_dingtalk_entry_app();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain("登录凭证已失效，请重新从钉钉工作台进入。");
      });

      host.querySelector<HTMLButtonElement>("button")?.click();

      await vi.waitFor(() => {
        expect(host.textContent).toContain("身份验证完成。");
      });
      expect(request_auth_code).toHaveBeenNthCalledWith(
        1,
        expect.objectContaining({
          corpId: "ding-test-corp",
          clientId: "ding-test-client",
          success: expect.any(Function),
          fail: expect.any(Function),
        }),
      );
      expect(request_auth_code).toHaveBeenNthCalledWith(
        2,
        expect.objectContaining({
          corpId: "ding-test-corp",
          clientId: "ding-test-client",
          success: expect.any(Function),
          fail: expect.any(Function),
        }),
      );
      expect(fetch_mock).toHaveBeenNthCalledWith(
        1,
        "/api/v1/auth/dingtalk/exchange",
        expect.objectContaining({ body: JSON.stringify({ auth_code: "expired-code" }) }),
      );
      expect(fetch_mock).toHaveBeenNthCalledWith(
        2,
        "/api/v1/auth/dingtalk/exchange",
        expect.objectContaining({ body: JSON.stringify({ auth_code: "fresh-code" }) }),
      );
    } finally {
      unmount();
    }
  });
  it("retires the legacy Repairs product entry after identity is ready", async () => {
    stub_public_dingtalk_configuration();
    request_auth_code.mockResolvedValue({ code: "one-time-code" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ access_state: "ready", can_manage_system: false }),
          { status: 200 },
        ),
      ),
    );

    const { host, unmount } = mount_dingtalk_entry_app();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain("当前暂无正式启用模块");
      });
      expect(host.querySelector('[data-testid="xiaojing-app"]')).toBeNull();
      expect(host.textContent).not.toContain("小静报修入口");
    } finally {
      unmount();
    }
  });

});
