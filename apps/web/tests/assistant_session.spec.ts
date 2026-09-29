import { AssistantApiError } from "../src/features/assistant/assistant_api";
import { describe, expect, it, vi } from "vitest";

import {
  createAssistantSessionController,
  type AssistantSessionApi,
} from "../src/features/assistant/assistant_session";
import type {
  AssistantControl,
  AssistantControlKind,
  AssistantConversation,
} from "../src/features/assistant/assistant_contracts";


interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}


function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, reject, resolve };
}


function control(kind: AssistantControlKind): AssistantControl {
  return {
    kind,
    handle: `control.${kind}.1`,
    safe_label: `安全按钮-${kind}`,
  };
}


function snapshot(
  generation: number,
  options: {
    active?: boolean;
    controls?: AssistantControl[];
    display_text?: string;
  } = {},
): AssistantConversation {
  const has_turn = generation > 0;
  return {
    version: "1",
    conversation_ref: "conversation.1",
    generation,
    active_run: options.active && has_turn
      ? { turn_ref: `turn.${generation}`, run_ref: `run.${generation}`, generation }
      : null,
    turns: has_turn
      ? [
          {
            turn_ref: `turn.${generation}`,
            run_ref: `run.${generation}`,
            generation,
            teacher_utterance: `第${generation}句老师原话`,
            visible_state: options.active ? "active" : "completed",
            display_kind: options.active ? null : "text",
            display_payload: options.active
              ? null
              : { text: options.display_text ?? `第${generation}次回复` },
            controls: options.controls ?? [],
          },
        ]
      : [],
  };
}


function fake_api(open_snapshot = snapshot(0)): {
  api: AssistantSessionApi;
  createConversation: ReturnType<typeof vi.fn>;
  executeControl: ReturnType<typeof vi.fn>;
  fetchConversation: ReturnType<typeof vi.fn>;
  sendTurn: ReturnType<typeof vi.fn>;
  stopRun: ReturnType<typeof vi.fn>;
} {
  const createConversation = vi.fn().mockResolvedValue(open_snapshot);
  const executeControl = vi.fn();
  const fetchConversation = vi.fn();
  const sendTurn = vi.fn();
  const stopRun = vi.fn();
  return {
    api: {
      createConversation,
      executeControl,
      fetchConversation,
      sendTurn,
      stopRun,
    },
    createConversation,
    executeControl,
    fetchConversation,
    sendTurn,
    stopRun,
  };
}


describe("assistant mechanical session controller", () => {
  it("uses a switch claim for initial open only, never for refresh or the next conversation", async () => {
    const { api, createConversation, fetchConversation } = fake_api();
    const fresh = { ...snapshot(0), conversation_ref: "conversation.2" };
    createConversation.mockResolvedValueOnce(snapshot(0)).mockResolvedValueOnce(fresh);
    fetchConversation.mockResolvedValue(snapshot(0));
    const controller = createAssistantSessionController(api);
    expect(await controller.open("C".repeat(43))).toBe(true);
    expect(createConversation.mock.calls[0]?.[1]).toBe("C".repeat(43));
    expect(await controller.refresh()).toBe(true);
    expect(await controller.newConversation()).toBe(true);
    expect(createConversation).toHaveBeenCalledTimes(2);
    expect(createConversation.mock.calls[1]?.[1]).toBeUndefined();
  });

  function prepared() {
    const value = snapshot(1, { controls: [control("action_offer")] });
    value.turns[0]!.display_payload = { text: "待确认", continuations: [{ handle: "continuation.1", proposal_ref: "proposal.1" }] };
    return value;
  }

  it("continues a matching maintenance result once without sending input or changing the draft", async () => {
    const { api, sendTurn } = fake_api(prepared());
    const pending = deferred<AssistantConversation>();
    const continueOperation = vi.fn().mockReturnValue(pending.promise);
    const controller = createAssistantSessionController({ ...api, continueOperation });
    await controller.open(); controller.setInput("下一条草稿");
    expect(await controller.continueOperation("manual.no-chat")).toBe(false);
    const running = controller.continueOperation("proposal.1");
    expect(continueOperation).toHaveBeenCalledWith("conversation.1", "continuation.1", expect.any(Function), expect.any(AbortSignal));
    expect(controller.getState().pending_message).toBeNull();
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    const accepted = snapshot(2, { active: true }); accepted.turns[0]!.continuation_of = "turn.1";
    continueOperation.mock.calls[0]![2](accepted);
    expect(controller.getState().snapshot?.turns).toHaveLength(2);
    pending.resolve({ ...snapshot(2), turns: [{ ...snapshot(2).turns[0]!, continuation_of: "turn.1" }] });
    expect(await running).toBe(true);
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(sendTurn).not.toHaveBeenCalled();
    expect(controller.getState().input).toBe("下一条草稿");
  });

  it("automatically uses the same continuation after the formal chat control snapshot", async () => {
    const value = prepared();
    const { api, executeControl, sendTurn } = fake_api(value);
    executeControl.mockResolvedValue({ ...value, turns: [{ ...value.turns[0]!, controls: [], display_payload: { ...(value.turns[0]!.display_payload as object), text: "正式回执" } }] });
    const continueOperation = vi.fn().mockResolvedValue(snapshot(2));
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open();
    expect(await controller.activateControl(control("action_offer"))).toBe(true);
    expect(continueOperation).toHaveBeenCalledOnce(); expect(executeControl).toHaveBeenCalledOnce(); expect(sendTurn).not.toHaveBeenCalled();
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toMatchObject({ text: "正式回执" });
  });

  it("waits for the original result-check control to finish before claiming its continuation", async () => {
    const value = prepared(); const { api, executeControl, sendTurn } = fake_api(value);
    const check = control("result_check_handle");
    const pending = { ...value, turns: [{ ...value.turns[0]!, controls: [check] }] };
    const terminal = { ...value, turns: [{ ...value.turns[0]!, controls: [] }] };
    executeControl.mockResolvedValueOnce(pending).mockResolvedValueOnce(pending).mockResolvedValue(terminal);
    const continueOperation = vi.fn().mockResolvedValue(snapshot(2));
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open();
    expect(await controller.activateControl(control("action_offer"))).toBe(true);
    expect(continueOperation).not.toHaveBeenCalled();
    expect(await controller.activateControl(check)).toBe(true);
    expect(continueOperation).not.toHaveBeenCalled();
    expect(await controller.activateControl(check)).toBe(true);
    expect(continueOperation).toHaveBeenCalledOnce();
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(executeControl.mock.calls.map(call => call[1])).toEqual(["action_offer", "result_check_handle", "result_check_handle"]);
    expect(sendTurn).not.toHaveBeenCalled();
  });

  it("keeps receipt and draft after continuation failure without automatic retry or fake failed user message", async () => {
    const { api, sendTurn } = fake_api(prepared());
    const continueOperation = vi.fn().mockRejectedValue(new AssistantApiError("failed", "assistant.unavailable", "not_accepted"));
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open(); controller.setInput("保留");
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(controller.getState().error_message).toContain("回执");
    expect(controller.getState().pending_message).toBeNull(); expect(controller.getState().input).toBe("保留");
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(continueOperation).toHaveBeenCalledOnce(); expect(sendTurn).not.toHaveBeenCalled();
  });

  it("does not revive old continuations after a newer user turn or identity invalidation", async () => {
    const value = prepared(); value.generation = 2; value.turns.push(...snapshot(2).turns);
    const { api } = fake_api(value); const continueOperation = vi.fn();
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open();
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    controller.invalidateIdentity(); expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(continueOperation).not.toHaveBeenCalled();
  });

  it("holds an unverified continuation delivery for refresh or close without inventing a user message", async () => {
    const { api, sendTurn } = fake_api(prepared());
    const continueOperation = vi.fn().mockRejectedValue(new Error("response lost before acceptance"));
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open();
    await controller.continueOperation("proposal.1"); controller.setInput("新问题");
    expect(controller.getState().delivery_uncertain).toBe(true);
    expect(controller.getState().pending_message).toBeNull();
    expect(await controller.send()).toBe(false); expect(sendTurn).not.toHaveBeenCalled();
  });

  it.each(["stop", "new", "identity"])("ignores late continuation acceptance and completion after %s", async action => {
    const { api, createConversation, stopRun } = fake_api(prepared());
    const pending = deferred<AssistantConversation>(); const continueOperation = vi.fn().mockReturnValue(pending.promise);
    const closed = { ...prepared(), generation: 3 };
    const controller = createAssistantSessionController({ ...api, continueOperation, closeConversation: vi.fn().mockResolvedValue(closed) });
    await controller.open(); controller.setInput("完整草稿"); const running = controller.continueOperation("proposal.1");
    const accepted = snapshot(2, { active: true }); accepted.turns[0]!.continuation_of = "turn.1";
    continueOperation.mock.calls[0]![2](accepted);
    if (action === "identity") controller.invalidateIdentity();
    else if (action === "new") { createConversation.mockResolvedValue({ ...snapshot(0), conversation_ref: "conversation.new" }); await controller.newConversation(); }
    else { stopRun.mockResolvedValue({ ...snapshot(3), turns: [{ ...accepted.turns[0]!, visible_state: "stopped" }] }); await controller.stop(); }
    const retained = controller.getState().snapshot;
    continueOperation.mock.calls[0]![2](accepted); pending.resolve(snapshot(2));
    expect(await running).toBe(false); expect(controller.getState().snapshot).toBe(retained);
    expect(controller.getState().input).toBe("完整草稿");
    expect(await controller.continueOperation("proposal.1")).toBe(false);
  });

  it("recovers an accepted continuation failure once and checks the same handle on explicit retry", async () => {
    const value = prepared(); const { api, fetchConversation, sendTurn, executeControl } = fake_api(value);
    const accepted = snapshot(2, { active: true }); accepted.turns[0]!.continuation_of = "turn.1";
    const completed = { ...snapshot(2), turns: [...value.turns, { ...snapshot(2).turns[0]!, continuation_of: "turn.1", display_payload: { error_code: "assistant.budget", text: "后续失败" } }] };
    const continueOperation = vi.fn().mockImplementationOnce(async (_conversation, _handle, onAccepted) => {
      onAccepted(accepted); throw new AssistantApiError("budget exhausted", "assistant.run_failed", "accepted_failed");
    }).mockResolvedValue(completed);
    fetchConversation.mockResolvedValue(completed);
    const controller = createAssistantSessionController({ ...api, continueOperation }); await controller.open();
    expect(await controller.continueOperation("proposal.1")).toBe(false);
    expect(fetchConversation).toHaveBeenCalledOnce(); expect(controller.getState().snapshot).toEqual(completed);
    expect(controller.getState().error_message).toContain("原回执");
    expect(await controller.retryContinuation("turn.1")).toBe(true);
    expect(continueOperation).toHaveBeenCalledTimes(2);
    expect(continueOperation.mock.calls.map(call => call.slice(0, 2))).toEqual([["conversation.1", "continuation.1"], ["conversation.1", "continuation.1"]]);
    expect(sendTurn).not.toHaveBeenCalled(); expect(executeControl).not.toHaveBeenCalled();
  });

  it.each([0, 3])("keeps an unknown send anchored at generation %s across same-generation refreshes", async (generation) => {
    const { api, createConversation, sendTurn, fetchConversation } = fake_api(snapshot(generation));
    createConversation.mockResolvedValueOnce(snapshot(generation))
      .mockResolvedValue({ ...snapshot(0), conversation_ref: "conversation.2" });
    sendTurn.mockRejectedValue(new Error("send response unavailable"));
    fetchConversation.mockResolvedValue(snapshot(generation));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("接纳尚待核验");
    await controller.send();
    expect(await controller.newConversation()).toBe(false);
    expect(await controller.refresh()).toBe(true);
    expect(controller.getState().delivery_uncertain).toBe(true);
    expect(controller.getState().error_message).toContain("发送结果");
    expect(await controller.send()).toBe(false);
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(sendTurn).toHaveBeenCalledTimes(1);
    fetchConversation.mockResolvedValue(snapshot(generation + 1));
    expect(await controller.refresh()).toBe(true);
    expect(controller.getState().delivery_uncertain).toBe(false);
    expect(await controller.newConversation()).toBe(true);
  });

  it("does not treat a same-generation refresh before the send error as verified delivery", async () => {
    const pending = deferred<AssistantConversation>();
    const { api, sendTurn, fetchConversation, createConversation } = fake_api(snapshot(3));
    sendTurn.mockReturnValue(pending.promise);
    fetchConversation.mockResolvedValue(snapshot(3));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("较早刷新不能核验的发送");
    const sending = controller.send();
    await controller.refresh();
    pending.reject(new Error("send response later failed"));
    expect(await sending).toBe(false);
    expect(controller.getState().delivery_uncertain).toBe(true);
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
  });

  it("does not create a new conversation while a failed send remains unverified in the original generation", async () => {
    const { api, createConversation, sendTurn, fetchConversation, stopRun } = fake_api();
    sendTurn.mockRejectedValue(new Error("response failed before acceptance was observed"));
    fetchConversation.mockResolvedValue(snapshot(0));
    createConversation.mockResolvedValueOnce(snapshot(0))
      .mockResolvedValueOnce({ ...snapshot(0), conversation_ref: "conversation.2" });
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("可能迟到接纳的原话");
    expect(await controller.send()).toBe(false);
    expect(controller.getState().sending).toBe(false);
    expect(controller.getState().delivery_uncertain).toBe(true);
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(stopRun).not.toHaveBeenCalled();
    expect(controller.getState().snapshot?.conversation_ref).toBe("conversation.1");
    expect(controller.getState().delivery_uncertain).toBe(true);
    expect(controller.getState().error_message).toContain("发送状态仍待核验");
  });

  it("keeps the current conversation while a formal control result is pending", async () => {
    const pending = deferred<AssistantConversation>();
    const published = control("action_offer");
    const { api, createConversation, executeControl, fetchConversation } = fake_api(snapshot(1, { controls: [published] }));
    executeControl.mockReturnValue(pending.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();
    const clicking = controller.activateControl(published);
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(fetchConversation).not.toHaveBeenCalled();
    pending.resolve(snapshot(1));
    await clicking;
  });

  it("reports an unverified send result and requires a refresh before another send", async () => {
    const { api, sendTurn, fetchConversation } = fake_api();
    sendTurn.mockRejectedValue(new Error("response lost after acceptance"));
    fetchConversation.mockResolvedValue(snapshot(1));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("这一句可能已处理");
    expect(await controller.send()).toBe(false);
    expect(controller.getState().error_message).toContain("发送结果暂时无法核验");
    expect(controller.getState().error_message).not.toContain("没能送达");
    expect(await controller.send()).toBe(false);
    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(await controller.refresh()).toBe(true);
  });

  it("creates an isolated conversation after stopping an active run and ignores its late response", async () => {
    const late = deferred<AssistantConversation>();
    const { api, createConversation, fetchConversation, sendTurn, stopRun, executeControl } = fake_api();
    sendTurn.mockReturnValue(late.promise);
    fetchConversation.mockResolvedValue(snapshot(1, { active: true }));
    stopRun.mockResolvedValue(snapshot(2));
    const fresh = { ...snapshot(0), conversation_ref: "conversation.2" };
    createConversation.mockResolvedValueOnce(snapshot(0)).mockResolvedValueOnce(fresh);
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("旧对话内容");
    const sending = controller.send();
    expect(await controller.newConversation()).toBe(true);
    expect(stopRun).toHaveBeenCalledExactlyOnceWith("conversation.1", "run.1", 1);
    expect(createConversation.mock.invocationCallOrder[1]).toBeGreaterThan(stopRun.mock.invocationCallOrder[0]!);
    expect(controller.getState().snapshot).toEqual(fresh);
    expect(controller.getState().input).toBe("");
    late.resolve(snapshot(1, { display_text: "旧对话迟到答案" }));
    expect(await sending).toBe(false);
    expect(controller.getState().snapshot).toEqual(fresh);
    expect(executeControl).not.toHaveBeenCalled();
  });

  it("keeps the old conversation if its in-flight run cannot be verified or stopped", async () => {
    const { api, createConversation, fetchConversation, stopRun } = fake_api(snapshot(1, { active: true }));
    fetchConversation.mockResolvedValue(snapshot(1, { active: true }));
    stopRun.mockRejectedValue(new Error("stop response lost"));
    const controller = createAssistantSessionController(api);
    await controller.open();
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(controller.getState().snapshot?.conversation_ref).toBe("conversation.1");
  });

  it("does not leave an unobserved send behind", async () => {
    const pending = deferred<AssistantConversation>();
    const { api, createConversation, fetchConversation, sendTurn } = fake_api();
    fetchConversation.mockResolvedValue(snapshot(0));
    sendTurn.mockReturnValue(pending.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("仍未看到接纳状态");
    const sending = controller.send();
    expect(await controller.newConversation()).toBe(false);
    expect(createConversation).toHaveBeenCalledTimes(1);
    pending.resolve(snapshot(1));
    await sending;
  });

  it("keeps typed input and rejects send while the server snapshot is busy", async () => {
    const { api, sendTurn } = fake_api(snapshot(1, { active: true }));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("先写在输入框里的下一句");

    expect(await controller.send()).toBe(false);
    expect(controller.getState().input).toBe("先写在输入框里的下一句");
    expect(sendTurn).not.toHaveBeenCalled();
  });

  it("rejects double-send without a hidden queue and preserves text typed during the request", async () => {
    const run = deferred<AssistantConversation>();
    const { api, sendTurn } = fake_api();
    sendTurn.mockReturnValue(run.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("第一句");

    const first = controller.send();
    expect(await controller.send()).toBe(false);
    controller.setInput("请求期间新写的下一句");
    run.resolve(snapshot(1));

    expect(await first).toBe(true);
    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(sendTurn).toHaveBeenCalledWith("conversation.1", "第一句", expect.any(Function), expect.any(AbortSignal));
    expect(controller.getState().input).toBe("请求期间新写的下一句");
  });

  it("blocks a control request while refresh is in flight without queueing it", async () => {
    const clicked = control("action_offer");
    const refreshing = deferred<AssistantConversation>();
    const { api, executeControl, fetchConversation } = fake_api(
      snapshot(1, { controls: [clicked] }),
    );
    fetchConversation.mockReturnValue(refreshing.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();

    const first = controller.refresh();
    expect(await controller.activateControl(clicked)).toBe(false);
    expect(executeControl).not.toHaveBeenCalled();
    refreshing.resolve(snapshot(1, { controls: [clicked], display_text: "刷新完成" }));

    expect(await first).toBe(true);
    expect(fetchConversation).toHaveBeenCalledTimes(1);
  });

  it("does not let an older send failure overwrite a newer successful refresh", async () => {
    const old_send = deferred<AssistantConversation>();
    const { api, fetchConversation, sendTurn } = fake_api();
    sendTurn.mockReturnValue(old_send.promise);
    fetchConversation.mockResolvedValue(snapshot(2, { display_text: "刷新成功" }));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("会失败的旧请求");

    const sending = controller.send();
    expect(await controller.refresh()).toBe(true);
    old_send.reject(new Error("old send failed"));

    expect(await sending).toBe(false);
    expect(controller.getState().error_message).toBeNull();
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "刷新成功" });
    expect(controller.getState().input).toBe("");
  });

  it("uses stop as one mechanical API call and rejects a double click", async () => {
    const stopped = deferred<AssistantConversation>();
    const { api, executeControl, sendTurn, stopRun } = fake_api(
      snapshot(2, { active: true }),
    );
    stopRun.mockReturnValue(stopped.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();

    const first = controller.stop();
    expect(await controller.stop()).toBe(false);
    stopped.resolve(snapshot(3));

    expect(await first).toBe(true);
    expect(stopRun).toHaveBeenCalledTimes(1);
    expect(stopRun).toHaveBeenCalledWith("conversation.1", "run.2", 2);
    expect(sendTurn).not.toHaveBeenCalled();
    expect(executeControl).not.toHaveBeenCalled();
  });

  it("refreshes only with GET semantics and never resends or clears the input", async () => {
    const { api, executeControl, fetchConversation, sendTurn } = fake_api(snapshot(1));
    fetchConversation.mockResolvedValue(snapshot(1, { display_text: "刷新后的已提交快照" }));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("尚未发送的老师输入");

    expect(await controller.refresh()).toBe(true);

    expect(fetchConversation).toHaveBeenCalledTimes(1);
    expect(fetchConversation).toHaveBeenCalledWith("conversation.1");
    expect(sendTurn).not.toHaveBeenCalled();
    expect(executeControl).not.toHaveBeenCalled();
    expect(controller.getState().input).toBe("尚未发送的老师输入");
  });

  it("treats natural-language confirmation as an ordinary turn", async () => {
    const { api, executeControl, sendTurn } = fake_api();
    sendTurn.mockResolvedValue(snapshot(1));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("确认");

    expect(await controller.send()).toBe(true);

    expect(sendTurn).toHaveBeenCalledWith("conversation.1", "确认", expect.any(Function), expect.any(AbortSignal));
    expect(executeControl).not.toHaveBeenCalled();
  });

  it.each<AssistantControlKind>([
    "action_offer",
    "recovery_control",
    "result_check_handle",
  ])("executes %s only from a published control and rejects double-click", async (kind) => {
    const clicked = control(kind);
    const result = deferred<AssistantConversation>();
    const { api, executeControl, sendTurn } = fake_api(
      snapshot(1, { controls: [clicked] }),
    );
    executeControl.mockReturnValue(result.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();

    const first = controller.activateControl(clicked);
    expect(await controller.activateControl(clicked)).toBe(false);
    result.resolve(snapshot(1, { display_text: "点击后的服务端结果" }));

    expect(await first).toBe(true);
    expect(executeControl).toHaveBeenCalledTimes(1);
    expect(executeControl).toHaveBeenCalledWith(
      "conversation.1",
      kind,
      clicked.handle,
    );
    expect(sendTurn).not.toHaveBeenCalled();
    expect(await controller.activateControl(clicked)).toBe(false);
  });

  it("blocks refresh while a control request is in flight without queueing it", async () => {
    const clicked = control("result_check_handle");
    const result = deferred<AssistantConversation>();
    const { api, executeControl, fetchConversation } = fake_api(
      snapshot(2, { controls: [clicked] }),
    );
    executeControl.mockReturnValue(result.promise);
    const controller = createAssistantSessionController(api);
    await controller.open();

    const first = controller.activateControl(clicked);
    expect(await controller.refresh()).toBe(false);
    expect(fetchConversation).not.toHaveBeenCalled();
    result.resolve(snapshot(2, { display_text: "核验完成" }));

    expect(await first).toBe(true);
    expect(executeControl).toHaveBeenCalledTimes(1);
  });

  it("rejects an older GET response after a newer turn snapshot", async () => {
    const old_get = deferred<AssistantConversation>();
    const { api, fetchConversation, sendTurn } = fake_api();
    fetchConversation.mockReturnValue(old_get.promise);
    sendTurn.mockResolvedValue(snapshot(2, { display_text: "新回合" }));
    const controller = createAssistantSessionController(api);
    await controller.open();

    const refresh = controller.refresh();
    controller.setInput("新回合");
    await controller.send();
    old_get.resolve(snapshot(0));
    await refresh;

    expect(controller.getState().snapshot?.generation).toBe(2);
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "新回合" });
  });

  it("rejects an older turn response after a newer snapshot", async () => {
    const old_turn = deferred<AssistantConversation>();
    const { api, fetchConversation, sendTurn } = fake_api();
    sendTurn.mockReturnValue(old_turn.promise);
    fetchConversation.mockResolvedValue(snapshot(3, { display_text: "更新快照" }));
    const controller = createAssistantSessionController(api);
    await controller.open();
    controller.setInput("会迟到的回合");

    const sending = controller.send();
    await controller.refresh();
    old_turn.resolve(snapshot(1, { display_text: "迟到回合" }));
    await sending;

    expect(controller.getState().snapshot?.generation).toBe(3);
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "更新快照" });
  });

  it("rejects an older stop response after a newer generation", async () => {
    const old_stop = deferred<AssistantConversation>();
    const { api, fetchConversation, stopRun } = fake_api(snapshot(1, { active: true }));
    stopRun.mockReturnValue(old_stop.promise);
    fetchConversation.mockResolvedValue(snapshot(4, { display_text: "更新快照" }));
    const controller = createAssistantSessionController(api);
    await controller.open();

    const stopping = controller.stop();
    await controller.refresh();
    old_stop.resolve(snapshot(2, { display_text: "迟到停止" }));
    await stopping;

    expect(controller.getState().snapshot?.generation).toBe(4);
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "更新快照" });
  });

  it("rejects an older generation returned by control", async () => {
    const clicked = control("action_offer");
    const { api, executeControl } = fake_api(
      snapshot(3, { controls: [clicked], display_text: "当前快照" }),
    );
    executeControl.mockResolvedValue(snapshot(2, { display_text: "迟到点击" }));
    const controller = createAssistantSessionController(api);
    await controller.open();

    expect(await controller.activateControl(clicked)).toBe(false);

    expect(controller.getState().snapshot?.generation).toBe(3);
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "当前快照" });
    expect(controller.getState().snapshot?.turns[0]?.controls).toEqual([clicked]);
  });

  it("does not regress a completed same-generation snapshot to active", async () => {
    const { api, fetchConversation } = fake_api(
      snapshot(2, { display_text: "已完成" }),
    );
    fetchConversation.mockResolvedValue(snapshot(2, { active: true }));
    const controller = createAssistantSessionController(api);
    await controller.open();

    expect(await controller.refresh()).toBe(false);

    expect(controller.getState().snapshot?.active_run).toBeNull();
    expect(controller.getState().snapshot?.turns[0]?.display_payload).toEqual({ text: "已完成" });
  });
});


describe("message lifecycle acceptance", () => {
  it("publishes immediately and preserves even an identical new draft", async () => {
    const pending = deferred<AssistantConversation>();
    const { api, sendTurn } = fake_api(); sendTurn.mockReturnValue(pending.promise);
    const c = createAssistantSessionController(api); await c.open();
    c.setInput("本次问题"); const sending = c.send();
    expect(c.getState().input).toBe("");
    expect(c.getState().pending_message?.text).toBe("本次问题");
    expect(c.getState().pending_message?.status).toBe("sending");
    c.setInput("本次问题"); expect(await c.send()).toBe(false);
    pending.resolve(snapshot(1)); await sending;
    expect(c.getState().input).toBe("本次问题"); expect(sendTurn).toHaveBeenCalledTimes(1); c.dispose();
  });
  it("exposes the actual accepted run before answer completion", async () => {
    const pending = deferred<AssistantConversation>();
    const { api, sendTurn, stopRun } = fake_api();
    sendTurn.mockImplementation((_ref, _text, accepted) => { accepted(snapshot(1, { active: true })); return pending.promise; });
    stopRun.mockResolvedValue({ ...snapshot(2), turns: [{ ...snapshot(1).turns[0]!, visible_state: "stopped", display_payload: null }] });
    const c = createAssistantSessionController(api); await c.open(); c.setInput("第1句老师原话"); const sending = c.send();
    expect(c.getState().snapshot?.active_run?.run_ref).toBe("run.1"); expect(c.getState().pending_message).toBeNull();
    c.setInput("下一轮草稿"); expect(await c.stop()).toBe(true);
    pending.resolve(snapshot(1)); await sending;
    expect(c.getState().snapshot?.generation).toBe(2); expect(c.getState().input).toBe("下一轮草稿"); c.dispose();
  });
  it("seals an unknown send before new conversation and discards late acceptance", async () => {
    const pending = deferred<AssistantConversation>(); let accepted!: (s: AssistantConversation) => void;
    const { api, sendTurn, createConversation } = fake_api();
    const closeConversation = vi.fn().mockResolvedValue(snapshot(0));
    sendTurn.mockImplementation((_ref, _text, callback) => { accepted = callback; return pending.promise; });
    createConversation.mockResolvedValueOnce(snapshot(0)).mockResolvedValueOnce({ ...snapshot(0), conversation_ref: "conversation.2" });
    const c = createAssistantSessionController({ ...api, closeConversation }); await c.open();
    c.setInput("旧问题"); const sending = c.send(); c.setInput("下一轮草稿"); expect(await c.newConversation()).toBe(true);
    expect(closeConversation).toHaveBeenCalledWith("conversation.1", expect.any(AbortSignal));
    accepted(snapshot(1, { active: true })); pending.resolve(snapshot(1)); await sending;
    expect(c.getState().snapshot?.conversation_ref).toBe("conversation.2"); expect(c.getState().input).toBe("下一轮草稿"); c.dispose();
  });
});


describe("close and failure recovery", () => {
  it("applies stopped state if closing succeeded but creating a new conversation failed", async () => {
    const {api, createConversation} = fake_api(snapshot(1, { active:true }));
    createConversation.mockResolvedValueOnce(snapshot(1,{active:true})).mockRejectedValueOnce(new Error("offline"));
    const stopped = {...snapshot(2),turns:[{...snapshot(1).turns[0]!,visible_state:"stopped" as const,display_payload:null}]};
    const c=createAssistantSessionController({...api,closeConversation:vi.fn().mockResolvedValue(stopped)});await c.open();
    c.setInput("保留的草稿");expect(await c.newConversation()).toBe(false);
    expect(c.getState().snapshot?.active_run).toBeNull();expect(c.getState().snapshot?.turns[0]?.visible_state).toBe("stopped");
    expect(c.getState().conversation_closed).toBe(true);expect(c.getState().input).toBe("保留的草稿");expect(await c.send()).toBe(false);c.dispose();
  });
  it("retains a rejected message when a different draft is sent", async()=>{
    const {AssistantApiError}=await import("../src/features/assistant/assistant_api");
    const {api,sendTurn}=fake_api();sendTurn.mockRejectedValueOnce(new AssistantApiError("rejected","assistant.unavailable","not_accepted")).mockResolvedValueOnce(snapshot(1));
    const c=createAssistantSessionController(api);await c.open();c.setInput("失败原文");await c.send();c.setInput("新问题");await c.send();
    expect(c.getState().failed_messages.map(x=>x.text)).toEqual(["失败原文"]);c.dispose();
  });
});


it("marks an unacknowledged message stopped when close succeeds but replacement creation fails", async()=>{
  const late=deferred<AssistantConversation>();const {api,sendTurn,createConversation}=fake_api();sendTurn.mockReturnValue(late.promise);
  createConversation.mockResolvedValueOnce(snapshot(0)).mockRejectedValueOnce(new Error("offline"));
  const c=createAssistantSessionController({...api,closeConversation:vi.fn().mockResolvedValue(snapshot(0))});await c.open();c.setInput("未核验原文");const sending=c.send();c.setInput("仍可编辑");
  expect(await c.newConversation()).toBe(false);expect(c.getState().pending_message?.status).toBe("stopped");expect(c.getState().input).toBe("仍可编辑");
  late.resolve(snapshot(1));await sending;expect(c.getState().snapshot?.generation).toBe(0);c.dispose();
});


describe("bounded new-conversation preparation", () => {
  it("unlocks a timed-out close, blocks uncertain sends, and recovers only after another explicit close", async () => {
    vi.useFakeTimers();
    const oldClose = deferred<AssistantConversation>();
    const { api, createConversation, sendTurn } = fake_api(snapshot(1));
    const closeConversation = vi.fn().mockReturnValueOnce(oldClose.promise).mockResolvedValue(snapshot(1));
    createConversation.mockResolvedValueOnce(snapshot(1)).mockResolvedValue({ ...snapshot(0), conversation_ref: "conversation.2" });
    const c = createAssistantSessionController({ ...api, closeConversation });
    try {
      await c.open(); c.setInput("保留的草稿"); const first = c.newConversation();
      expect(c.getState().navigation_phase).toBe("closing");
      await vi.advanceTimersByTimeAsync(15_001);
      expect(c.getState().navigation_phase).toBe("close_failed");
      expect(c.getState().starting_new).toBe(false);
      expect(await first).toBe(false);
      expect(c.getState().navigation_uncertain).toBe(true);
      expect(c.getState().error_message).toContain("尚未确认");
      expect(c.getState().input).toBe("保留的草稿");
      expect(await c.send()).toBe(false); expect(sendTurn).not.toHaveBeenCalled();
      expect(createConversation).toHaveBeenCalledTimes(1);
      expect(closeConversation.mock.calls[0]![1].aborted).toBe(true);
      expect(await c.newConversation()).toBe(true);
      expect(c.getState().navigation_uncertain).toBe(false);
      oldClose.resolve(snapshot(9)); await Promise.resolve();
      expect(c.getState().snapshot?.conversation_ref).toBe("conversation.2");
      expect(c.getState().input).toBe("保留的草稿");
    } finally { c.dispose(); vi.useRealTimers(); }
  });

  it("keeps confirmed closed history when replacement creation does not return", async () => {
    vi.useFakeTimers(); const create = deferred<AssistantConversation>();
    const { api, createConversation } = fake_api(snapshot(1));
    createConversation.mockResolvedValueOnce(snapshot(1)).mockReturnValueOnce(create.promise);
    const c = createAssistantSessionController({ ...api, closeConversation: vi.fn().mockResolvedValue(snapshot(1)) });
    try {
      await c.open(); c.setInput("创建期间草稿"); const changing = c.newConversation();
      await vi.advanceTimersByTimeAsync(15_001);
      expect(c.getState().starting_new).toBe(false); expect(await changing).toBe(false);
      expect(c.getState().conversation_closed).toBe(true); expect(await c.send()).toBe(false);
      expect(c.getState().navigation_phase).toBe("create_failed");
      expect(c.getState().snapshot?.conversation_ref).toBe("conversation.1");
      expect(c.getState().input).toBe("创建期间草稿");
      create.resolve({ ...snapshot(0), conversation_ref: "late.conversation" }); await Promise.resolve();
      expect(c.getState().snapshot?.conversation_ref).toBe("conversation.1");
    } finally { c.dispose(); vi.useRealTimers(); }
  });

  it("aborts slow close on identity disposal without creating a replacement", async () => {
    const close = deferred<AssistantConversation>(); const { api, createConversation } = fake_api(snapshot(1));
    const closeConversation = vi.fn().mockReturnValue(close.promise);
    const c = createAssistantSessionController({ ...api, closeConversation });
    await c.open(); const changing = c.newConversation(); c.dispose();
    expect(closeConversation.mock.calls[0]![1]?.aborted).toBe(true);
    expect(await changing).toBe(false); close.resolve(snapshot(1)); await Promise.resolve();
    expect(createConversation).toHaveBeenCalledTimes(1);
  });
});


it("keeps the close-uncertain warning when the old answer completes after navigation times out", async () => {
  vi.useFakeTimers(); const send = deferred<AssistantConversation>(); const close = deferred<AssistantConversation>();
  const { api, sendTurn } = fake_api(); sendTurn.mockReturnValue(send.promise);
  const c = createAssistantSessionController({ ...api, closeConversation: vi.fn().mockReturnValue(close.promise) });
  try {
    await c.open(); c.setInput("旧问题"); const sending = c.send(); const changing = c.newConversation();
    await vi.advanceTimersByTimeAsync(15_001); expect(await changing).toBe(false);
    send.resolve(snapshot(1)); await sending;
    expect(c.getState().navigation_uncertain).toBe(true);
    expect(c.getState().error_message).toContain("尚未确认");
    expect(await c.send()).toBe(false);
  } finally { c.dispose(); vi.useRealTimers(); }
});


it("clears navigation uncertainty after a verified legacy navigation recovery", async () => {
  const { api, createConversation, fetchConversation } = fake_api(snapshot(1));
  fetchConversation.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(snapshot(1));
  createConversation.mockResolvedValueOnce(snapshot(1)).mockResolvedValue({ ...snapshot(0), conversation_ref: "conversation.2" });
  const c = createAssistantSessionController(api);
  try {
    await c.open(); expect(await c.newConversation()).toBe(false);
    expect(c.getState().navigation_uncertain).toBe(true);
    expect(await c.newConversation()).toBe(true);
    expect(c.getState().navigation_uncertain).toBe(false);
  } finally { c.dispose(); }
});


it("publishes actual close and create phases without conversation content", async () => {
  const close = deferred<AssistantConversation>(); const create = deferred<AssistantConversation>();
  const { api, createConversation } = fake_api(snapshot(1));
  createConversation.mockResolvedValueOnce(snapshot(1)).mockReturnValueOnce(create.promise);
  const c = createAssistantSessionController({ ...api, closeConversation: vi.fn().mockReturnValue(close.promise) });
  try {
    await c.open(); const changing = c.newConversation();
    expect(c.getState().navigation_phase).toBe("closing");
    close.resolve(snapshot(1));
    await vi.waitFor(() => expect(c.getState().navigation_phase).toBe("creating"));
    create.resolve({ ...snapshot(0), conversation_ref: "conversation.2" });
    expect(await changing).toBe(true); expect(c.getState().navigation_phase).toBeNull();
  } finally { c.dispose(); }
});


it("preserves draft/messages but locks all old identity operations after close 401", async () => {
  const { api, createConversation, sendTurn, fetchConversation, stopRun, executeControl } = fake_api(snapshot(1, { controls: [control("action_offer")] }));
  const close = vi.fn().mockRejectedValue(new AssistantApiError("expired", "assistant.authentication_required", "not_accepted"));
  api.closeConversation = close;
  const session = createAssistantSessionController(api);
  await session.open(); session.setInput("KEEP_DRAFT");
  const before = session.getState().snapshot;
  expect(await session.newConversation()).toBe(false);
  expect(session.getState()).toMatchObject({ authentication_required: true, input: "KEEP_DRAFT", snapshot: before, starting_new: false });
  expect(session.getState().error_message).toContain("身份");
  expect(session.getState().error_message).not.toContain("再次点击新对话");
  await session.newConversation(); await session.refresh(); await session.send(); await session.stop(); await session.activateControl(control("action_offer"));
  expect(close).toHaveBeenCalledTimes(1); expect(createConversation).toHaveBeenCalledTimes(1);
  for (const operation of [sendTurn, fetchConversation, stopRun, executeControl]) expect(operation).not.toHaveBeenCalled();
});
it("discards late snapshots after a concurrent refresh rejects the identity", async () => {
  const { api, sendTurn, fetchConversation } = fake_api();
  const pending = deferred<AssistantConversation>(); sendTurn.mockReturnValue(pending.promise);
  fetchConversation.mockRejectedValue(new AssistantApiError("expired", "assistant.authentication_required"));
  const session = createAssistantSessionController(api); await session.open(); session.setInput("KEEP_MESSAGE");
  const sending = session.send(); await session.refresh();
  pending.resolve(snapshot(1, { display_text: "LATE_SECRET" })); await sending;
  expect(session.getState().snapshot?.generation).toBe(0);
  expect(session.getState().pending_message?.text).toBe("KEEP_MESSAGE");
  expect(session.getState().authentication_required).toBe(true);
});


it.each(["open", "refresh", "send", "stop", "activateControl"] as const)("handles 401 consistently for %s", async (operation) => {
  const { api, createConversation, fetchConversation, sendTurn, stopRun, executeControl } = fake_api(snapshot(1, { active: operation === "stop", controls: [control("action_offer")] }));
  const denied = new AssistantApiError("expired", "assistant.authentication_required", "not_accepted");
  const methods = { open: createConversation, refresh: fetchConversation, send: sendTurn, stop: stopRun, activateControl: executeControl };
  const session = createAssistantSessionController(api);
  if (operation !== "open") await session.open();
  methods[operation].mockRejectedValue(denied); session.setInput("PRESERVED");
  if (operation === "activateControl") await session.activateControl(control("action_offer")); else await session[operation]();
  expect(session.getState().authentication_required).toBe(true);
  expect(session.getState().error_message).toContain("身份");
  expect(session.getState().sending || session.getState().opening || session.getState().stopping || session.getState().refreshing).toBe(false);
});


it("recovers a server-terminated run once without resending and preserves the next draft", async () => {
  const { api, sendTurn, fetchConversation } = fake_api();
  const recovered = snapshot(1);
  recovered.turns[0]!.display_payload = { text: "小静暂时不可用，请稍后重试。", error_code: "assistant.runtime_unavailable" };
  fetchConversation.mockResolvedValue(recovered);
  sendTurn.mockImplementation(async (_ref, _text, accepted) => {
    accepted(snapshot(1, { active: true }));
    session.setInput("NEXT_DRAFT");
    throw new AssistantApiError("本轮处理已中止，未取得完整回答。请刷新核对对话。", "assistant.run_failed", "accepted_failed");
  });
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  expect(await session.send()).toBe(false);
  expect(fetchConversation).toHaveBeenCalledExactlyOnceWith("conversation.1");
  expect(sendTurn).toHaveBeenCalledTimes(1);
  expect(session.getState()).toMatchObject({ terminal_failure_run_ref: null, sending: false, delivery_uncertain: false, input: "NEXT_DRAFT", error_message: null });
  expect(session.getState().snapshot).toEqual(recovered);
  sendTurn.mockResolvedValue(snapshot(2));
  expect(await session.send()).toBe(true);
  expect(sendTurn.mock.calls[1]![1]).toBe("NEXT_DRAFT");
});


it("renders the recovered failure and enables an explicit retry without resending automatically", async () => {
  const { createApp, nextTick } = await import("vue");
  const { default: Conversation } = await import("../src/features/assistant/AssistantConversation.vue");
  const accepted = snapshot(1, { active: true });
  const recovered = snapshot(1);
  recovered.turns[0]!.display_payload = { text: "小静暂时不可用，请稍后重试。", error_code: "assistant.runtime_unavailable" };
  const body = JSON.stringify({ event: "accepted", snapshot: accepted }) + "\n" + JSON.stringify({ event: "error", code: "assistant.runtime_unavailable" }) + "\n";
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(snapshot(0))))
    .mockResolvedValueOnce(new Response(body, { headers: { "Content-Type": "application/x-ndjson" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify(recovered)));
  vi.stubGlobal("fetch", fetcher);
  const host = document.createElement("div"); document.body.append(host); const app = createApp(Conversation);
  try {
    app.mount(host);
    await vi.waitFor(() => expect(host.querySelector('[data-action="new-conversation"]')).not.toBeNull());
    const input = host.querySelector('textarea')!; input.value = "第1句老师原话"; input.dispatchEvent(new Event("input", { bubbles: true })); await nextTick();
    host.querySelector<HTMLButtonElement>('[data-action="send"]')!.click();
    await vi.waitFor(() => expect(host.querySelector('.turn-status')?.textContent).toBe("本轮未能完成"));
    expect(host.textContent).not.toContain("连接中断");
    expect(host.querySelector('.assistant-response')?.textContent).toContain("小静暂时不可用，请稍后重试。");
    expect(host.querySelector('[data-action="stop"]')).toBeNull();
    expect(host.querySelector<HTMLButtonElement>('[data-action="retry-turn"]')!.disabled).toBe(false);
    input.value = "下一条草稿"; input.dispatchEvent(new Event("input", { bubbles: true })); await nextTick();
    expect(host.querySelector<HTMLButtonElement>('[data-action="send"]')!.disabled).toBe(false);
    expect(host.querySelector('[data-action="send"]')!.textContent?.trim()).toBe("发送");
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[2]![1].method).toBe("GET");
  } finally { app.unmount(); host.remove(); vi.unstubAllGlobals(); }
});

it.each(["offline", "still_active"])("keeps manual recovery available after one automatic check is %s", async (outcome) => {
  const { api, sendTurn, fetchConversation } = fake_api();
  sendTurn.mockImplementation(async (_ref, _text, accepted) => {
    accepted(snapshot(1, { active: true }));
    throw new AssistantApiError("本轮处理已中止，未取得完整回答。请刷新核对对话。", "assistant.run_failed", "accepted_failed");
  });
  if (outcome === "offline") fetchConversation.mockRejectedValueOnce(new Error("offline"));
  else fetchConversation.mockResolvedValueOnce(snapshot(1, { active: true }));
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  expect(await session.send()).toBe(false);
  expect(fetchConversation).toHaveBeenCalledTimes(1);
  expect(session.getState()).toMatchObject({ terminal_failure_run_ref: "run.1", sending: false, delivery_uncertain: false, refreshing: false });
  expect(session.getState().error_message).toContain("刷新");
  expect(session.getState().snapshot?.turns[0]!.teacher_utterance).toBe("第1句老师原话");
  session.setInput("KEEP_DRAFT"); expect(await session.send()).toBe(false);
  expect(sendTurn).toHaveBeenCalledTimes(1);
  fetchConversation.mockResolvedValueOnce(snapshot(1)); expect(await session.refresh()).toBe(true);
  expect(session.getState()).toMatchObject({ terminal_failure_run_ref: null, input: "KEEP_DRAFT" });
});

it("permits a new conversation while the failure check is pending and ignores its late snapshot", async () => {
  const { api, sendTurn, fetchConversation, createConversation } = fake_api();
  const recovery = deferred<AssistantConversation>(); fetchConversation.mockReturnValue(recovery.promise);
  api.closeConversation = vi.fn().mockResolvedValue(snapshot(1));
  createConversation.mockResolvedValueOnce(snapshot(0)).mockResolvedValueOnce({ ...snapshot(0), conversation_ref: "conversation.2" });
  sendTurn.mockImplementation(async (_ref, _text, accepted) => {
    accepted(snapshot(1, { active: true }));
    throw new AssistantApiError("已中止，请刷新", "assistant.run_failed", "accepted_failed");
  });
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  const sending = session.send();
  await vi.waitFor(() => expect(fetchConversation).toHaveBeenCalledTimes(1));
  session.setInput("KEEP_DRAFT");
  expect(session.getState().sending).toBe(false);
  expect(await session.newConversation()).toBe(true);
  recovery.resolve(snapshot(1, { display_text: "OLD_RESPONSE" })); await sending;
  expect(session.getState()).toMatchObject({ input: "KEEP_DRAFT", terminal_failure_run_ref: null });
  expect(session.getState().snapshot?.conversation_ref).toBe("conversation.2");
  expect(session.getState().snapshot?.turns).toEqual([]);
});

it("locks the expired identity after the automatic failure check receives 401", async () => {
  const { api, sendTurn, fetchConversation } = fake_api();
  fetchConversation.mockRejectedValue(new AssistantApiError("expired", "assistant.authentication_required"));
  sendTurn.mockImplementation(async (_ref, _text, accepted) => {
    accepted(snapshot(1, { active: true })); session.setInput("KEEP_DRAFT");
    throw new AssistantApiError("已中止，请刷新", "assistant.run_failed", "accepted_failed");
  });
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  expect(await session.send()).toBe(false);
  expect(session.getState()).toMatchObject({ authentication_required: true, input: "KEEP_DRAFT", sending: false });
  expect(session.getState().snapshot?.turns).toHaveLength(1);
  expect(session.getState().error_message).toContain("身份");
  await session.send(); await session.refresh(); await session.newConversation();
  expect(fetchConversation).toHaveBeenCalledTimes(1); expect(sendTurn).toHaveBeenCalledTimes(1);
});

it.each(["identity", "stop", "newer_run"])("ignores a late failure-check snapshot after %s invalidates that run", async (change) => {
  const { api, sendTurn, fetchConversation, stopRun } = fake_api();
  const recovery = deferred<AssistantConversation>(); fetchConversation.mockReturnValueOnce(recovery.promise);
  sendTurn.mockImplementation(async (_ref, _text, accepted) => {
    accepted(snapshot(1, { active: true }));
    throw new AssistantApiError("已中止，请刷新", "assistant.run_failed", "accepted_failed");
  });
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  const sending = session.send();
  await vi.waitFor(() => expect(fetchConversation).toHaveBeenCalledTimes(1));
  session.setInput("KEEP_DRAFT");
  if (change === "identity") session.invalidateIdentity();
  else if (change === "stop") {
    const stopped = snapshot(1); stopped.turns[0]!.visible_state = "stopped";
    stopRun.mockResolvedValue(stopped); expect(await session.stop()).toBe(true);
  } else {
    fetchConversation.mockResolvedValueOnce(snapshot(2, { active: true }));
    expect(await session.refresh()).toBe(true);
  }
  const preserved = session.getState().snapshot;
  recovery.resolve(snapshot(1, { display_text: "LATE_RESPONSE" })); await sending;
  expect(session.getState().snapshot).toBe(preserved);
  expect(session.getState().input).toBe("KEEP_DRAFT");
  expect(session.getState().authentication_required).toBe(change === "identity");
});


it("does not assign a late terminal failure to a newer refreshed run", async () => {
  const { api, sendTurn, fetchConversation } = fake_api();
  const pending = deferred<AssistantConversation>();
  sendTurn.mockImplementation((_ref, _text, accepted) => { accepted(snapshot(1, { active: true })); return pending.promise; });
  const session = createAssistantSessionController(api); await session.open(); session.setInput("QUESTION");
  const sending = session.send();
  fetchConversation.mockResolvedValue(snapshot(2, { active: true })); await session.refresh();
  pending.reject(new AssistantApiError("已中止", "assistant.run_failed", "accepted_failed")); await sending;
  expect(session.getState().snapshot?.active_run?.run_ref).toBe("run.2");
  expect(session.getState().terminal_failure_run_ref).toBeNull();
  expect(session.getState().error_message).toBeNull();
});
