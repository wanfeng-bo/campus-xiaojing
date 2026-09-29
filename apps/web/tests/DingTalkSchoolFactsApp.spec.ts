import { createApp } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import SchoolFactsApp from "../src/app/SchoolFactsApp.vue";
const loaders = import.meta.glob("../src/app/DingTalkSchoolFactsApp.vue");
const { authCode } = vi.hoisted(() => ({ authCode: vi.fn() }));
vi.mock("../src/app/dingtalk", () => ({requestDingTalkAuthCode: authCode}));
afterEach(() => { vi.unstubAllGlobals(); authCode.mockReset(); });
it("formal product with an enabled assistant does not expose engineering accounts or synthetic data notices", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => response(url.endsWith("/features") ? { assistant_enabled: true } : {
    status: "signed_out", accounts: [], contexts: [], selected_context_ref: null, can_maintain: false,
  })));
  const host = document.createElement("div");
  const app = createApp(SchoolFactsApp, { formal: true });
  app.mount(host);
  try {
    await vi.waitFor(() => expect(host.textContent).toContain("从钉钉重新进入"));
    expect(host.textContent).not.toContain("工程身份");
    expect(host.querySelector("#engineering-account")).toBeNull();
    expect(host.textContent).not.toContain("合成学校");
    expect(host.textContent).toContain("本次提问和必要的获准学校资料会发送给DeepSeek");
    expect(host.textContent).toContain("请勿输入证件号码、私人电话、详细住址、密码或家庭敏感情况");
  } finally { app.unmount(); }
});
it("formal DingTalk entry is available separately from the old identity test entry", () => {
  expect(loaders["../src/app/DingTalkSchoolFactsApp.vue"]).toBeTypeOf("function");
});

const signedOut = { status: "signed_out", accounts: [], contexts: [], selected_context_ref: null, can_maintain: false };
const unbound = { ...signedOut, status: "signed_in" };
function response(value: unknown) { return new Response(JSON.stringify(value), {status: 200}); }
const signedIn = { ...unbound, contexts: [{ref: "context.staff", label: "当前任职"}], selected_context_ref: "context.staff" };
it.each([
  ["disabled", {assistant_enabled: false}],
  ["missing", {}],
  ["invalid", {assistant_enabled: "true"}],
  ["HTTP failure", new Response("unavailable", {status: 503})],
  ["network failure", new Error("offline")],
])("formal product keeps assistant closed when feature response is %s", async (_label, features) => {
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/features")) {
      if (features instanceof Error) throw features;
      return features instanceof Response ? features : response(features);
    }
    if (url.endsWith("/session")) return response(signedIn);
    return response({});
  });
  vi.stubGlobal("fetch", fetcher);
  const host = document.createElement("div"); const app = createApp(SchoolFactsApp, {formal: true}); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.querySelector('[data-action="query-contacts"]')).not.toBeNull());
    expect(host.textContent).toContain(_label === "disabled" ? "小静尚未启用，可继续查看获准资料并办理有权限的维护。" : "暂时无法确认小静是否可用，请刷新后重试。");
    if (_label !== "disabled") expect(host.textContent).not.toContain("小静尚未启用");
    expect(host.querySelector('[data-role="assistant-input"]')).toBeNull();
    expect(host.textContent).not.toContain("发送给DeepSeek");
    expect(fetcher.mock.calls.some(([url]) => url.includes("/assistant/"))).toBe(false);
  } finally { app.unmount(); }
});
it("formal product waits for explicit assistant enablement before opening a conversation", async () => {
  let resolveFeatures!: (response: Response) => void;
  const features = new Promise<Response>(resolve => { resolveFeatures = resolve; });
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/features")) return features;
    if (url.endsWith("/session")) return response(signedIn);
    return response({version: "1", conversation_ref: "conversation.1", generation: 0, active_run: null, turns: []});
  });
  vi.stubGlobal("fetch", fetcher);
  const host = document.createElement("div"); const app = createApp(SchoolFactsApp, {formal: true}); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.querySelector('[data-action="query-contacts"]')).not.toBeNull());
    expect(host.textContent).toContain("正在核对小静是否可用");
    expect(host.textContent).not.toContain("小静尚未启用");
    expect(host.querySelector('[data-role="assistant-input"]')).toBeNull();
    expect(fetcher.mock.calls.some(([url]) => url.includes("/assistant/"))).toBe(false);
    resolveFeatures(response({assistant_enabled: true}));
    await vi.waitFor(() => expect(host.querySelector('[data-role="assistant-input"]')).not.toBeNull());
    expect(host.textContent).toContain("发送给DeepSeek");
    expect(host.textContent).not.toContain("小静尚未启用");
  } finally { app.unmount(); }
});
it("formal parent view does not invite assistant questions while it is disabled", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => response(url.endsWith("/session") ? {
    ...unbound, family_enabled: true, family_conversation_available: true, assistant_context_key: "family.1",
    contexts: [{ref: "child.1", label: "孩子", kind: "guardian_relationship"}],
  } : {assistant_enabled: false})));
  const host = document.createElement("div"); const app = createApp(SchoolFactsApp, {formal: true}); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.textContent).toContain("选择孩子查看资料卡"));
    expect(host.textContent).toContain("如需查看孩子资料卡，请在上方选择孩子");
    expect(host.textContent).not.toContain("向小静");
    expect(host.querySelector('[data-role="assistant-input"]')).toBeNull();
  } finally { app.unmount(); }
});
it("DingTalk verification enters the formal product and shows missing mapping without account selection", async () => {
  let exchanged = false;
  authCode.mockResolvedValue("single-use-code");
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/dingtalk/exchange")) { exchanged = true; return response(unbound); }
    if (url.endsWith("/session")) return response(exchanged ? unbound : signedOut);
    return response({});
  });
  vi.stubGlobal("fetch", fetcher);
  const { default: Entry } = await import("../src/app/DingTalkSchoolFactsApp.vue");
  const host = document.createElement("div"); const app = createApp(Entry); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.textContent).toContain("尚未完成绑定"));
    expect(host.textContent).not.toContain("工程身份");
    expect(host.querySelector("#engineering-account")).toBeNull();
    expect(authCode).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/api/v1/school-fact-product/dingtalk/exchange", expect.objectContaining({
      method: "POST", credentials: "same-origin", body: JSON.stringify({auth_code: "single-use-code"}),
    }));
  } finally { app.unmount(); }
});
it("legacy ready response cannot admit a formal product user", async () => {
  authCode.mockResolvedValue("new-code");
  vi.stubGlobal("fetch", vi.fn(async (url: string) => response(url.endsWith("/session") ? signedOut : {access_state:"ready",can_manage_system:true})));
  const { default: Entry } = await import("../src/app/DingTalkSchoolFactsApp.vue");
  const host = document.createElement("div"); const app = createApp(Entry); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.textContent).toContain("身份验证未完成"));
    expect(host.querySelector(".school-facts-app")).toBeNull();
  } finally { app.unmount(); }
});
it("a current formal session resumes without consuming another DingTalk code", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response(unbound)));
  const { default: Entry } = await import("../src/app/DingTalkSchoolFactsApp.vue");
  const host = document.createElement("div"); const app = createApp(Entry); app.mount(host);
  try {
    await vi.waitFor(() => expect(host.textContent).toContain("尚未完成绑定"));
    expect(authCode).not.toHaveBeenCalled();
  } finally { app.unmount(); }
});
it("unmount during DingTalk authentication discards the late code", async () => {
  let resolveCode!: (value: string) => void;
  authCode.mockReturnValue(new Promise(resolve => {resolveCode = resolve;}));
  const fetcher = vi.fn(async () => response(signedOut)); vi.stubGlobal("fetch", fetcher);
  const { default: Entry } = await import("../src/app/DingTalkSchoolFactsApp.vue");
  const host = document.createElement("div"); const app = createApp(Entry); app.mount(host);
  await vi.waitFor(() => expect(authCode).toHaveBeenCalledTimes(1));
  app.unmount(); resolveCode("too-late"); await Promise.resolve(); await Promise.resolve();
  expect(fetcher).toHaveBeenCalledTimes(1);
});
