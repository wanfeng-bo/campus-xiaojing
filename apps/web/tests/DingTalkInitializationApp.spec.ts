import { createApp, nextTick } from "vue";
import { afterEach, expect, it, vi } from "vitest";

const loaders = import.meta.glob("../src/app/DingTalkInitializationApp.vue");
const { authCode } = vi.hoisted(() => ({ authCode: vi.fn() }));
vi.mock("../src/app/dingtalk", () => ({ requestDingTalkAuthCode: authCode }));
const mounts: Array<() => void> = [];
afterEach(() => { mounts.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.unstubAllGlobals(); authCode.mockReset(); });
async function mount() {
  const loader = loaders["../src/app/DingTalkInitializationApp.vue"];
  expect(loader, "a separate temporary identity handoff page must exist").toBeTypeOf("function");
  const { default: Entry } = await loader() as {default: any};
  const host = document.createElement("div");
  const app = createApp(Entry); app.mount(host);
  let active = true; const unmount = () => { if (active) app.unmount(); active = false; };
  mounts.push(unmount);
  return { host, unmount, send: () => (host.querySelector("button") as HTMLButtonElement).click() };
}
const reply = (status: number, value: unknown) => new Response(JSON.stringify(value), { status });

it("requires an explicit click and delivers once without exposing authentication material", async () => {
  authCode.mockResolvedValue("fixture-code-not-for-display");
  const fetcher = vi.fn(async () => reply(200, { status: "identity_delivered" })); vi.stubGlobal("fetch", fetcher);
  const page = await mount();
  expect(authCode).not.toHaveBeenCalled(); expect(fetcher).not.toHaveBeenCalled();
  page.send(); page.send();
  await vi.waitFor(() => expect(page.host.textContent).toContain("身份已核验"));
  expect(page.host.textContent).toContain("等待资料配置");
  expect(page.host.textContent).not.toContain("fixture-code");
  expect(authCode).toHaveBeenCalledTimes(1); expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith("/api/v1/school-fact-initialization/dingtalk/exchange", expect.objectContaining({
    method: "POST", credentials: "same-origin", body: JSON.stringify({auth_code: "fixture-code-not-for-display"}),
  }));
  expect(page.host.querySelector("textarea")).toBeNull();
});

it.each([
  [403, { detail: "private server details" }, "本次身份核验入口已关闭"],
  [409, { detail: "private server details" }, "请先核对原交付结果"],
  [401, { detail: "private server details" }, "未完成身份验证"],
  [429, { detail: "private server details" }, "操作较频繁"],
  [503, { detail: "private server details" }, "暂时无法核实交付结果"],
  [200, { status: "authenticated" }, "暂时无法核实交付结果"],
])("does not confuse status %s with completed delivery", async (status, body, expected) => {
  authCode.mockResolvedValue("fixture-code"); const fetcher = vi.fn(async () => reply(status, body)); vi.stubGlobal("fetch", fetcher);
  const page = await mount(); page.send();
  await vi.waitFor(() => expect(page.host.textContent).toContain(expected));
  expect(page.host.textContent).not.toContain("private server details");
  expect(page.host.textContent).not.toContain("身份已核验");
  page.send(); expect(fetcher).toHaveBeenCalledTimes(1);
});

it("does not retry or claim failure of the server transaction after a network interruption", async () => {
  authCode.mockResolvedValue("fixture-code"); const fetcher = vi.fn(async () => { throw new Error("sensitive failure"); }); vi.stubGlobal("fetch", fetcher);
  const page = await mount(); page.send();
  await vi.waitFor(() => expect(page.host.textContent).toContain("暂时无法核实交付结果"));
  expect(page.host.textContent).not.toContain("sensitive failure"); page.send(); expect(fetcher).toHaveBeenCalledTimes(1);
});

it("does not send a code arriving after timeout or unmount", async () => {
  let complete!: (code: string) => void;
  authCode.mockReturnValue(new Promise<string>(resolve => { complete = resolve; }));
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  const page = await mount(); vi.useFakeTimers(); page.send();
  await vi.advanceTimersByTimeAsync(30_000); await nextTick();
  expect(page.host.textContent).toContain("钉钉身份响应超时");
  complete("too-late"); await Promise.resolve(); await nextTick(); expect(fetcher).not.toHaveBeenCalled();
  page.unmount();
});

it("bounds waiting for delivery and discards a late success", async () => {
  authCode.mockResolvedValue("fixture-code"); let complete!: (response: Response) => void;
  const fetcher = vi.fn(() => new Promise<Response>(resolve => { complete = resolve; })); vi.stubGlobal("fetch", fetcher);
  const page = await mount(); vi.useFakeTimers(); page.send(); await Promise.resolve(); await nextTick();
  await vi.advanceTimersByTimeAsync(30_000); await nextTick();
  expect(page.host.textContent).toContain("暂时无法核实交付结果");
  expect((fetcher.mock.calls[0] as unknown as [string, RequestInit])[1].signal?.aborted).toBe(true);
  complete(reply(200, { status: "identity_delivered" })); await Promise.resolve(); await nextTick();
  expect(page.host.textContent).not.toContain("身份已核验");
});

it("unmount during authentication never starts a delivery", async () => {
  let complete!: (code: string) => void; authCode.mockReturnValue(new Promise(resolve => { complete = resolve; }));
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  const page = await mount(); page.send(); page.unmount(); complete("late"); await Promise.resolve(); await nextTick();
  expect(fetcher).not.toHaveBeenCalled();
});
