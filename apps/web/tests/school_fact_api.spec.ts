import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SchoolFactApiError,
  confirmSchoolFactPlan,
  discardSchoolFactSource,
  fetchSchoolFactResult,
  preflightAdaptedSchoolFacts,
  preflightSchoolFactAdmin,
  preflightSchoolFactExcel,
  prepareSchoolFactConfirmation,
  uploadSchoolFactSource,
} from "../src/features/school_facts/school_fact_api";
import type { SchoolFactAdminChange } from "../src/features/school_facts/school_fact_contracts";


const SHA = "a".repeat(64);


afterEach(() => {
  vi.unstubAllGlobals();
});


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
    is_clean: true,
    issues: [],
    changes: [],
    status_decisions: [],
  };
}


function confirmation_payload(): Record<string, unknown> {
  return {
    plan_ref: "sf3plan_1234567890",
    plan_sha256: SHA,
    credential: "sf3credential_1234567890",
    expires_at: "2026-08-21T12:00:00Z",
  };
}


function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status });
}


describe("school fact API client", () => {
  it("uploads one in-memory xlsx and sends only the formal preflight contract", async () => {
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(source_payload(), 201))
      .mockResolvedValueOnce(response(plan_payload()));
    vi.stubGlobal("fetch", fetch_mock);
    const file = new File([new Uint8Array([1, 2, 3])], "current-school.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });

    const source = await uploadSchoolFactSource(file);
    await preflightSchoolFactExcel(source.source_ref, 7);

    expect(fetch_mock).toHaveBeenNthCalledWith(1, "/api/v1/school-facts/sources", {
      body: JSON.stringify({
        filename_summary: "current-school.xlsx",
        content_base64: "AQID",
      }),
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
    expect(fetch_mock).toHaveBeenNthCalledWith(2, "/api/v1/school-facts/plans/excel", {
      body: JSON.stringify({ source_ref: source.source_ref, base_version: 7 }),
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
  });

  it("preflights deterministic multi-source mappings without authority fields", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(response(plan_payload()));
    vi.stubGlobal("fetch", fetch_mock);

    await preflightAdaptedSchoolFacts(
      [
        { source_slot: "school_file", source_ref: "sf3source_1234567890" },
        { source_slot: "staff_file", source_ref: "sf3source_0987654321" },
      ],
      "legacy_school_v3",
      8,
    );

    expect(fetch_mock).toHaveBeenCalledTimes(1);
    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/school-facts/plans/adapted",
      {
        body: JSON.stringify({
          sources: [
            { source_slot: "school_file", source_ref: "sf3source_1234567890" },
            { source_slot: "staff_file", source_ref: "sf3source_0987654321" },
          ],
          mapping_version: "legacy_school_v3",
          base_version: 8,
        }),
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
    const [, init] = fetch_mock.mock.calls[0] as [string, RequestInit];
    expect(Object.keys(JSON.parse(String(init.body))).sort()).toEqual([
      "base_version",
      "mapping_version",
      "sources",
    ]);
  });

  it("sends a typed admin change without free JSON or client authority", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(response(plan_payload()));
    vi.stubGlobal("fetch", fetch_mock);
    const change: SchoolFactAdminChange = {
      operation: "upsert",
      fact_kind: "school",
      source_key: "school:main",
      expected_version: 1,
      effective_at: "2026-08-21T09:00:00+08:00",
      content: {
        business_code: "S001",
        display_name: "候选学校",
        timezone_name: "Asia/Shanghai",
        lifecycle_status: "active",
      },
    };

    await preflightSchoolFactAdmin(3, [change]);

    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/school-facts/plans/admin",
      {
        body: JSON.stringify({ base_version: 3, changes: [change] }),
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
    const request_text = String((fetch_mock.mock.calls[0] as [string, RequestInit])[1].body);
    expect(request_text).not.toContain("actor");
    expect(request_text).not.toContain("school_id");
    expect(request_text).not.toContain("permission");
  });

  it("keeps the confirmation credential in the confirm body and uses explicit idempotency headers", async () => {
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(confirmation_payload()))
      .mockResolvedValueOnce(response({
        plan_ref: "sf3plan_1234567890",
        batch_id: "00000000-0000-0000-0000-000000000123",
        target_version: 4,
        changed_count: 1,
        replayed: false,
        cleanup_pending: false,
      }));
    vi.stubGlobal("fetch", fetch_mock);

    const confirmation = await prepareSchoolFactConfirmation(
      "sf3plan_1234567890",
      "prepare-click-1",
    );
    await confirmSchoolFactPlan(
      "sf3plan_1234567890",
      confirmation.credential,
      "confirm-click-1",
    );

    expect(fetch_mock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/school-facts/plans/sf3plan_1234567890/confirmation",
      {
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Idempotency-Key": "prepare-click-1" },
        method: "POST",
      },
    );
    expect(fetch_mock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/school-facts/plans/sf3plan_1234567890/confirm",
      {
        body: JSON.stringify({ credential: "sf3credential_1234567890" }),
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": "confirm-click-1",
        },
        method: "POST",
      },
    );
    expect(fetch_mock.mock.calls.map(([url]) => String(url)).join(" ")).not.toContain(
      confirmation.credential,
    );
  });

  it("refreshes only the authoritative result with GET and no retry", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(response({
      plan_ref: "sf3plan_1234567890",
      state: "pending",
      batch_id: null,
      target_version: null,
      changed_count: null,
    }));
    vi.stubGlobal("fetch", fetch_mock);

    await fetchSchoolFactResult("sf3plan_1234567890");

    expect(fetch_mock).toHaveBeenCalledTimes(1);
    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/school-facts/plans/sf3plan_1234567890/result",
      {
        cache: "no-store",
        credentials: "same-origin",
        method: "GET",
      },
    );
  });

  it("discards one private source with an empty same-origin DELETE and zero retry", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch_mock);

    await discardSchoolFactSource("sf3source_1234567890");

    expect(fetch_mock).toHaveBeenCalledTimes(1);
    expect(fetch_mock).toHaveBeenCalledWith(
      "/api/v1/school-facts/sources/sf3source_1234567890",
      {
        cache: "no-store",
        credentials: "same-origin",
        method: "DELETE",
      },
    );
  });

  it("returns fixed safe errors for transport, backend and malformed success with zero retry", async () => {
    const canary = "PRIVATE-STUDENT-13800000000";
    const fetch_mock = vi.fn()
      .mockRejectedValueOnce(new Error(canary))
      .mockResolvedValueOnce(response({ code: "private", message: canary }, 503))
      .mockResolvedValueOnce(response({ ...plan_payload(), raw_value: canary }));
    vi.stubGlobal("fetch", fetch_mock);

    const transport = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);
    const backend = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);
    const malformed = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);

    expect(fetch_mock).toHaveBeenCalledTimes(3);
    expect(transport).toBeInstanceOf(SchoolFactApiError);
    expect(backend).toBeInstanceOf(SchoolFactApiError);
    expect(malformed).toBeInstanceOf(SchoolFactApiError);
    expect((transport as Error).message).toBe("学校基础数据通道暂时不可用，请稍后重试。");
    expect((backend as Error).message).toBe("学校基础数据通道暂时不可用，请稍后重试。");
    expect((malformed as Error).message).toBe("学校基础数据服务返回异常，请刷新后重试。");
    expect([transport, backend, malformed].map((error) => (error as Error).message).join(" "))
      .not.toContain(canary);
  });

  it("publishes only mechanically validated status coordinates and a fixed version-conflict message", async () => {
    const request_id = "00000000-0000-0000-0000-000000000321";
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response({
        code: "school_facts.status_mapping_required",
        message: "staff_file/legacy_staff 第 12 行的 employment_state 需要按服务端状态字典 school_statuses_v1 处理。",
        request_id,
      }, 422))
      .mockResolvedValueOnce(response({
        code: "school_facts.change_rejected",
        message: "PRIVATE-STUDENT-13800000000",
        request_id,
      }, 409));
    vi.stubGlobal("fetch", fetch_mock);

    const mapping_result = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);
    const conflict_result = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);
    expect(mapping_result).toBeInstanceOf(SchoolFactApiError);
    expect(conflict_result).toBeInstanceOf(SchoolFactApiError);
    const mapping = mapping_result as SchoolFactApiError;
    const conflict = conflict_result as SchoolFactApiError;

    expect(mapping.code).toBe("school_facts.status_mapping_required");
    expect(mapping.message).toContain("staff_file / legacy_staff / 第 12 行 / employment_state");
    expect(mapping.message).toContain("school_statuses_v1");
    expect(conflict.code).toBe("school_facts.change_rejected");
    expect(conflict.message).toBe("学校基础数据已变化或确认已失效，请重新预检。");
    expect(`${mapping.message} ${conflict.message}`).not.toContain("PRIVATE-STUDENT");
  });

  it("rejects a forged status error message instead of echoing its canary", async () => {
    const canary = "PRIVATE-STUDENT-13800000000";
    const fetch_mock = vi.fn().mockResolvedValue(response({
      code: "school_facts.status_mapping_ambiguous",
      message: `staff_file/${canary} 第 2 行的 employment_state 需要按服务端状态字典 school_statuses_v1 处理。`,
      request_id: "00000000-0000-0000-0000-000000000321",
    }, 422));
    vi.stubGlobal("fetch", fetch_mock);

    const caught = await preflightSchoolFactExcel(
      "sf3source_1234567890",
      1,
    ).catch((error: unknown) => error);
    expect(caught).toBeInstanceOf(SchoolFactApiError);
    const error = caught as SchoolFactApiError;

    expect(error.code).toBe("school_facts.unavailable");
    expect(error.message).toBe("学校基础数据通道暂时不可用，请稍后重试。");
    expect(error.message).not.toContain(canary);
  });
});
