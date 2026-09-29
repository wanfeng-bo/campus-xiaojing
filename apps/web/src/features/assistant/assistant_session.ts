import {
  createAssistantConversation,
  continueAssistantOperation,
  closeAssistantConversation,
  AssistantApiError,
  executeAssistantControl,
  fetchAssistantConversation,
  sendAssistantTurn,
  stopAssistantRun,
} from "./assistant_api";
import type {
  AssistantControl,
  AssistantControlKind,
  AssistantConversation,
  AssistantDisplayTurn,
} from "./assistant_contracts";


export interface AssistantSessionApi {
  continueOperation?: (conversation_ref: string, handle: string, onAccepted?: (snapshot: AssistantConversation) => void, signal?: AbortSignal) => Promise<AssistantConversation>;
  createConversation: (signal?: AbortSignal, contextClaim?: string) => Promise<AssistantConversation>;
  fetchConversation: (conversation_ref: string) => Promise<AssistantConversation>;
  sendTurn: (
    conversation_ref: string,
    teacher_utterance: string,
    onAccepted?: (snapshot: AssistantConversation) => void,
    signal?: AbortSignal,
  ) => Promise<AssistantConversation>;
  closeConversation?: (conversation_ref: string, signal?: AbortSignal) => Promise<AssistantConversation>;
  stopRun: (
    conversation_ref: string,
    run_ref: string,
    generation: number,
  ) => Promise<AssistantConversation>;
  executeControl: (
    conversation_ref: string,
    kind: AssistantControlKind,
    handle: string,
  ) => Promise<AssistantConversation>;
}

export interface PendingMessage {
  id: number; text: string; status: "sending" | "uncertain" | "failed" | "stopped";
}

export interface AssistantSessionState {
  terminal_failure_run_ref: string | null;
  authentication_required: boolean;
  pending_message: PendingMessage | null;
  failed_messages: PendingMessage[];
  conversation_closed: boolean;
  navigation_uncertain: boolean;
  navigation_phase: "closing" | "creating" | "close_failed" | "create_failed" | null;
  snapshot: AssistantConversation | null;
  input: string;
  opening: boolean;
  starting_new: boolean;
  delivery_uncertain: boolean;
  refreshing: boolean;
  sending: boolean;
  stopping: boolean;
  pending_control: string | null;
  error_message: string | null;
}

export interface AssistantSessionController {
  getState: () => Readonly<AssistantSessionState>;
  subscribe: (listener: (state: Readonly<AssistantSessionState>) => void) => () => void;
  setInput: (value: string) => void;
  open: (contextClaim?: string) => Promise<boolean>;
  newConversation: () => Promise<boolean>;
  dispose: () => void;
  invalidateIdentity: () => void;
  refresh: () => Promise<boolean>;
  send: () => Promise<boolean>;
  retry: (text: string) => Promise<boolean>;
  stop: () => Promise<boolean>;
  activateControl: (control: AssistantControl) => Promise<boolean>;
  continueOperation: (proposal_ref: string) => Promise<boolean>;
  retryContinuation: (original_turn_ref: string) => Promise<boolean>;
}


const default_api: AssistantSessionApi = {
  createConversation: createAssistantConversation,
  continueOperation: continueAssistantOperation,
  closeConversation: closeAssistantConversation,
  executeControl: executeAssistantControl,
  fetchConversation: fetchAssistantConversation,
  sendTurn: sendAssistantTurn,
  stopRun: stopAssistantRun,
};


/** 创建不含业务词义的内存会话控制器；所有动作只编排正式 HTTP 端口。 */
export function createAssistantSessionController(
  api: AssistantSessionApi = default_api,
): AssistantSessionController {
  let state: AssistantSessionState = {
    terminal_failure_run_ref: null,
    authentication_required: false,
    pending_message: null,
    failed_messages: [],
    conversation_closed: false,
    navigation_uncertain: false,
    navigation_phase: null,
    snapshot: null,
    input: "",
    opening: false,
    starting_new: false,
    delivery_uncertain: false,
    refreshing: false,
    sending: false,
    stopping: false,
    pending_control: null,
    error_message: null,
  };
  let send_abort: AbortController | null = null;
  let navigation_abort: AbortController | null = null;
  let active_send_order = 0;
  let next_request_order = 0;
  let last_applied_order = 0;
  let lifecycle_start_order = 0;
  let uncertain_send_generation: number | null = null;
  const attempted_continuations = new Set<string>();
  const continuation_failure = "正式操作结果仍以原回执为准，后续回答未完成；可刷新核对对话。";
  const uncertain_message = "发送结果暂时无法核验，请先刷新本对话核对，勿重复发送。";
  const navigation_message = "新对话准备尚未确认，旧对话与草稿已保留。请再次点击新对话核对，暂不能发送。";
  let disposed = false;
  const current_request = (order: number) => !disposed && !state.authentication_required && order >= lifecycle_start_order;
  const listeners = new Set<(value: Readonly<AssistantSessionState>) => void>();

  function publish(patch: Partial<AssistantSessionState>): void {
    if (disposed) return;
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
  }

  function invalidateIdentity(): void {
    if (disposed || state.authentication_required) return;
    lifecycle_start_order = ++next_request_order;
    active_send_order = 0;
    send_abort?.abort(); navigation_abort?.abort();
    publish({ authentication_required: true, opening: false, starting_new: false,
      refreshing: false, sending: false, stopping: false, pending_control: null,
      pending_message: state.pending_message ? { ...state.pending_message, status: "uncertain" } : null,
      error_message: "当前身份已失效，请重新核对身份；旧消息与草稿已保留，暂不能继续操作。" });
  }
  function identityFailure(error: unknown, order: number): boolean {
    if (!(error instanceof AssistantApiError) || error.code !== "assistant.authentication_required") return false;
    if (current_request(order)) invalidateIdentity();
    return true;
  }

  function issue_request(): number {
    next_request_order += 1;
    return next_request_order;
  }

  function publish_error(message: string, request_order: number): void {
    if (current_request(request_order) && request_order >= last_applied_order) {
      publish({ error_message: message });
    }
  }

  function apply_snapshot(candidate: AssistantConversation, request_order: number): boolean {
    if (!current_request(request_order)) return false;
    const current = state.snapshot;
    if (current !== null) {
      if (candidate.conversation_ref !== current.conversation_ref) {
        return false;
      }
      if (candidate.generation < current.generation) {
        return false;
      }
      if (candidate.generation === current.generation) {
        const current_is_active = current.active_run !== null;
        const candidate_is_active = candidate.active_run !== null;
        if (!current_is_active && candidate_is_active) {
          return false;
        }
        if (
          current_is_active === candidate_is_active &&
          request_order < last_applied_order
        ) {
          return false;
        }
      }
    }
    last_applied_order = Math.max(last_applied_order, request_order);
    if (uncertain_send_generation !== null && candidate.generation > uncertain_send_generation) {
      uncertain_send_generation = null;
    }
    publish({
      snapshot: candidate,
      terminal_failure_run_ref: candidate.active_run?.run_ref === state.terminal_failure_run_ref ? state.terminal_failure_run_ref : null,
      ...(uncertain_send_generation === null && candidate.generation > (state.snapshot?.generation ?? 0) ? { pending_message: null } : {}),
      delivery_uncertain: uncertain_send_generation !== null,
      error_message: state.navigation_uncertain ? navigation_message : uncertain_send_generation === null ? null : uncertain_message,
    });
    return true;
  }

  // Close/create are mechanical requests. A missing response must not lock the UI forever.
  async function navigate_request(
    phase: "closing" | "creating",
    operation: (signal: AbortSignal) => Promise<AssistantConversation>,
  ): Promise<AssistantConversation> {
    const abort = new AbortController(); navigation_abort = abort;
    publish({ navigation_phase: phase });
    const interrupted = new Promise<never>((_resolve, reject) => {
      abort.signal.addEventListener("abort", () => reject(new Error("navigation interrupted")), { once: true });
    });
    const deadline = setTimeout(() => abort.abort(), 15_000);
    try {
      const snapshot = await Promise.race([operation(abort.signal), interrupted]);
      publish({ navigation_phase: null });
      return snapshot;
    } catch (error) {
      publish({ navigation_phase: phase === "closing" ? "close_failed" : "create_failed" });
      throw error;
    } finally {
      clearTimeout(deadline);
      if (navigation_abort === abort) navigation_abort = null;
    }
  }

  async function open(contextClaim?: string): Promise<boolean> {
    if (disposed || state.authentication_required || state.snapshot !== null || state.opening || state.starting_new) {
      return false;
    }
    publish({ error_message: null, opening: true });
    const request_order = issue_request();
    try {
      const candidate = await navigate_request("creating", signal => api.createConversation(signal, contextClaim));
      return apply_snapshot(candidate, request_order);
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      publish_error("小静暂时不可用，请稍后重试。", request_order);
      return false;
    } finally {
      if (current_request(request_order)) publish({ opening: false });
    }
  }

  async function refresh(): Promise<boolean> {
    const current = state.snapshot;
    if (disposed || state.authentication_required || current === null || state.starting_new || state.refreshing || state.pending_control !== null) {
      return false;
    }
    publish({ error_message: null, refreshing: true });
    const request_order = issue_request();
    try {
      const candidate = await api.fetchConversation(current.conversation_ref);
      return apply_snapshot(candidate, request_order);
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      publish_error("暂时没能刷新对话，请稍后重试。", request_order);
      return false;
    } finally {
      if (current_request(request_order)) publish({ refreshing: false });
    }
  }

  async function send(retry_text?: string, continuation?: { handle: string }): Promise<boolean> {
    const current = state.snapshot;
    const teacher_utterance = retry_text ?? state.input;
    if (
      disposed || state.authentication_required || state.conversation_closed || state.navigation_uncertain || state.starting_new || state.delivery_uncertain ||
      current === null ||
      current.active_run !== null ||
      state.opening ||
      state.sending ||
      state.stopping ||
      state.pending_control !== null ||
      (!continuation && (teacher_utterance.length > 8_000 || !/\S/.test(teacher_utterance))) ||
      (continuation && !api.continueOperation)
    ) {
      return false;
    }
    const request_order = issue_request(); active_send_order = request_order;
    let accepted_run_ref: string | null = null;
    send_abort = new AbortController();
    publish({ error_message: null, sending: true,
      ...(continuation ? {} : {
      failed_messages: state.pending_message?.status === "failed" ? [...state.failed_messages, state.pending_message] : state.failed_messages,
      ...(retry_text === undefined ? { input: "" } : {}),
      pending_message: { id: request_order, text: teacher_utterance, status: "sending" as const } }) });
    try {
      const candidate = await (continuation ? api.continueOperation! : api.sendTurn)(
        current.conversation_ref,
        continuation?.handle ?? teacher_utterance,
        (accepted) => {
          if (!current_request(request_order) || active_send_order !== request_order) return;
          accepted_run_ref = accepted.active_run?.run_ref ?? null;
          // Acceptance carries only this input/run, never old fact bodies.
          const previous = state.snapshot?.turns ?? [];
          const incoming = new Set(accepted.turns.map(turn => turn.turn_ref));
          const merged = { ...accepted, turns: [...previous.filter(turn => !incoming.has(turn.turn_ref)), ...accepted.turns] };
          if (apply_snapshot(merged, request_order)) publish({ pending_message: null });
        },
        send_abort.signal,
      );
      if (!current_request(request_order) || active_send_order !== request_order) return false;
      const incoming = new Set(candidate.turns.map(turn => turn.turn_ref));
      const merged = { ...candidate, turns: [...(state.snapshot?.turns ?? []).filter(turn => !incoming.has(turn.turn_ref)), ...candidate.turns] };
      const applied = apply_snapshot(merged, request_order);
      if (applied && active_send_order === request_order) publish({ pending_message: null });
      return applied;
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      if (current_request(request_order) && active_send_order === request_order &&
          error instanceof AssistantApiError && error.delivery === "accepted_failed") {
        if (accepted_run_ref !== null && state.snapshot?.active_run?.run_ref === accepted_run_ref) {
          publish({ terminal_failure_run_ref: accepted_run_ref, sending: false, error_message: error.message });
          // The error event has no snapshot. Read the authoritative result once;
          // do not hold navigation or retry the input while this GET is pending.
          const recovery_order = issue_request();
          const recovering_original_run = () => current_request(request_order) && active_send_order === request_order &&
            state.snapshot?.active_run?.run_ref === accepted_run_ref;
          try {
            const recovered = await api.fetchConversation(current.conversation_ref);
            if (recovering_original_run() && recovered.active_run?.run_ref !== accepted_run_ref) {
              apply_snapshot(recovered, recovery_order);
            }
          } catch (recovery_error) {
            if (identityFailure(recovery_error, recovery_order)) return false;
            if (recovering_original_run()) publish_error(
              "本轮处理已中止，暂时没能核对对话。请刷新核对，或新建对话继续。", recovery_order,
            );
          }
        }
        if (continuation && current_request(request_order) && active_send_order === request_order) publish({ error_message: continuation_failure });
        return false;
      }
      if (continuation && current_request(request_order) && active_send_order === request_order) {
        // Retry only the continuation handle. Never reconstruct a user input or replay a formal mutation.
        if (!(error instanceof AssistantApiError && error.delivery === "not_accepted") && state.snapshot?.generation === current.generation) {
          uncertain_send_generation = current.generation;
        }
        publish({ error_message: continuation_failure, delivery_uncertain: uncertain_send_generation !== null });
        return false;
      }
      if (current_request(request_order) && active_send_order === request_order && error instanceof AssistantApiError && error.delivery === "not_accepted") {
        publish({ pending_message: { id: request_order, text: teacher_utterance, status: "failed" }, error_message: "本次请求未获受理，可重试原消息。" });
        return false;
      }
      if (current_request(request_order) && active_send_order === request_order && state.snapshot?.conversation_ref === current.conversation_ref &&
        state.snapshot.generation <= current.generation) {
        uncertain_send_generation = current.generation;
        publish({ delivery_uncertain: true, error_message: uncertain_message, pending_message: { id: request_order, text: teacher_utterance, status: "uncertain" } });
      }
      if (current_request(request_order) && active_send_order === request_order && state.snapshot?.active_run) {
        publish({ error_message: "连接中断，回答状态待核对；可刷新、停止或新建对话。" });
      }
      return false;
    } finally {
      if (current_request(request_order) && active_send_order === request_order) publish({ sending: false });
    }
  }

  async function continueOperation(proposal_ref: string, retry = false): Promise<boolean> {
    const current = state.snapshot;
    if (!current || disposed || state.authentication_required || state.conversation_closed || state.navigation_uncertain ||
        state.starting_new || state.opening || state.refreshing || state.stopping || state.sending ||
        state.delivery_uncertain || state.pending_control !== null || current.active_run || !api.continueOperation) return false;
    const origin = current.turns.find(turn => continuation_refs(turn).some(item => item.proposal_ref === proposal_ref));
    if (!origin) return false;
    const latest = current.turns.at(-1);
    if (latest?.turn_ref !== origin.turn_ref && !(retry && latest?.continuation_of === origin.turn_ref)) return false;
    const reference = continuation_refs(origin).find(item => item.proposal_ref === proposal_ref)!;
    const key = `${current.conversation_ref}:${reference.handle}`;
    if (!retry && attempted_continuations.has(key)) return false;
    attempted_continuations.add(key);
    return send(undefined, reference);
  }

  async function retryContinuation(original_turn_ref: string): Promise<boolean> {
    const origin = state.snapshot?.turns.find(turn => turn.turn_ref === original_turn_ref);
    const references = origin ? continuation_refs(origin) : [];
    // A retry must identify one existing original operation, never guess among operations.
    return references.length === 1 ? continueOperation(references[0]!.proposal_ref, true) : false;
  }

  async function stop(): Promise<boolean> {
    const current = state.snapshot;
    const active = current?.active_run;
    if (!disposed && !state.authentication_required && current && !active && (state.sending || state.delivery_uncertain) && api.closeConversation && !state.stopping) {
      publish({ stopping: true }); const order = issue_request();
      try {
        const closed = await api.closeConversation(current.conversation_ref);
        if (!current_request(order)) return false;
        apply_snapshot(closed, order); send_abort?.abort(); active_send_order = 0; uncertain_send_generation = null;
        publish({ conversation_closed: true, navigation_uncertain: false, sending: false, delivery_uncertain: false,
          pending_message: state.pending_message ? { ...state.pending_message, status: "stopped" } : null,
          error_message: "本对话已停止接收消息；草稿保留，可新建对话继续。" }); return true;
      } catch (error) { if (identityFailure(error, order)) return false; publish_error("停止尚未确认，请刷新或重试；不会重发原消息。", order); return false; }
      finally { if (current_request(order)) publish({ stopping: false }); }
    }
    if (
      disposed || state.authentication_required || state.starting_new ||
      current === null ||
      active === null ||
      active === undefined ||
      state.stopping ||
      state.pending_control !== null
    ) {
      return false;
    }
    publish({ error_message: null, stopping: true });
    const request_order = issue_request();
    try {
      const candidate = await api.stopRun(
        current.conversation_ref,
        active.run_ref,
        active.generation,
      );
      const applied = apply_snapshot(candidate, request_order);
      if (applied) { send_abort?.abort(); active_send_order = 0; publish({ sending: false }); }
      return applied;
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      publish_error("暂时没能停止这次处理，请刷新后重试。", request_order);
      return false;
    } finally {
      if (current_request(request_order)) publish({ stopping: false });
    }
  }

  async function activateControl(control: AssistantControl): Promise<boolean> {
    const current = state.snapshot;
    if (
      disposed || state.authentication_required || state.starting_new ||
      current === null ||
      state.pending_control !== null ||
      state.refreshing ||
      state.stopping ||
      state.sending ||
      !contains_control(current, control)
    ) {
      return false;
    }
    publish({ error_message: null, pending_control: control.handle });
    const request_order = issue_request();
    try {
      const candidate = await api.executeControl(
        current.conversation_ref,
        control.kind,
        control.handle,
      );
      const applied = apply_snapshot(candidate, request_order);
      if (applied && control.kind !== "recovery_control") {
        const origin = current.turns.find(turn => turn.controls.some(item => item.handle === control.handle && item.kind === control.kind));
        const updated = candidate.turns.find(turn => turn.turn_ref === origin?.turn_ref);
        const references = updated ? continuation_refs(updated) : [];
        publish({ pending_control: null });
        // A result-check control means the original operation is still awaiting
        // an authoritative outcome. Do not consume its one-shot continuation.
        if (references.length === 1 && !updated?.controls.some(item => item.kind === "result_check_handle")) {
          await continueOperation(references[0]!.proposal_ref);
        }
      }
      return applied;
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      publish_error("这次点击暂时没能处理，请刷新后重试。", request_order);
      return false;
    } finally {
      if (current_request(request_order) && state.pending_control === control.handle) {
        publish({ pending_control: null });
      }
    }
  }

  async function newConversation(): Promise<boolean> {
    const previous = state.snapshot;
    if (disposed || state.authentication_required || previous === null || state.opening || state.starting_new ||
      state.stopping || state.refreshing || state.pending_control !== null) return false;
    publish({ starting_new: true, error_message: null });
    const request_order = issue_request();
    try {
      if (api.closeConversation) {
        const closed = await navigate_request("closing", signal => api.closeConversation!(previous.conversation_ref, signal));
        if (!current_request(request_order)) return false;
        apply_snapshot(closed, request_order);
        send_abort?.abort(); active_send_order = 0; uncertain_send_generation = null;
        publish({ conversation_closed: true, navigation_uncertain: false, sending: false, delivery_uncertain: false,
          pending_message: state.pending_message ? { ...state.pending_message, status: "stopped" } : null });
        const fresh = await navigate_request("creating", signal => api.createConversation(signal));
        if (!current_request(request_order) || fresh.conversation_ref === previous.conversation_ref || fresh.generation !== 0 || fresh.active_run !== null || fresh.turns.length !== 0) return false;
        lifecycle_start_order = request_order;
        publish({ snapshot: null, pending_message: null, failed_messages: [], conversation_closed: false, navigation_uncertain: false, stopping: false });
        return apply_snapshot(fresh, request_order);
      }
      const latest = await api.fetchConversation(previous.conversation_ref);
      if (!current_request(request_order) || latest.conversation_ref !== previous.conversation_ref) return false;
      apply_snapshot(latest, request_order);
      const observed = state.snapshot!;
      if (observed.active_run !== null) {
        const active = observed.active_run;
        const stopped = await api.stopRun(previous.conversation_ref, active.run_ref, active.generation);
        if (!current_request(request_order) || stopped.conversation_ref !== previous.conversation_ref || stopped.active_run !== null) {
          throw new Error("previous run remains active");
        }
        apply_snapshot(stopped, request_order);
      } else if ((state.sending || state.delivery_uncertain) &&
        observed.generation <= (uncertain_send_generation ?? previous.generation)) {
        publish_error("旧对话的发送状态仍待核验，请刷新后再新建对话。", request_order);
        return false;
      }
      const fresh = await navigate_request("creating", signal => api.createConversation(signal));
      if (!current_request(request_order)) return false;
      if (fresh.conversation_ref === previous.conversation_ref || fresh.generation !== 0 ||
        fresh.active_run !== null || fresh.turns.length !== 0) throw new Error("not a fresh conversation");
      lifecycle_start_order = request_order;
      uncertain_send_generation = null;
      publish({ snapshot: null, input: "", sending: false, stopping: false, delivery_uncertain: false, navigation_uncertain: false });
      return apply_snapshot(fresh, request_order);
    } catch (error) {
      if (identityFailure(error, request_order)) return false;
      if (current_request(request_order) && !state.conversation_closed) publish({ navigation_uncertain: true });
      publish_error(navigation_message, request_order);
      return false;
    } finally {
      if (current_request(request_order)) publish({ starting_new: false });
    }
  }

  return {
    activateControl,
    continueOperation,
    retryContinuation,
    newConversation,
    invalidateIdentity,
    dispose: () => { disposed = true; send_abort?.abort(); navigation_abort?.abort(); listeners.clear(); },
    getState: () => state,
    open,
    refresh,
    send: () => send(),
    retry: (text: string) => send(text),
    setInput: (value: string) => publish({ input: value }),
    stop,
    subscribe: (listener) => {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },
  };
}

function continuation_refs(turn: AssistantDisplayTurn): { handle: string; proposal_ref: string }[] {
  const payload = turn.display_payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || !Array.isArray(payload.continuations)) return [];
  return payload.continuations.flatMap(item => {
    if (!item || typeof item !== "object" || Array.isArray(item) ||
        typeof item.handle !== "string" || !/^[A-Za-z0-9_.:-]{1,200}$/.test(item.handle) ||
        typeof item.proposal_ref !== "string" || !/^[A-Za-z0-9_.:-]{1,200}$/.test(item.proposal_ref)) return [];
    return [{ handle: item.handle, proposal_ref: item.proposal_ref }];
  });
}


function contains_control(
  snapshot: AssistantConversation,
  expected: AssistantControl,
): boolean {
  return snapshot.turns.some((turn) =>
    turn.controls.some((control) =>
      control.kind === expected.kind &&
      control.handle === expected.handle &&
      control.safe_label === expected.safe_label,
    ),
  );
}
