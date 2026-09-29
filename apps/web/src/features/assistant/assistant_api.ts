import {
  decodeAssistantConversation,
  type AssistantControlKind,
  type AssistantConversation,
} from "./assistant_contracts";


/** assistant 请求失败时只携带本地固定文案，不保留未知响应正文。 */
export class AssistantApiError extends Error {
  readonly delivery: "unknown" | "not_accepted" | "accepted_failed";
  readonly code: "assistant.response_invalid" | "assistant.unavailable" | "assistant.authentication_required" | "assistant.run_failed";

  constructor(
    message: string,
    code: "assistant.response_invalid" | "assistant.unavailable" | "assistant.authentication_required" | "assistant.run_failed",
    delivery: "unknown" | "not_accepted" | "accepted_failed" = "unknown",
  ) {
    super(message);
    this.name = "AssistantApiError";
    this.code = code;
    this.delivery = delivery;
  }
}


/** 创建内存会话；POST 不发送正文、查询或客户端权威字段。 */
export async function createAssistantConversation(signal?: AbortSignal, contextClaim?: string): Promise<AssistantConversation> {
  return request_snapshot("/api/v1/assistant/conversations", {
    ...(signal ? { signal } : {}),
    ...(contextClaim ? { headers: { "X-Conversation-Context-Claim": contextClaim } } : {}),
    credentials: "same-origin",
    method: "POST",
  });
}


/** 只 GET 已提交快照，不重发任何老师输入。 */
export async function fetchAssistantConversation(
  conversation_ref: string,
): Promise<AssistantConversation> {
  return request_snapshot(
    `/api/v1/assistant/conversations/${encodeURIComponent(conversation_ref)}`,
    {
      credentials: "same-origin",
      method: "GET",
    },
  );
}


/** 发送且只发送老师本次原话；展示账本和控件不得成为请求输入。 */
export async function sendAssistantTurn(
  conversation_ref: string,
  teacher_utterance: string,
  onAccepted?: (snapshot: AssistantConversation) => void,
  signal?: AbortSignal,
): Promise<AssistantConversation> {
  return request_turn_stream(conversation_ref, "turns", teacher_utterance, onAccepted, signal);
}

/** Only a server-issued continuation reference crosses this boundary; the original task stays server-owned. */
export async function continueAssistantOperation(
  conversation_ref: string,
  handle: string,
  onAccepted?: (snapshot: AssistantConversation) => void,
  signal?: AbortSignal,
): Promise<AssistantConversation> {
  return request_turn_stream(conversation_ref, `continuations/${encodeURIComponent(handle)}`, undefined, onAccepted, signal);
}

async function request_turn_stream(
  conversation_ref: string,
  route: string,
  teacher_utterance: string | undefined,
  onAccepted?: (snapshot: AssistantConversation) => void,
  signal?: AbortSignal,
): Promise<AssistantConversation> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/assistant/conversations/${encodeURIComponent(conversation_ref)}/${route}`, {
      ...(teacher_utterance === undefined ? {} : { body: JSON.stringify({ teacher_utterance }) }), credentials: "same-origin",
      headers: { ...(teacher_utterance === undefined ? {} : { "Content-Type": "application/json" }), Accept: "application/x-ndjson" }, method: "POST", signal,
    });
  } catch { throw unavailable_error(); }
  if (response.status === 401) throw new AssistantApiError(
    "当前身份已失效，请重新核对身份；旧消息与草稿已保留。", "assistant.authentication_required", "not_accepted",
  );
  if (!response.ok) {
    throw new AssistantApiError("发送未能完成，请核对状态。", "assistant.unavailable",
      [400, 404, 409, 422].includes(response.status) ? "not_accepted" : "unknown");
  }
  if (!response.headers.get("content-type")?.includes("application/x-ndjson")) {
    const snapshot = decodeAssistantConversation(await response.json());
    if (snapshot.conversation_ref !== conversation_ref) throw unavailable_error();
    return snapshot;
  }
  const reader = response.body?.getReader();
  if (!reader) throw unavailable_error();
  const decoder = new TextDecoder(); let buffer = ""; let accepted: AssistantConversation["active_run"] = null; let final: AssistantConversation | null = null;
  let accepted_text: string | undefined; let continuation_of: string | undefined;
  function consume(line: string) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.event === "error") {
      const terminalCodes = ["assistant.runtime_unavailable", "assistant.run_stale", "assistant.dependency",
        "assistant.tool_limit", "assistant.deadline", "assistant.budget", "assistant.context_limit", "assistant.output_limit", "assistant.data_policy"];
      if (!accepted || final || !terminalCodes.includes(event.code)) throw unavailable_error();
      throw new AssistantApiError("本轮处理已中止，未取得完整回答。请刷新核对对话。", "assistant.run_failed", "accepted_failed");
    }
    if (event.event !== "accepted" && event.event !== "completed") throw unavailable_error();
    const snapshot = decodeAssistantConversation(event.snapshot);
    if (snapshot.conversation_ref !== conversation_ref || final) throw unavailable_error();
    if (event.event === "accepted") {
      if (accepted || !snapshot.active_run) throw unavailable_error();
      const turn = snapshot.turns.find(turn => turn.turn_ref === snapshot.active_run!.turn_ref);
      if (!turn || turn.run_ref !== snapshot.active_run.run_ref || turn.generation !== snapshot.active_run.generation ||
          turn.generation !== snapshot.generation || turn.visible_state !== "active" ||
          (teacher_utterance === undefined ? !turn.continuation_of : turn.teacher_utterance !== teacher_utterance || !!turn.continuation_of) ||
          (snapshot.active_run.continuation_of !== undefined && snapshot.active_run.continuation_of !== turn.continuation_of) ||
          turn.display_payload !== null || turn.controls.length > 0 || snapshot.turns.length !== 1) throw unavailable_error();
      accepted_text = turn.teacher_utterance; continuation_of = turn.continuation_of;
      accepted = snapshot.active_run; onAccepted?.(snapshot);
    } else {
      if (!accepted || snapshot.active_run !== null || snapshot.generation < accepted.generation) throw unavailable_error();
      const turn = snapshot.turns.find(turn => turn.turn_ref === accepted!.turn_ref);
      if (!turn || turn.run_ref !== accepted.run_ref || turn.generation !== accepted.generation ||
          turn.visible_state === "active" || turn.teacher_utterance !== accepted_text || turn.continuation_of !== continuation_of) throw unavailable_error();
      final = snapshot;
    }
  }
  try {
    while (true) {
      const chunk = await reader.read(); buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) { consume(buffer.slice(0, end)); buffer = buffer.slice(end + 1); }
      if (chunk.done) break;
    }
    consume(buffer); if (!final) throw unavailable_error(); return final;
  } catch (error) {
    if (error instanceof AssistantApiError && error.delivery === "accepted_failed") throw error;
    throw unavailable_error();
  }
  finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}

/** Seals unknown/active natural-language sends; submitted formal actions remain intact. */
export async function closeAssistantConversation(conversation_ref: string, signal?: AbortSignal): Promise<AssistantConversation> {
  return request_snapshot(`/api/v1/assistant/conversations/${encodeURIComponent(conversation_ref)}/close`, {
    ...(signal ? { signal } : {}),
    credentials: "same-origin", method: "POST",
  });
}


/** stop 是纯机械请求，只提交服务端已发布的 run 与 generation。 */
export async function stopAssistantRun(
  conversation_ref: string,
  run_ref: string,
  generation: number,
): Promise<AssistantConversation> {
  return request_snapshot(
    `/api/v1/assistant/conversations/${encodeURIComponent(conversation_ref)}` +
      `/runs/${encodeURIComponent(run_ref)}/stop`,
    {
      body: JSON.stringify({ generation }),
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
}


/** 三类正式控件只能由真实点击调用各自无正文 POST。 */
export async function executeAssistantControl(
  conversation_ref: string,
  kind: AssistantControlKind,
  handle: string,
): Promise<AssistantConversation> {
  const route: Record<AssistantControlKind, string> = {
    action_offer: "action-offers",
    recovery_control: "recovery-controls",
    result_check_handle: "result-checks",
  };
  return request_snapshot(
    `/api/v1/assistant/conversations/${encodeURIComponent(conversation_ref)}` +
      `/controls/${route[kind]}/${encodeURIComponent(handle)}`,
    {
      credentials: "same-origin",
      method: "POST",
    },
  );
}


async function request_snapshot(
  path: string,
  init: RequestInit,
): Promise<AssistantConversation> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw unavailable_error();
  }
  const payload: unknown = await response.json().catch(() => undefined);
  if (response.status === 401) throw new AssistantApiError(
    "当前身份已失效，请重新核对身份；旧消息与草稿已保留。", "assistant.authentication_required", "not_accepted",
  );
  if (!response.ok) {
    throw unavailable_error();
  }
  try {
    return decodeAssistantConversation(payload);
  } catch {
    throw new AssistantApiError(
      "小静服务返回异常，请稍后刷新。",
      "assistant.response_invalid",
    );
  }
}


function unavailable_error(): AssistantApiError {
  return new AssistantApiError(
    "小静暂时不可用，请稍后重试。",
    "assistant.unavailable",
  );
}
