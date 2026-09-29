import { createApp, h, nextTick, ref } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import AssistantConversation from "../src/features/assistant/AssistantConversation.vue";
import type { AssistantConversation as Snapshot } from "../src/features/assistant/assistant_contracts";

interface ConversationUi {
  openConversations(): void;
  newConversation(): Promise<boolean>;
  focusInput(): void;
  continueOperation(reference: string): Promise<boolean>;
}

const cleanups: (() => void)[] = [];
afterEach(() => {
  cleanups.splice(0).forEach(cleanup => cleanup());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

function snapshot(messages: string[], reference = "conversation.current"): Snapshot {
  return {
    version: "1", conversation_ref: reference, generation: messages.length, active_run: null,
    turns: messages.map((message, index) => ({
      turn_ref: `turn.${index + 1}`, run_ref: `run.${index + 1}`, generation: index + 1,
      teacher_utterance: message, visible_state: "completed", display_kind: "text",
      display_payload: { text: `回答 ${index + 1}` }, controls: [],
    })),
  };
}

const response = (value: Snapshot) => new Response(JSON.stringify(value));

function mount() {
  const invalid = ref(false);
  const component = ref<ConversationUi>();
  const activity = vi.fn();
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp({ setup: () => () => h(AssistantConversation, {
    ref: component, identityInvalid: invalid.value, onActivityChange: activity,
  }, { home: () => h("div", { "data-test": "home" }, "首页快捷入口") }) });
  app.mount(host);
  cleanups.push(() => app.unmount());
  return { host, invalid, component, activity };
}

it("exposes shell controls and keeps the home slot only until actual conversation activity", async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const fetcher = vi.fn().mockResolvedValueOnce(response(snapshot([]))).mockReturnValueOnce(pending);
  vi.stubGlobal("fetch", fetcher);
  const { host, component, activity } = mount();
  await vi.waitFor(() => expect(host.querySelector('[data-action="new-conversation"]')).not.toBeNull());
  expect(activity).toHaveBeenLastCalledWith(false);
  expect(host.querySelector('[data-test="home"]')).not.toBeNull();
  expect(component.value?.continueOperation).toBeTypeOf("function");
  expect(component.value?.focusInput).toBeTypeOf("function");
  component.value!.focusInput();
  await nextTick();
  const input = host.querySelector<HTMLTextAreaElement>('[data-role="assistant-input"]')!;
  expect(document.activeElement).toBe(input);
  input.value = "待发送的问题";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick();
  host.querySelector<HTMLButtonElement>('[data-action="send"]')!.click();
  await nextTick();
  expect(activity).toHaveBeenLastCalledWith(true);
  expect(host.querySelector('[data-test="home"]')).toBeNull();
  expect(host.textContent).toContain("尚未确认送达");
  finish(response(snapshot(["待发送的问题"])));
  await vi.waitFor(() => expect(host.textContent).toContain("回答 1"));
});

it("finds only current questions and locates a message without any request or stored history", async () => {
  const current = snapshot(["第一项问题", "第二项问题", "系统续接不应作为新问题"]);
  current.turns[2]!.continuation_of = "turn.2";
  const fetcher = vi.fn().mockResolvedValue(response(current));
  vi.stubGlobal("fetch", fetcher);
  const { host, component } = mount();
  await vi.waitFor(() => expect(host.querySelectorAll('[data-turn-ref]')).toHaveLength(3));
  expect(component.value?.openConversations).toBeTypeOf("function");
  component.value!.openConversations();
  await nextTick();
  const drawer = host.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(drawer.textContent).toContain("当前对话");
  expect(drawer.textContent).toContain("24小时");
  expect(drawer.textContent).not.toContain("系统续接不应作为新问题");
  const search = drawer.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = "第二项";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick();
  const questions = drawer.querySelectorAll<HTMLButtonElement>('[data-action="locate-turn"]');
  expect(questions).toHaveLength(1);
  const target = host.querySelector<HTMLElement>('[data-turn-ref="turn.2"]')!;
  const scroll = vi.fn();
  target.scrollIntoView = scroll;
  questions[0]!.click();
  await nextTick();
  expect(scroll).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(target);
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

it("traps keyboard focus, closes on Escape, restores the opener and resets the search", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(snapshot(["当前问题"]))));
  const { host, component } = mount();
  await vi.waitFor(() => expect(host.querySelector('[data-turn-ref]')).not.toBeNull());
  const opener = document.createElement("button");
  document.body.prepend(opener);
  opener.focus();
  expect(component.value?.openConversations).toBeTypeOf("function");
  component.value!.openConversations();
  await nextTick();
  const drawer = host.querySelector<HTMLElement>('[role="dialog"]')!;
  const search = drawer.querySelector<HTMLInputElement>('input[type="search"]')!;
  expect(document.activeElement).toBe(search);
  search.value = "不存在";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick();
  const controls = drawer.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)');
  controls[controls.length - 1]!.focus();
  controls[controls.length - 1]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
  expect(document.activeElement).toBe(controls[0]);
  drawer.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await nextTick();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(opener);
  component.value!.openConversations();
  await nextTick();
  expect(host.querySelector<HTMLInputElement>('input[type="search"]')!.value).toBe("");
});

it("hides the current-question index immediately when the identity becomes invalid", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(snapshot(["当前问题"]))));
  const { host, component, invalid } = mount();
  await vi.waitFor(() => expect(host.querySelector('[data-turn-ref]')).not.toBeNull());
  expect(component.value?.openConversations).toBeTypeOf("function");
  component.value!.openConversations();
  await nextTick();
  expect(host.querySelector('[role="dialog"]')).not.toBeNull();
  invalid.value = true;
  await nextTick();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  component.value!.openConversations();
  await nextTick();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(host.querySelector<HTMLButtonElement>('[data-action="send"]')!.disabled).toBe(true);
});

it("keeps a current search open when a same-conversation refresh completes", async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response(snapshot(["当前问题"]))).mockReturnValueOnce(pending));
  const { host, component } = mount();
  await vi.waitFor(() => expect(host.querySelector('[data-turn-ref]')).not.toBeNull());
  host.querySelector<HTMLButtonElement>('[data-action="refresh"]')!.click();
  component.value!.openConversations();
  await nextTick();
  const search = host.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = "当前";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  finish(response(snapshot(["当前问题", "另一项问题"])));
  await vi.waitFor(() => expect(host.querySelectorAll('[data-turn-ref]')).toHaveLength(2));
  expect(host.querySelector('[role="dialog"]')).not.toBeNull();
  expect(host.querySelector<HTMLInputElement>('input[type="search"]')!.value).toBe("当前");
  expect(host.querySelectorAll('[data-action="locate-turn"]')).toHaveLength(1);
});

it("new conversation uses the existing close/create flow and exposes no previous questions", async () => {
  const original = snapshot(["旧问题"]);
  const fetcher = vi.fn().mockResolvedValueOnce(response(original))
    .mockResolvedValueOnce(response(original))
    .mockResolvedValueOnce(response(snapshot([], "conversation.next")));
  vi.stubGlobal("fetch", fetcher);
  const { host, component, activity } = mount();
  await vi.waitFor(() => expect(host.querySelector('[data-turn-ref]')).not.toBeNull());
  expect(component.value?.newConversation).toBeTypeOf("function");
  component.value!.openConversations();
  await nextTick();
  await component.value!.newConversation();
  await nextTick();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(activity).toHaveBeenLastCalledWith(false);
  expect(host.querySelector('[data-test="home"]')).not.toBeNull();
  expect(fetcher.mock.calls.map(call => call[0])).toEqual([
    "/api/v1/assistant/conversations", "/api/v1/assistant/conversations/conversation.current/close", "/api/v1/assistant/conversations",
  ]);
  component.value!.openConversations();
  await nextTick();
  expect(host.querySelector('[role="dialog"]')?.textContent).not.toContain("旧问题");
  expect(host.querySelectorAll('[data-action="locate-turn"]')).toHaveLength(0);
});
