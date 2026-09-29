import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import AssistantConversation from "../src/features/assistant/AssistantConversation.vue";
import type {
  AssistantControl,
  AssistantControlKind,
  AssistantConversation as AssistantConversationPayload,
  AssistantJsonValue,
} from "../src/features/assistant/assistant_contracts";


interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}


afterEach(() => {
  document.body.replaceChildren();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});


function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}


function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status });
}


function control(kind: AssistantControlKind): AssistantControl {
  return {
    kind,
    handle: `control.${kind}.1`,
    safe_label: `点击-${kind}`,
  };
}


function conversation(
  generation: number,
  options: {
    active?: boolean;
    controls?: AssistantControl[];
    display_payload?: AssistantJsonValue;
    teacher_utterance?: string;
  } = {},
): AssistantConversationPayload {
  return {
    version: "1",
    conversation_ref: "conversation.1",
    generation,
    active_run: options.active
      ? { turn_ref: `turn.${generation}`, run_ref: `run.${generation}`, generation }
      : null,
    turns: generation === 0
      ? []
      : [
          {
            turn_ref: `turn.${generation}`,
            run_ref: `run.${generation}`,
            generation,
            teacher_utterance: options.teacher_utterance ?? "老师原话",
            visible_state: options.active ? "active" : "completed",
            display_kind: options.active ? null : "text",
            display_payload: options.active
              ? null
              : options.display_payload ?? { text: "小静回复" },
            controls: options.controls ?? [],
          },
        ],
  };
}


function mount_component(): { host: HTMLDivElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(AssistantConversation);
  app.mount(host);
  return {
    host,
    unmount: () => {
      app.unmount();
      host.remove();
    },
  };
}

it("labels continued answers as system continuation and never retries them as a user message", async () => {
  const original = conversation(1, { display_payload: { text: "正式回执保留" } });
  const resumed = conversation(2, { display_payload: { text: "回答未完成", error_code: "assistant.budget" } });
  resumed.turns[0]!.continuation_of = original.turns[0]!.turn_ref;
  resumed.turns = [...original.turns, ...resumed.turns];
  const fetcher = vi.fn().mockResolvedValue(response(resumed)); vi.stubGlobal("fetch", fetcher);
  const { host, unmount } = mount_component();
  try {
    for (let i = 0; i < 15; i++) await nextTick();
    expect(host.querySelectorAll(".user-message")).toHaveLength(1);
    expect(host.textContent).toContain("继续处理原任务");
    expect(host.textContent).toContain("正式回执保留");
    expect(host.querySelector('[data-action="retry-turn"]')).toBeNull();
    expect(host.textContent).toContain("原回执");
  } finally { unmount(); }
});


function set_input(host: HTMLElement, value: string): HTMLTextAreaElement {
  const input = host.querySelector<HTMLTextAreaElement>(
    '[data-role="assistant-input"]',
  );
  if (input === null) {
    throw new Error("expected assistant input");
  }
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return input;
}


describe("AssistantConversation", () => {
  it("offers a fresh conversation without copying old turns or interpreting it as business cancellation", async () => {
    const fresh = { ...conversation(0), conversation_ref: "conversation.2" };
    const fetch_mock = vi.fn().mockResolvedValueOnce(response(conversation(1)))
      .mockResolvedValueOnce(response(conversation(1)))
      .mockResolvedValueOnce(response(fresh));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => expect(host.querySelector('[data-action="new-conversation"]')).not.toBeNull());
      expect(host.textContent).toContain("不会撤销已提交的操作");
      set_input(host, "旧输入");
      host.querySelector<HTMLButtonElement>('[data-action="new-conversation"]')!.click();
      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(3));
      await vi.waitFor(() => expect(host.textContent).not.toContain("老师原话"));
      expect(host.querySelector<HTMLTextAreaElement>('[data-role="assistant-input"]')!.value).toBe("旧输入");
      expect(fetch_mock.mock.calls[2]).toEqual(["/api/v1/assistant/conversations", { credentials: "same-origin", method: "POST", signal: expect.any(AbortSignal) }]);
    } finally { unmount(); }
  });

  it("keeps the input visible while busy and stop is one mechanical click", async () => {
    const stop_result = deferred<Response>();
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(conversation(1, { active: true })))
      .mockReturnValueOnce(stop_result.promise);
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => {
        expect(fetch_mock).toHaveBeenCalledTimes(1);
        expect(host.querySelector('[data-action="stop"]')).not.toBeNull();
      });
      const input = set_input(host, "处理期间先写好的下一句");
      await nextTick();

      const send = host.querySelector<HTMLButtonElement>('[data-action="send"]');
      const stop = host.querySelector<HTMLButtonElement>('[data-action="stop"]');
      expect(input.value).toBe("处理期间先写好的下一句");
      expect(send?.disabled).toBe(true);
      expect(stop).not.toBeNull();

      stop?.click();
      stop?.click();
      await nextTick();
      expect(fetch_mock).toHaveBeenCalledTimes(2);
      expect(fetch_mock).toHaveBeenLastCalledWith(
        "/api/v1/assistant/conversations/conversation.1/runs/run.1/stop",
        expect.objectContaining({ body: JSON.stringify({ generation: 1 }), method: "POST" }),
      );

      stop_result.resolve(response(conversation(2)));
      await vi.waitFor(() => expect(host.querySelector('[data-action="stop"]')).toBeNull());
      expect(input.value).toBe("处理期间先写好的下一句");
    } finally {
      unmount();
    }
  });

  it("natural-language confirmation sends only the current utterance", async () => {
    const private_control = control("action_offer");
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(
        response(conversation(1, {
          controls: [private_control],
          display_payload: { text: "DISPLAY-PRIVATE-13900001111" },
        })),
      )
      .mockResolvedValueOnce(response(conversation(2, { teacher_utterance: "确认" })));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => expect(host.textContent).toContain("DISPLAY-PRIVATE"));
      set_input(host, "确认");
      await nextTick();
      host.querySelector<HTMLButtonElement>('[data-action="send"]')?.click();

      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(2));
      const [url, init] = fetch_mock.mock.calls[1] as [string, RequestInit];
      expect(url).toBe("/api/v1/assistant/conversations/conversation.1/turns");
      expect(JSON.parse(String(init.body))).toEqual({ teacher_utterance: "确认" });
      expect(String(init.body)).not.toContain("DISPLAY-PRIVATE");
      expect(String(init.body)).not.toContain(private_control.handle);
    } finally {
      unmount();
    }
  });

  it("refresh preserves unsent input and only reads the snapshot", async () => {
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(conversation(1)))
      .mockResolvedValueOnce(response(conversation(1, { display_payload: { text: "已刷新" } })));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => {
        expect(fetch_mock).toHaveBeenCalledTimes(1);
        expect(host.querySelector('[data-action="refresh"]')).not.toBeNull();
      });
      const input = set_input(host, "刷新前尚未发送的内容");
      host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.click();

      await vi.waitFor(() => expect(host.textContent).toContain("已刷新"));
      expect(input.value).toBe("刷新前尚未发送的内容");
      expect(fetch_mock).toHaveBeenLastCalledWith(
        "/api/v1/assistant/conversations/conversation.1",
        { credentials: "same-origin", method: "GET" },
      );
    } finally {
      unmount();
    }
  });

  it("disables control buttons while refresh is in flight", async () => {
    const clicked = control("recovery_control");
    const refresh_result = deferred<Response>();
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(conversation(1, { controls: [clicked] })))
      .mockReturnValueOnce(refresh_result.promise);
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => expect(host.textContent).toContain(clicked.safe_label));
      set_input(host, "忙碌期间保留的输入");
      host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.click();
      await nextTick();

      expect(host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.disabled).toBe(true);
      expect(host.querySelector<HTMLButtonElement>('[data-control-kind="recovery_control"]')?.disabled).toBe(true);

      host.querySelector<HTMLButtonElement>('[data-control-kind="recovery_control"]')?.click();
      expect(fetch_mock).toHaveBeenCalledTimes(2);

      refresh_result.resolve(response(conversation(1, { controls: [clicked] })));
      await vi.waitFor(() => {
        expect(host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.disabled).toBe(false);
      });
    } finally {
      unmount();
    }
  });

  it("disables refresh while a control request is in flight", async () => {
    const clicked = control("action_offer");
    const control_result = deferred<Response>();
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(conversation(1, { controls: [clicked] })))
      .mockReturnValueOnce(control_result.promise);
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => expect(host.textContent).toContain(clicked.safe_label));
      host.querySelector<HTMLButtonElement>('[data-control-kind="action_offer"]')?.click();
      await nextTick();

      expect(host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.disabled).toBe(true);
      host.querySelector<HTMLButtonElement>('[data-action="refresh"]')?.click();
      expect(fetch_mock).toHaveBeenCalledTimes(2);

      control_result.resolve(response(conversation(1, { display_payload: { text: "点击完成" } })));
      await vi.waitFor(() => expect(host.textContent).toContain("点击完成"));
    } finally {
      unmount();
    }
  });

  it.each<[AssistantControlKind, string]>([
    ["action_offer", "action-offers"],
    ["recovery_control", "recovery-controls"],
    ["result_check_handle", "result-checks"],
  ])("executes %s only through its real button", async (kind, route) => {
    const clicked = control(kind);
    const click_result = deferred<Response>();
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(conversation(1, { controls: [clicked] })))
      .mockReturnValueOnce(click_result.promise);
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      await vi.waitFor(() => expect(host.textContent).toContain(clicked.safe_label));
      const button = host.querySelector<HTMLButtonElement>(
        `[data-control-kind="${kind}"]`,
      );
      button?.click();
      button?.click();
      await nextTick();

      expect(fetch_mock).toHaveBeenCalledTimes(2);
      expect(fetch_mock).toHaveBeenLastCalledWith(
        `/api/v1/assistant/conversations/conversation.1/controls/${route}/${clicked.handle}`,
        { credentials: "same-origin", method: "POST" },
      );
      click_result.resolve(response(conversation(1, { display_payload: { text: "点击完成" } })));
      await vi.waitFor(() => expect(host.textContent).toContain("点击完成"));
      expect(host.textContent).not.toContain(clicked.safe_label);
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);
    } finally {
      unmount();
    }
  });
});


it("shows navigation uncertainty after a close timeout and retains the draft until explicit recovery", async () => {
  const slow = deferred<Response>();
  const fetch_mock = vi.fn().mockResolvedValueOnce(response(conversation(1)))
    .mockReturnValueOnce(slow.promise).mockResolvedValueOnce(response(conversation(1)))
    .mockResolvedValueOnce(response({ ...conversation(0), conversation_ref: "conversation.2" }));
  vi.stubGlobal("fetch", fetch_mock); const { host, unmount } = mount_component();
  try {
    await vi.waitFor(() => expect(host.querySelector('[data-action="new-conversation"]')).not.toBeNull());
    vi.useFakeTimers(); set_input(host, "恢复期间草稿");
    const fresh = host.querySelector<HTMLButtonElement>('[data-action="new-conversation"]')!;
    fresh.click(); await nextTick(); expect(fresh.disabled).toBe(true);
    expect(host.querySelector(".assistant-conversation")?.getAttribute("data-navigation-phase")).toBe("closing");
    await vi.advanceTimersByTimeAsync(15_001); await nextTick();
    expect(fresh.disabled).toBe(false); expect(host.querySelector('[role="alert"]')?.textContent).toContain("尚未确认");
    expect(host.querySelector(".assistant-conversation")?.getAttribute("data-navigation-phase")).toBe("close_failed");
    expect(host.querySelector<HTMLButtonElement>('[data-action="send"]')!.disabled).toBe(true);
    expect(host.querySelectorAll('[data-turn-ref]')).toHaveLength(1);
    expect(host.querySelector<HTMLTextAreaElement>('[data-role="assistant-input"]')!.value).toBe("恢复期间草稿");
    fresh.click(); await vi.advanceTimersByTimeAsync(1); await nextTick();
    expect(host.querySelectorAll('[data-turn-ref]')).toHaveLength(0);
    expect(host.querySelector<HTMLButtonElement>('[data-action="send"]')!.disabled).toBe(false);
    slow.resolve(response(conversation(1))); await vi.advanceTimersByTimeAsync(1); await nextTick();
    expect(host.querySelectorAll('[data-turn-ref]')).toHaveLength(0);
  } finally { unmount(); vi.useRealTimers(); }
});
