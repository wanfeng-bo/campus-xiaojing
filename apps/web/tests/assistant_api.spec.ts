import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AssistantApiError,
  continueAssistantOperation,
  closeAssistantConversation,
  createAssistantConversation,
  executeAssistantControl,
  fetchAssistantConversation,
  sendAssistantTurn,
  stopAssistantRun,
} from "../src/features/assistant/assistant_api";
import type {
  AssistantControlKind,
  AssistantConversation,
} from "../src/features/assistant/assistant_contracts";


afterEach(() => {
  vi.unstubAllGlobals();
});


function snapshot(generation = 0): AssistantConversation {
  return {
    version: "1",
    conversation_ref: "conversation.1",
    generation,
    active_run: null,
    turns: [],
  };
}


function successful_fetch(payload: unknown = snapshot()): ReturnType<typeof vi.fn> {
  const fetch_mock = vi.fn().mockImplementation(() =>
    Promise.resolve(new Response(JSON.stringify(payload), { status: 200 })),
  );
  vi.stubGlobal("fetch", fetch_mock);
  return fetch_mock;
}


describe("assistant API client", () => {
  it("continues through a bodyless endpoint using the server original task in accepted and completed events", async () => {
    const turn = { turn_ref: "t.2", run_ref: "r.2", generation: 2, teacher_utterance: "服务端保留的原任务", continuation_of: "t.1", visible_state: "active", display_kind: null, display_payload: null, controls: [] };
    const accepted = { ...snapshot(2), active_run: { turn_ref: "t.2", run_ref: "r.2", generation: 2, continuation_of: "t.1" }, turns: [turn] };
    const completed = { ...snapshot(2), turns: [{ ...turn, visible_state: "completed", display_kind: "text", display_payload: { text: "已完成后续核对" } }] };
    const body = [accepted, completed].map((snapshot, i) => JSON.stringify({ event: i ? "completed" : "accepted", snapshot })).join("\n");
    const fetcher = vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/x-ndjson" } })); vi.stubGlobal("fetch", fetcher);
    const callback = vi.fn();
    expect(await continueAssistantOperation("conversation.1", "continuation.1", callback)).toEqual(completed);
    expect(callback).toHaveBeenCalledWith(accepted);
    expect(fetcher.mock.calls[0]).toEqual(["/api/v1/assistant/conversations/conversation.1/continuations/continuation.1", { credentials: "same-origin", headers: { Accept: "application/x-ndjson" }, method: "POST", signal: undefined }]);
  });

  it("accepts an idempotent continuation snapshot without inventing another accepted run", async () => {
    successful_fetch(snapshot(2)); const callback = vi.fn();
    expect(await continueAssistantOperation("conversation.1", "continuation.1", callback)).toEqual(snapshot(2));
    expect(callback).not.toHaveBeenCalled();
  });

  it("creates and refreshes a conversation without body, query or authority headers", async () => {
    const fetch_mock = successful_fetch();

    await createAssistantConversation();
    await fetchAssistantConversation("conversation.1");

    expect(fetch_mock).toHaveBeenNthCalledWith(1, "/api/v1/assistant/conversations", {
      credentials: "same-origin",
      method: "POST",
    });
    expect(fetch_mock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/assistant/conversations/conversation.1",
      {
        credentials: "same-origin",
        method: "GET",
      },
    );
  });

  it("sends a one-time opaque selection-context claim only when creating the bound conversation", async () => {
    const fetch_mock = successful_fetch();
    await createAssistantConversation(undefined, "C".repeat(43));
    await fetchAssistantConversation("conversation.1");
    expect(fetch_mock).toHaveBeenNthCalledWith(1, "/api/v1/assistant/conversations", {
      credentials: "same-origin", method: "POST",
      headers: { "X-Conversation-Context-Claim": "C".repeat(43) },
    });
    expect(fetch_mock.mock.calls[1]?.[1]).not.toHaveProperty("headers");
  });

  it("sends exactly the current teacher utterance and no ledger or control data", async () => {
    const fetch_mock = successful_fetch(snapshot(1));

    await sendAssistantTurn("conversation.1", "自然语言确认");

    expect(fetch_mock).toHaveBeenCalledTimes(1);
    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/assistant/conversations/conversation.1/turns",
      {
        body: JSON.stringify({ teacher_utterance: "自然语言确认" }),
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        method: "POST", signal: undefined,
      },
    );
    const [, init] = fetch_mock.mock.calls[0] as [string, RequestInit];
    expect(Object.keys(JSON.parse(String(init.body)))).toEqual(["teacher_utterance"]);
  });

  it("stops only the named run and generation", async () => {
    const fetch_mock = successful_fetch(snapshot(3));

    await stopAssistantRun("conversation.1", "run.2", 2);

    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/assistant/conversations/conversation.1/runs/run.2/stop",
      {
        body: JSON.stringify({ generation: 2 }),
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
  });

  it.each<[AssistantControlKind, string]>([
    ["action_offer", "action-offers"],
    ["recovery_control", "recovery-controls"],
    ["result_check_handle", "result-checks"],
  ])("maps %s to its bodyless click route", async (kind, route) => {
    const fetch_mock = successful_fetch(snapshot(2));

    await executeAssistantControl(
      "conversation.1",
      kind,
      `control.${kind}.1`,
    );

    expect(fetch_mock).toHaveBeenCalledTimes(1);
    expect(fetch_mock).toHaveBeenCalledWith(
      `/api/v1/assistant/conversations/conversation.1/controls/${route}/control.${kind}.1`,
      {
        credentials: "same-origin",
        method: "POST",
      },
    );
    const [url, init] = fetch_mock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain("?");
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined();
  });

  it("strictly decodes success and does not expose malformed response values", async () => {
    successful_fetch({
      ...snapshot(),
      actor_context_ref: "PRIVATE-ACTOR-13900001111",
    });

    const rejected = await createAssistantConversation().catch(
      (error: unknown) => error,
    );

    expect(rejected).toBeInstanceOf(AssistantApiError);
    expect((rejected as Error).message).toBe("小静服务返回异常，请稍后刷新。");
    expect((rejected as Error).message).not.toContain("PRIVATE-ACTOR");
  });

  it("uses one fixed safe error for transport and backend failures with zero retry", async () => {
    const fetch_mock = vi.fn()
      .mockRejectedValueOnce(new Error("PRIVATE-NETWORK-13900001111"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: "assistant.runtime_unavailable",
            message: "PRIVATE-BACKEND-13900001111",
            request_id: "request.1",
          }),
          { status: 503 },
        ),
      );
    vi.stubGlobal("fetch", fetch_mock);

    const transport_error = await fetchAssistantConversation("conversation.1").catch(
      (error: unknown) => error,
    );
    const backend_error = await fetchAssistantConversation("conversation.1").catch(
      (error: unknown) => error,
    );

    expect(fetch_mock).toHaveBeenCalledTimes(2);
    expect((transport_error as Error).message).toBe(
      "小静暂时不可用，请稍后重试。",
    );
    expect((backend_error as Error).message).toBe(
      "小静暂时不可用，请稍后重试。",
    );
    expect((transport_error as Error).message).not.toContain("PRIVATE");
    expect((backend_error as Error).message).not.toContain("PRIVATE");
  });
});


describe("real acceptance event decoding", () => {
  it("delivers acceptance before consuming final content across arbitrary chunks", async () => {
    let wire!: ReadableStreamDefaultController<Uint8Array>; const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({ start(c) { wire = c; } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, { headers: { "Content-Type": "application/x-ndjson" } })));
    const turn = { turn_ref: "t.1", run_ref: "r.1", generation: 1, teacher_utterance: "问题", visible_state: "active" as const, display_kind: null, display_payload: null, controls: [] };
    const accepted = { ...snapshot(1), active_run: { turn_ref: "t.1", run_ref: "r.1", generation: 1 }, turns: [turn] };
    const completed = { ...snapshot(1), turns: [{ ...turn, visible_state: "completed" as const }] };
    const onAccepted = vi.fn(); let finished = false;
    const result = sendAssistantTurn("conversation.1", "问题", onAccepted).then(x => { finished = true; return x; });
    const line = JSON.stringify({ event: "accepted", snapshot: accepted }) + "\n";
    wire.enqueue(encoder.encode(line.slice(0, 9))); wire.enqueue(encoder.encode(line.slice(9)));
    await vi.waitFor(() => expect(onAccepted).toHaveBeenCalledWith(accepted)); expect(finished).toBe(false);
    wire.enqueue(encoder.encode(JSON.stringify({ event: "completed", snapshot: completed }) + "\n")); wire.close();
    expect(await result).toEqual(completed);
  });
  it("does not treat an interrupted stream as a completed answer", async () => {
    const line = JSON.stringify({ event: "accepted", snapshot: { ...snapshot(1), active_run: { turn_ref: "t", run_ref: "r", generation: 1 } } }) + "\n";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(line, { headers: { "Content-Type": "application/x-ndjson" } })));
    await expect(sendAssistantTurn("conversation.1", "问题")).rejects.toBeInstanceOf(AssistantApiError);
  });
  it("rejects another conversation's event without publishing it", async () => {
    const line = JSON.stringify({ event: "accepted", snapshot: { ...snapshot(1), conversation_ref: "other", active_run: { turn_ref: "t", run_ref: "r", generation: 1 } } }) + "\n";
    const callback = vi.fn(); vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(line, { headers: { "Content-Type": "application/x-ndjson" } })));
    await expect(sendAssistantTurn("conversation.1", "问题", callback)).rejects.toBeInstanceOf(AssistantApiError); expect(callback).not.toHaveBeenCalled();
  });
});


describe("run binding rejection", () => {
  it.each(["wrong_run", "still_active", "missing_turn"])("rejects %s completion", async (mode) => {
    const turn = { turn_ref: "t.1", run_ref: "r.1", generation: 1, teacher_utterance: "问题", visible_state: "active", display_kind: null, display_payload: null, controls: [] };
    const accepted = { ...snapshot(1), active_run: { turn_ref: "t.1", run_ref: "r.1", generation: 1 }, turns: [turn] };
    const completed = { ...snapshot(1), active_run: mode === "still_active" ? accepted.active_run : null,
      turns: mode === "missing_turn" ? [] : [{ ...turn, run_ref: mode === "wrong_run" ? "other" : "r.1", visible_state: mode === "still_active" ? "active" : "completed" }] };
    const body = [accepted, completed].map((v, i) => JSON.stringify({event:i ? "completed" : "accepted",snapshot:v})).join("\n")+"\n";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/x-ndjson" } })));
    await expect(sendAssistantTurn("conversation.1", "问题")).rejects.toBeInstanceOf(AssistantApiError);
  });
});


describe("identity failures", () => {
  it.each([
    () => createAssistantConversation(), () => closeAssistantConversation("conversation.1"),
    () => fetchAssistantConversation("conversation.1"), () => sendAssistantTurn("conversation.1", "draft"),
    () => stopAssistantRun("conversation.1", "run.1", 1),
    () => executeAssistantControl("conversation.1", "action_offer", "control.1"),
  ])("maps 401 without exposing server text", async (operation) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"PRIVATE"}', { status: 401 })));
    await expect(operation()).rejects.toMatchObject({ code: "assistant.authentication_required", delivery: "not_accepted" });
    await expect(operation()).rejects.not.toThrow("PRIVATE");
  });
});


it.each(["assistant.runtime_unavailable", "assistant.run_stale", "assistant.dependency", "assistant.tool_limit", "assistant.deadline", "assistant.budget", "assistant.context_limit", "assistant.output_limit"])("distinguishes terminal NDJSON %s from a broken stream", async (code) => {
  const turn = { turn_ref: "t.1", run_ref: "r.1", generation: 1, teacher_utterance: "问题", visible_state: "active", display_kind: null, display_payload: null, controls: [] };
  const accepted = { ...snapshot(1), active_run: { turn_ref: "t.1", run_ref: "r.1", generation: 1 }, turns: [turn] };
  const body = JSON.stringify({ event: "accepted", snapshot: accepted }) + "\n" + JSON.stringify({ event: "error", code, detail: "PRIVATE" }) + "\n";
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/x-ndjson" } })));
  const callback = vi.fn();
  const failure = await sendAssistantTurn("conversation.1", "问题", callback).catch(error => error);
  expect(callback).toHaveBeenCalledOnce();
  expect(failure).toMatchObject({ code: "assistant.run_failed", delivery: "accepted_failed" });
  expect(failure.message).not.toContain("PRIVATE");
});
it("does not turn an unknown stream error into a trusted terminal result", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"event":"error","code":"private.unknown"}\n', { headers: { "Content-Type": "application/x-ndjson" } })));
  await expect(sendAssistantTurn("conversation.1", "问题")).rejects.toMatchObject({code:"assistant.unavailable",delivery:"unknown"});
});
