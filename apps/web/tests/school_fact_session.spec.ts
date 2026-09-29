import { describe, expect, it, vi } from "vitest";

import {
  createSchoolFactSessionController,
  type SchoolFactSessionApi,
} from "../src/features/school_facts/school_fact_session";
import { SchoolFactApiError } from "../src/features/school_facts/school_fact_api";
import type {
  SchoolFactAdminChange,
  SchoolFactApply,
  SchoolFactConfirmation,
  SchoolFactPlan,
  SchoolFactResult,
  SchoolFactSource,
} from "../src/features/school_facts/school_fact_contracts";


const SHA = "a".repeat(64);


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


function source(ref = "sf3source_1234567890"): SchoolFactSource {
  return {
    source_ref: ref,
    source_sha256: SHA,
    expires_at: "2026-08-21T12:00:00Z",
  };
}


function plan(ref = "sf3plan_1234567890", clean = true): SchoolFactPlan {
  return {
    plan_ref: ref,
    plan_sha256: SHA,
    is_clean: clean,
    issues: clean ? [] : [{
      source_slot: "formal_workbook",
      sheet_code: "02_人员",
      row_number: 2,
      column_code: "display_name",
      error_code: "fact.required",
    }],
    changes: [{
      operation: "upsert",
      fact_kind: "person",
      source_key: "person:001",
      expected_version: 0,
      sheet_code: "02_人员",
      row_number: 2,
      fields: [{ field_code: "display_name", new_value: "同名人员甲" }],
    }],
    status_decisions: [],
  };
}


function confirmation(ref = "sf3plan_1234567890"): SchoolFactConfirmation {
  return {
    plan_ref: ref,
    plan_sha256: SHA,
    credential: "sf3credential_PRIVATE_1234567890",
    expires_at: "2026-08-21T12:00:00Z",
  };
}


function applied(ref = "sf3plan_1234567890"): SchoolFactApply {
  return {
    plan_ref: ref,
    batch_id: "00000000-0000-0000-0000-000000000123",
    target_version: 2,
    changed_count: 1,
    replayed: false,
    cleanup_pending: false,
  };
}


function admin_change(): SchoolFactAdminChange {
  return {
    operation: "upsert",
    fact_kind: "person",
    source_key: "person:001",
    expected_version: 0,
    effective_at: "2026-08-21T09:00:00+08:00",
    content: {
      display_name: "同名人员甲",
      person_kind: "staff",
      lifecycle_status: "active",
    },
  };
}


function boarding_move_changes(): SchoolFactAdminChange[] {
  const effective_at = "2026-08-21T09:00:00+08:00";
  return [
    {
      operation: "end",
      fact_kind: "student_bed_residence",
      source_key: "residence:student-001:old",
      expected_version: 1,
      effective_at,
      content: null,
    },
    {
      operation: "upsert",
      fact_kind: "student_bed_residence",
      source_key: "residence:student-001:new",
      expected_version: 0,
      effective_at,
      content: {
        student_source_key: "student:001",
        bed_source_key: "bed:new-01",
        valid_to: "2026-12-31T18:00:00+08:00",
      },
    },
  ];
}


function sized_file(name: string, size: number): File {
  const file = new File(["x"], name);
  Object.defineProperty(file, "size", { configurable: true, value: size });
  return file;
}


function fake_api(): {
  api: SchoolFactSessionApi;
  discardSource: ReturnType<typeof vi.fn>;
  uploadSource: ReturnType<typeof vi.fn>;
  preflightExcel: ReturnType<typeof vi.fn>;
  preflightAdmin: ReturnType<typeof vi.fn>;
  preflightAdapted: ReturnType<typeof vi.fn>;
  prepareConfirmation: ReturnType<typeof vi.fn>;
  confirmPlan: ReturnType<typeof vi.fn>;
  fetchResult: ReturnType<typeof vi.fn>;
} {
  const uploadSource = vi.fn();
  const discardSource = vi.fn().mockResolvedValue(undefined);
  const preflightExcel = vi.fn();
  const preflightAdmin = vi.fn();
  const preflightAdapted = vi.fn();
  const prepareConfirmation = vi.fn();
  const confirmPlan = vi.fn();
  const fetchResult = vi.fn();
  return {
    api: {
      confirmPlan,
      discardSource,
      fetchResult,
      preflightAdapted,
      preflightAdmin,
      preflightExcel,
      prepareConfirmation,
      uploadSource,
    },
    confirmPlan,
    discardSource,
    fetchResult,
    preflightAdapted,
    preflightAdmin,
    preflightExcel,
    prepareConfirmation,
    uploadSource,
  };
}


describe("school fact in-memory session", () => {
  it("does not issue requests on construction or disposal and keeps no browser-restorable state", () => {
    const { api, uploadSource, preflightAdmin, prepareConfirmation } = fake_api();
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");

    expect(controller.getState()).toEqual({
      plan: null,
      confirmation_ready: false,
      confirmation_locked: false,
      confirmation_expires_at: null,
      apply_result: null,
      result: null,
      busy_write: null,
      refreshing_result: false,
      error_message: null,
    });
    controller.dispose();

    expect(uploadSource).not.toHaveBeenCalled();
    expect(preflightAdmin).not.toHaveBeenCalled();
    expect(prepareConfirmation).not.toHaveBeenCalled();
    expect(controller.getState().plan).toBeNull();
  });

  it("runs upload then formal preflight once, rejects double-click and retains no File or raw bytes", async () => {
    const upload = deferred<SchoolFactSource>();
    const { api, uploadSource, discardSource, preflightExcel } = fake_api();
    uploadSource.mockReturnValue(upload.promise);
    preflightExcel.mockResolvedValue(plan());
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const file = new File(["PRIVATE-WORKBOOK-CANARY"], "private-school.xlsx");

    const first = controller.preflightExcelFromClick(file, 1);
    expect(await controller.preflightExcelFromClick(file, 1)).toBe(false);
    upload.resolve(source());

    expect(await first).toBe(true);
    expect(uploadSource).toHaveBeenCalledTimes(1);
    expect(preflightExcel).toHaveBeenCalledTimes(1);
    expect(preflightExcel).toHaveBeenCalledWith("sf3source_1234567890", 1);
    expect(discardSource).toHaveBeenCalledWith("sf3source_1234567890");
    expect(JSON.stringify(controller.getState())).not.toContain("PRIVATE-WORKBOOK-CANARY");
    expect(JSON.stringify(controller.getState())).not.toContain("private-school.xlsx");
  });

  it("rejects an oversized workbook before reading or uploading it", async () => {
    const { api, uploadSource } = fake_api();
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const oversized = new File(
      [new Uint8Array(10 * 1024 * 1024 + 1)],
      "oversized.xlsx",
    );

    expect(await controller.preflightExcelFromClick(oversized, 1)).toBe(false);
    expect(uploadSource).not.toHaveBeenCalled();
  });

  it("makes every write mutually exclusive without a hidden queue", async () => {
    const upload = deferred<SchoolFactSource>();
    const { api, preflightAdmin, prepareConfirmation, uploadSource } = fake_api();
    uploadSource.mockReturnValue(upload.promise);
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const running = controller.preflightExcelFromClick(new File(["x"], "facts.xlsx"), 1);

    expect(await controller.preflightAdminFromClick(1, [admin_change()])).toBe(false);
    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(preflightAdmin).not.toHaveBeenCalled();
    expect(prepareConfirmation).not.toHaveBeenCalled();

    upload.reject(new Error("synthetic"));
    expect(await running).toBe(false);
    expect(controller.getState().busy_write).toBeNull();
  });

  it("submits one boarding move as end-old plus upsert-new in the same admin preflight", async () => {
    const { api, preflightAdmin } = fake_api();
    preflightAdmin.mockResolvedValue(plan());
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const changes = boarding_move_changes();

    expect(await controller.preflightAdminFromClick(7, changes)).toBe(true);

    expect(preflightAdmin).toHaveBeenCalledTimes(1);
    expect(preflightAdmin).toHaveBeenCalledWith(7, changes);
    expect(changes[0].effective_at).toBe(changes[1].effective_at);
    expect(changes[1].content).toMatchObject({
      valid_to: "2026-12-31T18:00:00+08:00",
    });
    expect(changes[1].content).not.toHaveProperty("arrived_from_source_key");
    expect(changes[1].content).not.toHaveProperty("departed_to_source_key");
  });

  it("uploads adapted sources in declared order and submits one deterministic mapping request", async () => {
    const { api, uploadSource, preflightAdapted } = fake_api();
    uploadSource
      .mockResolvedValueOnce(source("sf3source_1234567890"))
      .mockResolvedValueOnce(source("sf3source_0987654321"));
    preflightAdapted.mockResolvedValue(plan());
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const school_file = new File(["school"], "school.xlsx");
    const staff_file = new File(["staff"], "staff.xlsx");

    expect(await controller.preflightAdaptedFromClick([
      { source_slot: "school_file", file: school_file },
      { source_slot: "staff_file", file: staff_file },
    ], "legacy_school_v3", 4)).toBe(true);

    expect(uploadSource.mock.calls.map(([file]) => (file as File).name)).toEqual([
      "school.xlsx",
      "staff.xlsx",
    ]);
    expect(preflightAdapted).toHaveBeenCalledWith(
      [
        { source_slot: "school_file", source_ref: "sf3source_1234567890" },
        { source_slot: "staff_file", source_ref: "sf3source_0987654321" },
      ],
      "legacy_school_v3",
      4,
    );
  });

  it("rejects adapted input above eight files or 32 MiB total before any upload", async () => {
    const { api, uploadSource } = fake_api();
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const nine = Array.from({ length: 9 }, (_, index) => ({
      source_slot: `source_${index}`,
      file: sized_file(`source-${index}.xlsx`, 1),
    }));
    const over_total = [
      { source_slot: "source_a", file: sized_file("a.xlsx", 10 * 1024 * 1024) },
      { source_slot: "source_b", file: sized_file("b.xlsx", 10 * 1024 * 1024) },
      { source_slot: "source_c", file: sized_file("c.xlsx", 10 * 1024 * 1024) },
      { source_slot: "source_d", file: sized_file("d.xlsx", 2 * 1024 * 1024 + 1) },
    ];

    expect(await controller.preflightAdaptedFromClick(nine, "legacy_school_v3", 1))
      .toBe(false);
    expect(await controller.preflightAdaptedFromClick(
      over_total,
      "legacy_school_v3",
      1,
    )).toBe(false);
    expect(uploadSource).not.toHaveBeenCalled();
  });

  it("best-effort wipes private sources after dirty or failed preflight without hiding the primary error", async () => {
    const first = fake_api();
    first.uploadSource.mockResolvedValue(source());
    first.preflightExcel.mockResolvedValue(plan("sf3plan_1234567890", false));
    const dirty = createSchoolFactSessionController(first.api, () => "idempotency-1");

    expect(await dirty.preflightExcelFromClick(new File(["x"], "dirty.xlsx"), 1)).toBe(true);
    expect(first.discardSource).toHaveBeenCalledTimes(1);
    expect(first.discardSource).toHaveBeenCalledWith("sf3source_1234567890");

    const second = fake_api();
    second.uploadSource.mockResolvedValue(source("sf3source_0987654321"));
    second.preflightExcel.mockRejectedValue(new SchoolFactApiError(
      "状态映射需人工处理：formal_workbook / 02_人员 / 第 2 行 / lifecycle_status；字典 school_statuses_v1。",
      "school_facts.status_mapping_required",
    ));
    second.discardSource.mockRejectedValue(new Error("cleanup unavailable"));
    const failed = createSchoolFactSessionController(second.api, () => "idempotency-1");

    expect(await failed.preflightExcelFromClick(new File(["x"], "failed.xlsx"), 1)).toBe(false);
    expect(second.discardSource).toHaveBeenCalledWith("sf3source_0987654321");
    expect(failed.getState().error_message).toContain("formal_workbook / 02_人员 / 第 2 行");
  });

  it("wipes earlier adapted uploads when a later upload fails", async () => {
    const { api, uploadSource, discardSource, preflightAdapted } = fake_api();
    uploadSource
      .mockResolvedValueOnce(source("sf3source_1234567890"))
      .mockRejectedValueOnce(new Error("second upload failed"));
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");

    expect(await controller.preflightAdaptedFromClick([
      { source_slot: "school_file", file: new File(["school"], "school.xlsx") },
      { source_slot: "staff_file", file: new File(["staff"], "staff.xlsx") },
    ], "legacy_school_v3", 1)).toBe(false);

    expect(preflightAdapted).not.toHaveBeenCalled();
    expect(discardSource).toHaveBeenCalledTimes(1);
    expect(discardSource).toHaveBeenCalledWith("sf3source_1234567890");
  });

  it("prepares and consumes a credential only through explicit click methods", async () => {
    const { api, preflightAdmin, prepareConfirmation, confirmPlan } = fake_api();
    preflightAdmin.mockResolvedValue(plan());
    prepareConfirmation.mockResolvedValue(confirmation());
    const confirm_result = deferred<SchoolFactApply>();
    confirmPlan.mockReturnValue(confirm_result.promise);
    const keys = ["prepare-click-1", "confirm-click-1"];
    const controller = createSchoolFactSessionController(api, () => keys.shift() ?? "unexpected");
    await controller.preflightAdminFromClick(1, [admin_change()]);

    expect(await controller.prepareConfirmationFromClick()).toBe(true);
    expect(prepareConfirmation).toHaveBeenCalledWith(
      "sf3plan_1234567890",
      "prepare-click-1",
    );
    expect(controller.getState().confirmation_ready).toBe(true);
    expect(JSON.stringify(controller.getState())).not.toContain("sf3credential_PRIVATE");
    expect("confirmFromText" in controller).toBe(false);

    const first = controller.confirmFromClick();
    expect(await controller.confirmFromClick()).toBe(false);
    confirm_result.resolve(applied());

    expect(await first).toBe(true);
    expect(confirmPlan).toHaveBeenCalledTimes(1);
    expect(confirmPlan).toHaveBeenCalledWith(
      "sf3plan_1234567890",
      "sf3credential_PRIVATE_1234567890",
      "confirm-click-1",
    );
    expect(controller.getState().confirmation_ready).toBe(false);
    expect(controller.getState().confirmation_locked).toBe(true);
    expect(controller.getState().apply_result).toEqual(applied());
  });

  it("reuses one prepare idempotency key after response loss and rotates it only for a new plan", async () => {
    const { api, preflightAdmin, prepareConfirmation } = fake_api();
    preflightAdmin
      .mockResolvedValueOnce(plan("sf3plan_1234567890"))
      .mockResolvedValueOnce(plan("sf3plan_0987654321"));
    prepareConfirmation
      .mockRejectedValueOnce(new Error("prepare response lost"))
      .mockResolvedValueOnce(confirmation("sf3plan_1234567890"))
      .mockResolvedValueOnce(confirmation("sf3plan_0987654321"));
    const key_factory = vi.fn()
      .mockReturnValueOnce("prepare-k1")
      .mockReturnValueOnce("prepare-k2");
    const controller = createSchoolFactSessionController(api, key_factory);
    await controller.preflightAdminFromClick(1, [admin_change()]);

    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(controller.getState().error_message).toContain("再次点击“准备正式确认”");
    expect(await controller.prepareConfirmationFromClick()).toBe(true);
    expect(prepareConfirmation).toHaveBeenNthCalledWith(
      1,
      "sf3plan_1234567890",
      "prepare-k1",
    );
    expect(prepareConfirmation).toHaveBeenNthCalledWith(
      2,
      "sf3plan_1234567890",
      "prepare-k1",
    );
    expect(key_factory).toHaveBeenCalledTimes(1);

    await controller.preflightAdminFromClick(1, [admin_change()]);
    expect(await controller.prepareConfirmationFromClick()).toBe(true);
    expect(prepareConfirmation).toHaveBeenNthCalledWith(
      3,
      "sf3plan_0987654321",
      "prepare-k2",
    );
    expect(key_factory).toHaveBeenCalledTimes(2);
  });

  it("never reopens confirmation after confirm was sent, including after result recovery", async () => {
    const { api, preflightAdmin, prepareConfirmation, confirmPlan, fetchResult } = fake_api();
    preflightAdmin.mockResolvedValue(plan());
    prepareConfirmation.mockResolvedValue(confirmation());
    confirmPlan.mockRejectedValue(new Error("outcome unknown"));
    fetchResult.mockResolvedValue({
      plan_ref: "sf3plan_1234567890",
      state: "applied",
      batch_id: "00000000-0000-0000-0000-000000000123",
      target_version: 2,
      changed_count: 1,
    });
    const controller = createSchoolFactSessionController(api, () => "click-id");
    await controller.preflightAdminFromClick(1, [admin_change()]);
    await controller.prepareConfirmationFromClick();

    expect(await controller.confirmFromClick()).toBe(false);
    expect(controller.getState().confirmation_locked).toBe(true);
    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(await controller.preflightAdminFromClick(1, [admin_change()])).toBe(false);
    expect(prepareConfirmation).toHaveBeenCalledTimes(1);
    expect(preflightAdmin).toHaveBeenCalledTimes(1);

    expect(await controller.refreshResult()).toBe(true);
    expect(controller.getState().result?.state).toBe("applied");
    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(prepareConfirmation).toHaveBeenCalledTimes(1);
    expect(confirmPlan).toHaveBeenCalledTimes(1);
  });

  it("invalidates the rejected plan and permits a new preflight after a strict zero-write 409", async () => {
    const { api, preflightAdmin, prepareConfirmation, confirmPlan } = fake_api();
    preflightAdmin
      .mockResolvedValueOnce(plan("sf3plan_1234567890"))
      .mockResolvedValueOnce(plan("sf3plan_0987654321"));
    prepareConfirmation.mockResolvedValue(confirmation());
    confirmPlan.mockRejectedValue(new SchoolFactApiError(
      "学校基础数据已变化或确认已失效，请重新预检。",
      "school_facts.change_rejected",
    ));
    const controller = createSchoolFactSessionController(api, () => "click-id");
    await controller.preflightAdminFromClick(1, [admin_change()]);
    await controller.prepareConfirmationFromClick();

    expect(await controller.confirmFromClick()).toBe(false);
    expect(controller.getState().confirmation_locked).toBe(false);
    expect(controller.getState().plan).toBeNull();
    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(await controller.preflightAdminFromClick(1, [admin_change()])).toBe(true);
    expect(preflightAdmin).toHaveBeenCalledTimes(2);
    expect(controller.getState().plan?.plan_ref).toBe("sf3plan_0987654321");
  });

  it("blocks result refresh while confirm is in flight and never regresses applied to pending", async () => {
    const confirming = deferred<SchoolFactApply>();
    const { api, preflightAdmin, prepareConfirmation, confirmPlan, fetchResult } = fake_api();
    preflightAdmin.mockResolvedValue(plan());
    prepareConfirmation.mockResolvedValue(confirmation());
    confirmPlan.mockReturnValue(confirming.promise);
    fetchResult.mockResolvedValue({
      plan_ref: "sf3plan_1234567890",
      state: "pending",
      batch_id: null,
      target_version: null,
      changed_count: null,
    });
    const controller = createSchoolFactSessionController(api, () => "click-id");
    await controller.preflightAdminFromClick(1, [admin_change()]);
    await controller.prepareConfirmationFromClick();

    const writing = controller.confirmFromClick();
    expect(await controller.refreshResult()).toBe(false);
    expect(fetchResult).not.toHaveBeenCalled();
    confirming.resolve(applied());
    expect(await writing).toBe(true);

    expect(await controller.refreshResult()).toBe(false);
    expect(controller.getState().result?.state).toBe("applied");
  });

  it("never prepares confirmation for a dirty plan", async () => {
    const { api, preflightAdmin, prepareConfirmation } = fake_api();
    preflightAdmin.mockResolvedValue(plan("sf3plan_1234567890", false));
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    await controller.preflightAdminFromClick(1, [admin_change()]);

    expect(await controller.prepareConfirmationFromClick()).toBe(false);
    expect(prepareConfirmation).not.toHaveBeenCalled();
  });

  it("surfaces only API-validated status coordinates and fixed version conflicts", async () => {
    const { api, preflightAdmin } = fake_api();
    preflightAdmin
      .mockRejectedValueOnce(new SchoolFactApiError(
        "状态映射需人工处理：staff_file / legacy_staff / 第 12 行 / employment_state；字典 school_statuses_v1。",
        "school_facts.status_mapping_required",
      ))
      .mockRejectedValueOnce(new SchoolFactApiError(
        "学校基础数据已变化或确认已失效，请重新预检。",
        "school_facts.change_rejected",
      ));
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");

    expect(await controller.preflightAdminFromClick(1, [admin_change()])).toBe(false);
    expect(controller.getState().error_message).toContain(
      "staff_file / legacy_staff / 第 12 行 / employment_state",
    );
    expect(await controller.preflightAdminFromClick(1, [admin_change()])).toBe(false);
    expect(controller.getState().error_message).toBe(
      "学校基础数据已变化或确认已失效，请重新预检。",
    );
  });

  it("uses result refresh as GET-only state and rejects a stale result after a new plan", async () => {
    const old_result = deferred<SchoolFactResult>();
    const { api, fetchResult, preflightAdmin, confirmPlan } = fake_api();
    preflightAdmin
      .mockResolvedValueOnce(plan("sf3plan_1234567890"))
      .mockResolvedValueOnce(plan("sf3plan_0987654321"));
    fetchResult.mockReturnValue(old_result.promise);
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    await controller.preflightAdminFromClick(1, [admin_change()]);

    const refreshing = controller.refreshResult();
    await controller.preflightAdminFromClick(1, [admin_change()]);
    old_result.resolve({
      plan_ref: "sf3plan_1234567890",
      state: "applied",
      batch_id: "00000000-0000-0000-0000-000000000123",
      target_version: 2,
      changed_count: 1,
    });

    expect(await refreshing).toBe(false);
    expect(fetchResult).toHaveBeenCalledTimes(1);
    expect(fetchResult).toHaveBeenCalledWith("sf3plan_1234567890");
    expect(confirmPlan).not.toHaveBeenCalled();
    expect(controller.getState().plan?.plan_ref).toBe("sf3plan_0987654321");
    expect(controller.getState().result).toBeNull();
  });

  it("ignores a late write result after disposal and clears the private credential", async () => {
    const preparing = deferred<SchoolFactConfirmation>();
    const { api, preflightAdmin, prepareConfirmation } = fake_api();
    preflightAdmin.mockResolvedValue(plan());
    prepareConfirmation.mockReturnValue(preparing.promise);
    const controller = createSchoolFactSessionController(api, () => "prepare-click-1");
    await controller.preflightAdminFromClick(1, [admin_change()]);
    const pending = controller.prepareConfirmationFromClick();

    controller.dispose();
    preparing.resolve(confirmation());

    expect(await pending).toBe(false);
    expect(controller.getState().plan).toBeNull();
    expect(controller.getState().confirmation_ready).toBe(false);
    expect(JSON.stringify(controller.getState())).not.toContain("sf3credential_PRIVATE");
  });

  it("does not start downstream POSTs after disposal interrupts an upload chain", async () => {
    const first_upload = deferred<SchoolFactSource>();
    const { api, uploadSource, discardSource, preflightExcel, preflightAdapted } = fake_api();
    uploadSource.mockReturnValue(first_upload.promise);
    const controller = createSchoolFactSessionController(api, () => "idempotency-1");
    const running = controller.preflightAdaptedFromClick([
      { source_slot: "school_file", file: new File(["school"], "school.xlsx") },
      { source_slot: "staff_file", file: new File(["staff"], "staff.xlsx") },
    ], "legacy_school_v3", 1);

    controller.dispose();
    first_upload.resolve(source());

    expect(await running).toBe(false);
    expect(uploadSource).toHaveBeenCalledTimes(1);
    expect(discardSource).toHaveBeenCalledWith("sf3source_1234567890");
    expect(preflightAdapted).not.toHaveBeenCalled();
    expect(preflightExcel).not.toHaveBeenCalled();
  });
});
