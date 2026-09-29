import { describe, expect, it } from "vitest";

import {
  decodeSchoolFactApply,
  decodeSchoolFactConfirmation,
  decodeSchoolFactPlan,
  decodeSchoolFactResult,
  decodeSchoolFactSource,
  SCHOOL_FACT_KINDS,
  schoolFactOperationsForKind,
} from "../src/features/school_facts/school_fact_contracts";


const SHA = "a".repeat(64);


function source_payload(): Record<string, unknown> {
  return {
    source_ref: "sf3source_1234567890",
    source_sha256: SHA,
    expires_at: "2026-08-21T12:00:00Z",
  };
}


function plan_payload(): Record<string, unknown> {
  return {
    plan_ref: "sf3plan_1234567890",
    plan_sha256: SHA,
    is_clean: false,
    issues: [
      {
        source_slot: "staff_file",
        sheet_code: "legacy_staff",
        row_number: 2,
        column_code: "employment_state",
        error_code: "fact.not_found",
      },
    ],
    changes: [
      {
        operation: "upsert",
        fact_kind: "person",
        source_key: "person:staff-001",
        expected_version: 0,
        sheet_code: "02_人员",
        row_number: 2,
        fields: [
          { field_code: "display_name", new_value: "同名学生甲" },
          { field_code: "aliases", new_value: ["旧名", "常用名"] },
          { field_code: "is_primary", new_value: true },
          { field_code: "priority", new_value: 1 },
        ],
      },
    ],
    status_decisions: [
      {
        source_slot: "staff_file",
        sheet_code: "legacy_staff",
        row_number: 2,
        field_code: "employment_state",
        original_status_value: "在岗",
        standard_value: "active",
        mapping_version: "school_statuses_v1",
      },
    ],
  };
}


describe("school fact HTTP contracts", () => {
  it("strictly decodes every published SF3 response", () => {
    const source = source_payload();
    const plan = plan_payload();
    const confirmation = {
      plan_ref: "sf3plan_1234567890",
      plan_sha256: SHA,
      credential: "sf3credential_1234567890",
      expires_at: "2026-08-21T12:00:00+08:00",
    };
    const applied = {
      plan_ref: "sf3plan_1234567890",
      batch_id: "00000000-0000-0000-0000-000000000123",
      target_version: 2,
      changed_count: 1,
      replayed: false,
      cleanup_pending: false,
    };
    const result = {
      plan_ref: "sf3plan_1234567890",
      state: "applied",
      batch_id: "00000000-0000-0000-0000-000000000123",
      target_version: 2,
      changed_count: 1,
    };

    expect(decodeSchoolFactSource(source)).toEqual(source);
    expect(decodeSchoolFactPlan(plan)).toEqual(plan);
    expect(decodeSchoolFactConfirmation(confirmation)).toEqual(confirmation);
    expect(decodeSchoolFactApply(applied)).toEqual(applied);
    expect(decodeSchoolFactResult(result)).toEqual(result);
    expect(decodeSchoolFactResult({
      plan_ref: "sf3plan_1234567890",
      state: "pending",
      batch_id: null,
      target_version: null,
      changed_count: null,
    }).state).toBe("pending");
  });

  it("publishes 19 fact kinds and only the legal operation set for each kind", () => {
    expect(SCHOOL_FACT_KINDS).toEqual([
      "school",
      "person",
      "student",
      "student_school_relationship",
      "login_mapping",
      "organization",
      "organization_campus_relationship",
      "assignment",
      "location",
      "location_alias",
      "dorm_room",
      "dorm_bed",
      "student_bed_residence",
      "person_contact",
      "student_contact",
      "official_contact",
      "policy",
      "policy_scope",
      "policy_version",
    ]);
    const roots = new Set(["school", "person", "student", "organization", "location"]);
    for (const fact_kind of SCHOOL_FACT_KINDS) {
      expect(schoolFactOperationsForKind(fact_kind)).toEqual(
        roots.has(fact_kind)
          ? ["upsert", "end", "revoke", "reactivate"]
          : ["upsert", "end", "revoke"],
      );
      const upsert = plan_payload();
      (upsert.changes as Array<Record<string, unknown>>)[0].fact_kind = fact_kind;
      expect(decodeSchoolFactPlan(upsert).changes[0]?.fact_kind).toBe(fact_kind);
    }

    for (const fact_kind of roots) {
      const payload = plan_payload();
      const change = (payload.changes as Array<Record<string, unknown>>)[0];
      change.fact_kind = fact_kind;
      change.operation = "reactivate";
      expect(decodeSchoolFactPlan(payload).changes[0]?.operation).toBe("reactivate");
    }

    const forged = plan_payload();
    const forged_change = (forged.changes as Array<Record<string, unknown>>)[0];
    forged_change.fact_kind = "assignment";
    forged_change.operation = "reactivate";
    expect(() => decodeSchoolFactPlan(forged)).toThrow();
  });

  it.each([
    ["plan", () => ({ ...plan_payload(), actor_context_ref: "private-actor" }), decodeSchoolFactPlan],
    ["issue", () => {
      const payload = plan_payload();
      (payload.issues as Array<Record<string, unknown>>)[0].raw_value = "13800000000";
      return payload;
    }, decodeSchoolFactPlan],
    ["field diff", () => {
      const payload = plan_payload();
      const change = (payload.changes as Array<Record<string, unknown>>)[0];
      (change.fields as Array<Record<string, unknown>>)[0].old_value = "私密旧值";
      return payload;
    }, decodeSchoolFactPlan],
    ["status decision", () => {
      const payload = plan_payload();
      (payload.status_decisions as Array<Record<string, unknown>>)[0].original_value = "在岗";
      return payload;
    }, decodeSchoolFactPlan],
    ["confirmation", () => ({
      plan_ref: "sf3plan_1234567890",
      plan_sha256: SHA,
      credential: "sf3credential_1234567890",
      expires_at: "2026-08-21T12:00:00Z",
      school_id: "forged-school",
    }), decodeSchoolFactConfirmation],
  ] as const)("rejects an extra authority or raw field in %s", (_label, build, decode) => {
    expect(() => decode(build())).toThrow("学校基础数据服务返回异常，请刷新后重试。");
  });

  it("rejects invalid refs, hashes, dates, enums and non-contract diff values", () => {
    expect(() => decodeSchoolFactSource({
      ...source_payload(),
      source_ref: "source/secret",
    })).toThrow();
    expect(() => decodeSchoolFactSource({
      ...source_payload(),
      source_sha256: "A".repeat(64),
    })).toThrow();
    expect(() => decodeSchoolFactConfirmation({
      plan_ref: "sf3plan_1234567890",
      plan_sha256: SHA,
      credential: "sf3credential_1234567890",
      expires_at: "2026-08-21 12:00:00",
    })).toThrow();

    const bad_kind = plan_payload();
    (bad_kind.changes as Array<Record<string, unknown>>)[0].fact_kind = "repair";
    expect(() => decodeSchoolFactPlan(bad_kind)).toThrow();

    const bad_operation = plan_payload();
    (bad_operation.changes as Array<Record<string, unknown>>)[0].operation = "delete";
    expect(() => decodeSchoolFactPlan(bad_operation)).toThrow();

    const bad_value = plan_payload();
    const change = (bad_value.changes as Array<Record<string, unknown>>)[0];
    (change.fields as Array<Record<string, unknown>>)[0].new_value = ["safe", 1];
    expect(() => decodeSchoolFactPlan(bad_value)).toThrow();
  });

  it("requires safe integer coordinates and coherent result nullability", () => {
    const fractional = plan_payload();
    (fractional.issues as Array<Record<string, unknown>>)[0].row_number = 2.5;
    expect(() => decodeSchoolFactPlan(fractional)).toThrow();

    expect(() => decodeSchoolFactResult({
      plan_ref: "sf3plan_1234567890",
      state: "applied",
      batch_id: null,
      target_version: null,
      changed_count: null,
    })).toThrow();
    expect(() => decodeSchoolFactResult({
      plan_ref: "sf3plan_1234567890",
      state: "pending",
      batch_id: "00000000-0000-0000-0000-000000000123",
      target_version: 2,
      changed_count: 1,
    })).toThrow();
  });

  it.each([" <在岗>", "在岗\n", "在岗😀", "<script>"])(
    "rejects an unsafe published status original %s",
    (original_status_value) => {
      const payload = plan_payload();
      (payload.status_decisions as Array<Record<string, unknown>>)[0]
        .original_status_value = original_status_value;

      expect(() => decodeSchoolFactPlan(payload)).toThrow(
        "学校基础数据服务返回异常，请刷新后重试。",
      );
    },
  );

  it("requires plan cleanliness to agree exactly with its issue list", () => {
    const clean = plan_payload();
    clean.is_clean = true;
    clean.issues = [];
    expect(decodeSchoolFactPlan(clean).is_clean).toBe(true);

    const false_clean = plan_payload();
    false_clean.is_clean = true;
    expect(() => decodeSchoolFactPlan(false_clean)).toThrow();

    const false_dirty = plan_payload();
    false_dirty.is_clean = false;
    false_dirty.issues = [];
    expect(() => decodeSchoolFactPlan(false_dirty)).toThrow();
  });

  it("accepts the published aggregate maxima for issues and status decisions", () => {
    const payload = plan_payload();
    const issue = (payload.issues as Array<Record<string, unknown>>)[0];
    const change = (payload.changes as Array<Record<string, unknown>>)[0];
    const decision = (payload.status_decisions as Array<Record<string, unknown>>)[0];
    payload.issues = Array(250_001).fill(issue);
    payload.changes = Array(50_000).fill(change);
    payload.status_decisions = Array(100_000).fill(decision);

    const decoded = decodeSchoolFactPlan(payload);

    expect(decoded.issues).toHaveLength(250_001);
    expect(decoded.changes).toHaveLength(50_000);
    expect(decoded.status_decisions).toHaveLength(100_000);
  });

  it("rejects change count max plus one with the fixed safe contract error", () => {
    const payload = plan_payload();
    const change = (payload.changes as Array<Record<string, unknown>>)[0];
    payload.changes = Array(50_001).fill(change);

    expect(() => decodeSchoolFactPlan(payload)).toThrow(
      "学校基础数据服务返回异常，请刷新后重试。",
    );
  });

  it("rejects issue count max plus one with the fixed safe contract error", () => {
    const payload = plan_payload();
    const issue = (payload.issues as Array<Record<string, unknown>>)[0];
    payload.issues = Array(250_002).fill(issue);

    expect(() => decodeSchoolFactPlan(payload)).toThrow(
      "学校基础数据服务返回异常，请刷新后重试。",
    );
  });

  it("rejects status-decision count max plus one with the fixed safe contract error", () => {
    const payload = plan_payload();
    const decision = (payload.status_decisions as Array<Record<string, unknown>>)[0];
    payload.status_decisions = Array(100_001).fill(decision);

    expect(() => decodeSchoolFactPlan(payload)).toThrow(
      "学校基础数据服务返回异常，请刷新后重试。",
    );
  });
});
