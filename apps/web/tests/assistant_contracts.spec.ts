import { describe, expect, it } from "vitest";

import {
  decodeAssistantConversation,
  isAssistantConversation,
} from "../src/features/assistant/assistant_contracts";


function conversation_payload(): Record<string, unknown> {
  return {
    version: "1",
    conversation_ref: "conversation.1",
    generation: 2,
    active_run: {
      turn_ref: "turn.2",
      run_ref: "run.2",
      generation: 2,
    },
    turns: [
      {
        turn_ref: "turn.1",
        run_ref: "run.1",
        generation: 1,
        teacher_utterance: "请处理我这次说的话",
        visible_state: "completed",
        display_kind: "card",
        display_payload: {
          title: "公开结果",
          rows: ["第一项", 2, true, null],
        },
        controls: [
          {
            kind: "action_offer",
            handle: "control.action.1",
            safe_label: "确认办理",
          },
          {
            kind: "recovery_control",
            handle: "control.recovery.1",
            safe_label: "放弃当前未提交内容",
          },
          {
            kind: "result_check_handle",
            handle: "control.check.1",
            safe_label: "核验结果",
          },
        ],
      },
    ],
  };
}

it("accepts optional system continuation origins while keeping old snapshots compatible", () => {
  const value = conversation_payload();
  expect(isAssistantConversation(value)).toBe(true);
  (value.turns as Record<string, unknown>[])[0]!.continuation_of = "turn.1";
  (value.active_run as Record<string, unknown>).continuation_of = "turn.1";
  expect(isAssistantConversation(value)).toBe(true);
  (value.turns as Record<string, unknown>[])[0]!.continuation_of = 23;
  expect(isAssistantConversation(value)).toBe(false);
});


describe("assistant HTTP contracts", () => {
  it("accepts only the published conversation snapshot shape", () => {
    const payload = conversation_payload();

    expect(isAssistantConversation(payload)).toBe(true);
    expect(decodeAssistantConversation(payload)).toEqual(payload);
  });

  it.each([
    ["conversation", { actor_context_ref: "actor.private" }],
    ["active run", { active_run: { request_id: "request.private" } }],
    ["display turn", { turn: { module_ref: "private.module" } }],
    ["control", { control: { state_revision: 7 } }],
  ])("rejects extra authority fields in the %s", (_label, mutation) => {
    const payload = conversation_payload();
    if ("actor_context_ref" in mutation) {
      Object.assign(payload, mutation);
    }
    if ("active_run" in mutation) {
      Object.assign(payload.active_run as Record<string, unknown>, mutation.active_run);
    }
    if ("turn" in mutation) {
      Object.assign(
        (payload.turns as Array<Record<string, unknown>>)[0],
        mutation.turn,
      );
    }
    if ("control" in mutation) {
      const turn = (payload.turns as Array<Record<string, unknown>>)[0];
      Object.assign(
        (turn.controls as Array<Record<string, unknown>>)[0],
        mutation.control,
      );
    }

    expect(isAssistantConversation(payload)).toBe(false);
    expect(() => decodeAssistantConversation(payload)).toThrow(
      "小静服务返回异常，请稍后刷新。",
    );
  });

  it.each([
    ["wrong version", { version: "2" }],
    ["wrong visible state", { visible_state: "cancelled" }],
    ["wrong display kind", { display_kind: "table" }],
    ["wrong control kind", { control_kind: "confirm_action" }],
  ])("rejects an invalid union member: %s", (_label, mutation) => {
    const payload = conversation_payload();
    const turn = (payload.turns as Array<Record<string, unknown>>)[0];
    const control = (turn.controls as Array<Record<string, unknown>>)[0];
    if ("version" in mutation) {
      payload.version = mutation.version;
    }
    if ("visible_state" in mutation) {
      turn.visible_state = mutation.visible_state;
    }
    if ("display_kind" in mutation) {
      turn.display_kind = mutation.display_kind;
    }
    if ("control_kind" in mutation) {
      control.kind = mutation.control_kind;
    }

    expect(isAssistantConversation(payload)).toBe(false);
  });

  it.each([
    null,
    [],
    { ...conversation_payload(), turns: {} },
    {
      ...conversation_payload(),
      turns: [
        {
          ...((conversation_payload().turns as Array<Record<string, unknown>>)[0]),
          display_payload: { invalid: undefined },
        },
      ],
    },
    {
      ...conversation_payload(),
      turns: [
        {
          ...((conversation_payload().turns as Array<Record<string, unknown>>)[0]),
          display_payload: Number.POSITIVE_INFINITY,
        },
      ],
    },
  ])("rejects a non-contract or non-JSON container", (payload) => {
    expect(isAssistantConversation(payload)).toBe(false);
  });

  it("enforces opaque refs, non-blank text, integer generations and nullable fields", () => {
    const valid = conversation_payload();
    valid.active_run = null;
    const turn = (valid.turns as Array<Record<string, unknown>>)[0];
    turn.display_kind = null;
    turn.display_payload = null;
    turn.controls = [];
    expect(isAssistantConversation(valid)).toBe(true);

    const invalid_ref = conversation_payload();
    invalid_ref.conversation_ref = "conversation/secret";
    expect(isAssistantConversation(invalid_ref)).toBe(false);

    const blank_label = conversation_payload();
    const blank_turn = (blank_label.turns as Array<Record<string, unknown>>)[0];
    const blank_control = (blank_turn.controls as Array<Record<string, unknown>>)[0];
    blank_control.safe_label = "   ";
    expect(isAssistantConversation(blank_label)).toBe(false);

    const fractional_generation = conversation_payload();
    fractional_generation.generation = 1.5;
    expect(isAssistantConversation(fractional_generation)).toBe(false);
  });
});
