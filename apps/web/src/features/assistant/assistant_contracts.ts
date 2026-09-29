export type AssistantControlKind =
  | "action_offer"
  | "recovery_control"
  | "result_check_handle";

export type AssistantVisibleState = "active" | "completed" | "stopped";
export type AssistantDisplayKind = "text" | "card";

export type AssistantJsonValue =
  | null
  | string
  | number
  | boolean
  | AssistantJsonValue[]
  | { [key: string]: AssistantJsonValue };

export interface AssistantActiveRun {
  continuation_of?: string;
  turn_ref: string;
  run_ref: string;
  generation: number;
}

export interface AssistantControl {
  kind: AssistantControlKind;
  handle: string;
  safe_label: string;
}

export interface AssistantDisplayTurn {
  continuation_of?: string;
  turn_ref: string;
  run_ref: string;
  generation: number;
  teacher_utterance: string;
  visible_state: AssistantVisibleState;
  display_kind: AssistantDisplayKind | null;
  display_payload: AssistantJsonValue;
  controls: AssistantControl[];
}

export interface AssistantConversation {
  version: "1";
  conversation_ref: string;
  generation: number;
  active_run: AssistantActiveRun | null;
  turns: AssistantDisplayTurn[];
}


/** 只接受正式 assistant HTTP Schema 的完整、精确公开快照。 */
export function isAssistantConversation(value: unknown): value is AssistantConversation {
  try {
    return is_assistant_conversation(value);
  } catch {
    return false;
  }
}


/** 解码失败只返回固定安全错误，不回显服务端正文或未知字段。 */
export function decodeAssistantConversation(value: unknown): AssistantConversation {
  if (!isAssistantConversation(value)) {
    throw new Error("小静服务返回异常，请稍后刷新。");
  }
  return value;
}


function is_assistant_conversation(value: unknown): value is AssistantConversation {
  return (
    is_record(value) &&
    has_exact_keys(value, [
      "active_run",
      "conversation_ref",
      "generation",
      "turns",
      "version",
    ]) &&
    value.version === "1" &&
    is_opaque_ref(value.conversation_ref) &&
    is_integer(value.generation) &&
    (value.active_run === null || is_active_run(value.active_run)) &&
    Array.isArray(value.turns) &&
    value.turns.every(is_display_turn)
  );
}


function is_active_run(value: unknown): value is AssistantActiveRun {
  return (
    is_record(value) &&
    has_exact_keys(value, ["generation", "run_ref", "turn_ref", ...("continuation_of" in value ? ["continuation_of"] : [])]) &&
    (!("continuation_of" in value) || is_opaque_ref(value.continuation_of)) &&
    is_opaque_ref(value.turn_ref) &&
    is_opaque_ref(value.run_ref) &&
    is_integer(value.generation)
  );
}


function is_display_turn(value: unknown): value is AssistantDisplayTurn {
  return (
    is_record(value) &&
    has_exact_keys(value, [
      "controls",
      "display_kind",
      "display_payload",
      "generation",
      "run_ref",
      "teacher_utterance",
      "turn_ref",
      "visible_state",
      ...("continuation_of" in value ? ["continuation_of"] : []),
    ]) &&
    (!("continuation_of" in value) || is_opaque_ref(value.continuation_of)) &&
    is_opaque_ref(value.turn_ref) &&
    is_opaque_ref(value.run_ref) &&
    is_integer(value.generation) &&
    is_non_blank_text(value.teacher_utterance) &&
    (value.visible_state === "active" ||
      value.visible_state === "completed" ||
      value.visible_state === "stopped") &&
    (value.display_kind === null ||
      value.display_kind === "text" ||
      value.display_kind === "card") &&
    is_json_value(value.display_payload, new WeakSet<object>()) &&
    Array.isArray(value.controls) &&
    value.controls.every(is_control)
  );
}


function is_control(value: unknown): value is AssistantControl {
  return (
    is_record(value) &&
    has_exact_keys(value, ["handle", "kind", "safe_label"]) &&
    (value.kind === "action_offer" ||
      value.kind === "recovery_control" ||
      value.kind === "result_check_handle") &&
    is_opaque_ref(value.handle) &&
    is_non_blank_text(value.safe_label)
  );
}


function is_opaque_ref(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 200 &&
    /^[A-Za-z0-9_.:-]+$/.test(value)
  );
}


function is_non_blank_text(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 8_000 &&
    /\S/.test(value)
  );
}


function is_integer(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}


function is_json_value(
  value: unknown,
  ancestors: WeakSet<object>,
): value is AssistantJsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (typeof value !== "object" || value === null || ancestors.has(value)) {
    return false;
  }
  ancestors.add(value);
  let valid: boolean;
  if (Array.isArray(value)) {
    valid = value.every((item) => is_json_value(item, ancestors));
  } else if (is_record(value)) {
    valid = Object.values(value).every((item) => is_json_value(item, ancestors));
  } else {
    valid = false;
  }
  ancestors.delete(value);
  return valid;
}


function is_record(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}


function has_exact_keys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Reflect.ownKeys(value);
  return (
    actual.every((key) => typeof key === "string") &&
    actual.length === expected.length &&
    expected.every((key) => Object.prototype.hasOwnProperty.call(value, key))
  );
}
