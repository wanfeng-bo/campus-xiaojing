import { createApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import HealthApp from "../src/app/HealthApp.vue";


afterEach(() => {
  vi.unstubAllGlobals();
});


function mountHealthApp(): { host: HTMLDivElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.append(host);

  const app = createApp(HealthApp);
  app.mount(host);

  return {
    host,
    unmount: () => {
      app.unmount();
      host.remove();
    },
  };
}


describe("HealthApp", () => {
  it("shows the available state after a successful health request", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok", database: "ok" }), { status: 200 }),
      ),
    );

    const { host, unmount } = mountHealthApp();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain("服务和数据库连接正常。");
      });
    } finally {
      unmount();
    }
  });

  it("shows a retry action after a failed health request", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 503 })));

    const { host, unmount } = mountHealthApp();
    try {
      await vi.waitFor(() => {
        expect(host.textContent).toContain("暂时无法连接服务，请稍后重试。");
        expect(host.querySelector("button")?.textContent).toBe("重新检查");
      });
    } finally {
      unmount();
    }
  });
});
