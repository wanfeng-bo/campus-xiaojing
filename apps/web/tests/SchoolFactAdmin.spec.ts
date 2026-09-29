import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import SchoolFactAdmin from "../src/features/school_facts/SchoolFactAdmin.vue";


const SHA = "a".repeat(64);
const PRIVATE_CREDENTIAL = "sf3credential_PRIVATE_1234567890";

const school_fact_sources = import.meta.glob<string>(
  "../src/features/school_facts/*.{ts,vue}",
  { eager: true, import: "default", query: "?raw" },
);
// F1 is an explicitly separate entry; its complete consumer graph keeps the same
// storage, logging, HTML and unrelated-business dependency protections.
const f1_sources = import.meta.glob<string>(
  ["../src/app/SchoolFactsApp.vue", "../src/school_facts_main.ts", "../src/features/assistant/*.{ts,vue}"],
  { eager: true, import: "default", query: "?raw" },
);
const production_entries = import.meta.glob<string>(
  [
    "../src/main.ts",
    "../src/app/DingTalkEntryApp.vue",
  ],
  { eager: true, import: "default", query: "?raw" },
);


interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}


afterEach(() => {
  document.body.replaceChildren();
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


function source_payload(ref = "sf3source_1234567890"): Record<string, unknown> {
  return {
    source_ref: ref,
    source_sha256: SHA,
    expires_at: "2026-08-21T12:00:00Z",
  };
}


function plan_payload(
  ref = "sf3plan_1234567890",
  dirty = false,
): Record<string, unknown> {
  return {
    plan_ref: ref,
    plan_sha256: SHA,
    is_clean: !dirty,
    issues: dirty ? [
      {
        source_slot: "formal_workbook",
        sheet_code: "02_人员",
        row_number: 2,
        column_code: "display_name",
        error_code: "fact.not_found",
      },
    ] : [],
    changes: [
      {
        operation: "upsert",
        fact_kind: "person",
        source_key: "person:001",
        expected_version: 0,
        sheet_code: "02_人员",
        row_number: 2,
        fields: [
          { field_code: "display_name", new_value: "同名人员甲" },
          { field_code: "priority", new_value: 1 },
        ],
      },
    ],
    status_decisions: [
      {
        source_slot: "formal_workbook",
        sheet_code: "02_人员",
        row_number: 2,
        field_code: "lifecycle_status",
        original_status_value: "在岗",
        standard_value: "active",
        mapping_version: "school_statuses_v1",
      },
    ],
  };
}


function mount_component(): { host: HTMLDivElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(SchoolFactAdmin);
  app.mount(host);
  return {
    host,
    unmount: () => {
      app.unmount();
      host.remove();
    },
  };
}


function set_value(host: HTMLElement, selector: string, value: string): void {
  const input = host.querySelector<HTMLInputElement | HTMLSelectElement>(selector);
  if (input === null) {
    throw new Error(`missing input ${selector}`);
  }
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}


function set_file(host: HTMLElement, selector: string, file: File): void {
  const input = host.querySelector<HTMLInputElement>(selector);
  if (input === null) {
    throw new Error(`missing file input ${selector}`);
  }
  Object.defineProperty(input, "files", {
    configurable: true,
    value: [file],
  });
  input.dispatchEvent(new Event("change", { bubbles: true }));
}


describe("SchoolFactAdmin", () => {
  it("keeps legacy admin outside default entries and protects every legacy and F1 consumer", () => {
    const school_features = Object.values(school_fact_sources).join("\n");
    const f1 = Object.values(f1_sources).join("\n");
    const combined = [school_features, f1].join("\n");
    const entry_text = Object.values(production_entries).join("\n");

    // Exact inventory covers original admin, current consumers and the F7 package entry.
    // Product files remain in school_features and therefore in every safety check.
    expect(Object.keys(school_fact_sources).map((path) => path.split("/").at(-1))
      .filter((name) => !name?.startsWith("product_")).sort())
      .toEqual([
        "AnswerText.vue",
        "DirectoryProposal.vue",
        "DirectoryResult.vue",
        "DirectoryWorkbench.vue",
        "FamilyCorrectionForm.vue",
        "FamilyCorrectionRecords.vue",
        "FamilyEducationResult.vue",
        "FamilyProfilePanel.vue",
        "FamilyProfileResult.vue",
        "ResidenceResult.vue",
        "SchoolFactAdmin.vue",
        "SchoolFactPackageImport.vue",
        "TeachingResult.vue",
        "directory_api.ts",
        "directory_contracts.ts",
        "directory_labels.ts",
        "directory_presentation.ts",
        "directory_session.ts",
        "family_contracts.ts",
        "family_corrections.ts",
        "family_education_contracts.ts",
        "package_api.ts",
        "package_contracts.ts",
        "package_session.ts",
        "school_fact_api.ts",
        "school_fact_contracts.ts",
        "school_fact_session.ts",
        "teaching_contracts.ts",
      ]);
    expect(combined).not.toMatch(
      /\b(?:localStorage|sessionStorage|indexedDB|cookieStore|caches|serviceWorker)\b/,
    );
    expect(combined).not.toMatch(/\b(?:document\.cookie|navigator\.storage|history\.)/);
    expect(combined).not.toMatch(/\bconsole\s*\./);
    expect(school_features).not.toMatch(
      /\bfrom\s+["'][^"']*(?:assistant|repairs|repair_|responsibility|models?)[^"']*["']/i,
    );
    expect(f1).not.toMatch(/\bfrom\s+["'][^"']*(?:repairs|repair_|responsibility|models?)[^"']*["']/i);
    expect(Object.keys(f1_sources)).toContain("../src/app/SchoolFactsApp.vue");
    expect(combined).not.toMatch(/\bv-html\b/);
    expect(entry_text).not.toMatch(/SchoolFactAdmin|features\/school_facts|school_fact_(?:api|session|contracts)/);
  });

  it("issues no request on mount, refresh or remount without a current in-memory plan", async () => {
    const fetch_mock = vi.fn();
    vi.stubGlobal("fetch", fetch_mock);

    const first = mount_component();
    await nextTick();
    first.host.querySelector<HTMLButtonElement>('[data-action="refresh-result"]')?.click();
    first.unmount();
    const second = mount_component();
    await nextTick();
    second.unmount();

    expect(fetch_mock).not.toHaveBeenCalled();
  });

  it("preflights an in-memory formal workbook, clears the File, and renders safe coordinates and diffs", async () => {
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(source_payload(), 201))
      .mockResolvedValueOnce(response(plan_payload("sf3plan_1234567890", true)))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      const file = new File([new Uint8Array([1, 2, 3])], "private-school.xlsx");
      set_file(host, '[data-role="formal-file"]', file);
      await nextTick();
      expect(host.textContent).toContain("private-school.xlsx");
      host.querySelector<HTMLButtonElement>('[data-action="preflight-excel"]')?.click();

      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(3));
      await vi.waitFor(() => expect(host.textContent).toContain("person:001"));
      expect(host.textContent).toContain("formal_workbook / 02_人员 / 第 2 行 / display_name");
      expect(host.textContent).toContain("display_name：同名人员甲");
      expect(host.textContent).toContain("lifecycle_status：在岗 → active");
      expect(host.textContent).toContain("在岗 → active");
      expect(host.textContent).toContain("school_statuses_v1");
      expect(host.textContent).not.toContain("private-school.xlsx");
      expect(host.querySelector('[data-action="prepare-confirmation"]')).toBeNull();
      expect(host.querySelector("textarea")).toBeNull();
    } finally {
      unmount();
    }
  });

  it("locks formal action after confirm is sent and recovers an unknown outcome with GET only", async () => {
    const preparing = deferred<Response>();
    const confirming = deferred<Response>();
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(source_payload(), 201))
      .mockResolvedValueOnce(response(plan_payload()))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockReturnValueOnce(preparing.promise)
      .mockReturnValueOnce(confirming.promise)
      .mockResolvedValueOnce(response({
        plan_ref: "sf3plan_1234567890",
        state: "applied",
        batch_id: "00000000-0000-0000-0000-000000000123",
        target_version: 2,
        changed_count: 1,
      }));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      set_file(host, '[data-role="formal-file"]', new File(["x"], "facts.xlsx"));
      await nextTick();
      host.querySelector<HTMLButtonElement>('[data-action="preflight-excel"]')?.click();
      await vi.waitFor(() => expect(host.querySelector('[data-action="prepare-confirmation"]')).not.toBeNull());

      const prepare = host.querySelector<HTMLButtonElement>('[data-action="prepare-confirmation"]');
      prepare?.click();
      prepare?.click();
      await nextTick();
      expect(fetch_mock).toHaveBeenCalledTimes(4);
      preparing.resolve(response({
        plan_ref: "sf3plan_1234567890",
        plan_sha256: SHA,
        credential: PRIVATE_CREDENTIAL,
        expires_at: "2026-08-21T12:00:00Z",
      }));

      await vi.waitFor(() => expect(host.querySelector('[data-action="confirm"]')).not.toBeNull());
      expect(host.textContent).not.toContain(PRIVATE_CREDENTIAL);
      const confirm = host.querySelector<HTMLButtonElement>('[data-action="confirm"]');
      confirm?.click();
      confirm?.click();
      await nextTick();
      expect(fetch_mock).toHaveBeenCalledTimes(5);
      const [confirm_url, confirm_init] = fetch_mock.mock.calls[4] as [string, RequestInit];
      expect(confirm_url).not.toContain(PRIVATE_CREDENTIAL);
      expect(JSON.parse(String(confirm_init.body))).toEqual({ credential: PRIVATE_CREDENTIAL });
      expect(host.textContent).not.toContain(PRIVATE_CREDENTIAL);
      confirming.resolve(response({
        code: "school_facts.unavailable",
        message: "学校基础数据通道暂时不可用。",
        request_id: "00000000-0000-0000-0000-000000000321",
      }, 503));

      await vi.waitFor(() => expect(host.textContent).toContain("不要重复提交"));
      expect(host.querySelector('[data-action="prepare-confirmation"]')).toBeNull();
      expect(host.querySelector('[data-action="confirm"]')).toBeNull();
      expect(host.querySelector('[data-action="refresh-result"]')).not.toBeNull();
      host.querySelector<HTMLButtonElement>('[data-action="refresh-result"]')?.click();
      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(6));
      expect(fetch_mock.mock.calls[5]).toEqual([
        "/api/v1/school-facts/plans/sf3plan_1234567890/result",
        { cache: "no-store", credentials: "same-origin", method: "GET" },
      ]);
      expect(fetch_mock.mock.calls.filter(([, init]) =>
        (init as RequestInit).method === "POST",
      )).toHaveLength(4);
      await vi.waitFor(() => expect(host.textContent).toContain("已应用 1 项变更"));
      expect(host.querySelector('[data-action="prepare-confirmation"]')).toBeNull();
      expect(host.querySelector('[data-action="confirm"]')).toBeNull();
      expect(host.querySelector('[data-action="refresh-result"]')).toBeNull();
      expect(host.querySelector('[name="confirmation"]')).toBeNull();
      expect(host.querySelector("textarea")).toBeNull();
    } finally {
      unmount();
    }
  });

  it("lets an administrator select a source mapping profile and sends adapted sources only after a click", async () => {
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(source_payload(), 201))
      .mockResolvedValueOnce(response(plan_payload()))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="adapted"]')?.click();
      await nextTick();
      set_value(host, '[data-role="mapping-version"]', "legacy_school_v3");
      set_value(host, '[data-role="adapted-slot-0"]', "school_file");
      set_file(
        host,
        '[data-role="adapted-file-0"]',
        new File(["legacy"], "legacy-private.xlsx"),
      );
      await nextTick();
      expect(host.textContent).toContain("legacy-private.xlsx");
      host.querySelector<HTMLButtonElement>('[data-action="preflight-adapted"]')?.click();

      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(3));
      const [url, init] = fetch_mock.mock.calls[1] as [string, RequestInit];
      expect(url).toBe("/api/v1/school-facts/plans/adapted");
      expect(JSON.parse(String(init.body))).toEqual({
        sources: [{
          source_slot: "school_file",
          source_ref: "sf3source_1234567890",
        }],
        mapping_version: "legacy_school_v3",
        base_version: 1,
      });
      await vi.waitFor(() => expect(host.textContent).not.toContain("legacy-private.xlsx"));
    } finally {
      unmount();
    }
  });

  it("caps one adapted batch at eight source files and states the 32 MiB total limit", async () => {
    const fetch_mock = vi.fn();
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="adapted"]')?.click();
      await nextTick();
      const add = host.querySelector<HTMLButtonElement>('[data-action="add-adapted-source"]');
      for (let index = 1; index < 8; index += 1) {
        add?.click();
        await nextTick();
      }

      expect(host.querySelectorAll('input[data-role^="adapted-slot-"]')).toHaveLength(8);
      expect(add?.disabled).toBe(true);
      expect(host.textContent).toContain("总计不超过 32 MiB");
      add?.click();
      await nextTick();
      expect(host.querySelectorAll('input[data-role^="adapted-slot-"]')).toHaveLength(8);
      expect(fetch_mock).not.toHaveBeenCalled();
    } finally {
      unmount();
    }
  });

  it("shows reactivate only for five roots and exposes the original 19 fact kinds and F6 building use", async () => {
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="admin"]')?.click();
      await nextTick();
      const fact_kind = host.querySelector<HTMLSelectElement>('[data-role="admin-fact-kind"]');
      expect(Array.from(fact_kind?.options ?? []).map((option) => option.value)).toEqual([
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
        "dormitory_building_profile",
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
      for (const option of Array.from(fact_kind?.options ?? [])) {
        set_value(host, '[data-role="admin-fact-kind"]', option.value);
        await nextTick();
        const operations = Array.from(
          host.querySelectorAll<HTMLOptionElement>('[data-role="admin-operation"] option'),
        ).map((item) => item.value);
        expect(operations).toEqual(
          roots.has(option.value)
            ? ["upsert", "end", "revoke", "reactivate"]
            : ["upsert", "end", "revoke"],
        );
      }
    } finally {
      unmount();
    }
  });

  it("renders only the v2 canonical fields for changed fact shapes", async () => {
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="admin"]')?.click();
      await nextTick();

      set_value(host, '[data-role="admin-fact-kind"]', "login_mapping");
      await nextTick();
      expect(host.querySelector('[data-field-code="display_name"]')).toBeNull();

      set_value(host, '[data-role="admin-fact-kind"]', "student_school_relationship");
      await nextTick();
      expect(host.querySelector('[data-field-code="primary_campus_source_key"]')).not.toBeNull();
      expect(host.querySelector('[data-field-code="administrative_campus_source_key"]')).not.toBeNull();

      set_value(host, '[data-role="admin-fact-kind"]', "organization_campus_relationship");
      await nextTick();
      expect(host.querySelector('[data-field-code="campus_source_key"]')).not.toBeNull();
      expect(host.querySelector('[data-field-code="valid_to"]')).not.toBeNull();

      set_value(host, '[data-role="admin-fact-kind"]', "policy_scope");
      await nextTick();
      expect(host.querySelector('[data-field-code="policy_source_key"]')).not.toBeNull();
      expect(host.querySelector('[data-field-code="valid_to"]')).not.toBeNull();

      set_value(host, '[data-role="admin-fact-kind"]', "student_bed_residence");
      await nextTick();
      expect(host.querySelector('[data-field-code="valid_to"]')).not.toBeNull();
      expect(host.querySelector('[data-field-code="arrived_from_source_key"]')).toBeNull();
      expect(host.querySelector('[data-field-code="departed_to_source_key"]')).toBeNull();
    } finally {
      unmount();
    }
  });

  it("sends a boarding move as one typed two-change transaction without free JSON", async () => {
    const fetch_mock = vi.fn().mockResolvedValue(response(plan_payload()));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="admin"]')?.click();
      await nextTick();
      const fact_kind = host.querySelector<HTMLSelectElement>('[data-role="admin-fact-kind"]');
      expect(fact_kind?.options).toHaveLength(20);
      expect(host.querySelector("textarea")).toBeNull();
      expect(host.querySelector('[data-role="admin-content-json"]')).toBeNull();

      set_value(host, '[data-role="admin-operation"]', "end");
      set_value(host, '[data-role="admin-fact-kind"]', "student_bed_residence");
      set_value(host, '[data-role="admin-source-key"]', "residence:student-001:old");
      set_value(host, '[data-role="admin-expected-version"]', "1");
      set_value(host, '[data-role="admin-effective-at"]', "2026-08-21T09:00:00+08:00");
      host.querySelector<HTMLButtonElement>('[data-action="add-admin-change"]')?.click();
      await nextTick();
      expect(fetch_mock).not.toHaveBeenCalled();

      set_value(host, '[data-role="admin-operation"]', "upsert");
      set_value(host, '[data-role="admin-fact-kind"]', "student_bed_residence");
      await nextTick();
      set_value(host, '[data-role="admin-source-key"]', "residence:student-001:new");
      set_value(host, '[data-role="admin-expected-version"]', "0");
      set_value(host, '[data-role="admin-effective-at"]', "2026-08-21T09:00:00+08:00");
      set_value(host, '[data-field-code="student_source_key"]', "student:001");
      set_value(host, '[data-field-code="bed_source_key"]', "bed:new-01");
      set_value(
        host,
        '[data-field-code="valid_to"]',
        "2026-12-31T18:00:00+08:00",
      );
      host.querySelector<HTMLButtonElement>('[data-action="add-admin-change"]')?.click();
      await nextTick();
      expect(fetch_mock).not.toHaveBeenCalled();
      expect(host.querySelectorAll('[data-role="admin-draft-item"]')).toHaveLength(2);
      host.querySelector<HTMLButtonElement>('[data-action="preflight-admin"]')?.click();

      await vi.waitFor(() => expect(fetch_mock).toHaveBeenCalledTimes(1));
      const [url, init] = fetch_mock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/api/v1/school-facts/plans/admin");
      const request_body = JSON.parse(String(init.body)) as {
        base_version: number;
        changes: Array<{
          content: null | Record<string, unknown>;
          effective_at: string;
          source_key: string;
        }>;
      };
      expect(request_body).toEqual({
        base_version: 1,
        changes: [
          {
            operation: "end",
            fact_kind: "student_bed_residence",
            schema_version: 4,
            source_key: "residence:student-001:old",
            expected_version: 1,
            effective_at: "2026-08-21T01:00:00.000Z",
            content: null,
          },
          {
            operation: "upsert",
            fact_kind: "student_bed_residence",
            schema_version: 4,
            source_key: "residence:student-001:new",
            expected_version: 0,
            effective_at: "2026-08-21T01:00:00.000Z",
            content: {
              student_source_key: "student:001",
              bed_source_key: "bed:new-01",
              valid_to: "2026-12-31T10:00:00.000Z",
            },
          },
        ],
      });
      expect(request_body.changes[1]?.effective_at).toBe(
        request_body.changes[0]?.effective_at,
      );
      expect(String(init.body)).not.toContain("arrived_from_source_key");
      expect(String(init.body)).not.toContain("departed_to_source_key");
      expect(String(init.body)).not.toContain("actor");
      expect(String(init.body)).not.toContain("school_id");
    } finally {
      unmount();
    }
  });

  it("requires an explicit time-zone offset for every management effective time", async () => {
    const fetch_mock = vi.fn();
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      host.querySelector<HTMLButtonElement>('[data-mode="admin"]')?.click();
      await nextTick();
      expect(host.textContent).toContain("时区偏移");
      set_value(host, '[data-role="admin-source-key"]', "school:main");
      set_value(host, '[data-role="admin-effective-at"]', "2026-08-21T09:00:00");
      set_value(host, '[data-field-code="business_code"]', "S001");
      set_value(host, '[data-field-code="display_name"]', "候选学校");
      set_value(host, '[data-field-code="timezone_name"]', "Asia/Shanghai");
      set_value(host, '[data-field-code="lifecycle_status"]', "active");
      host.querySelector<HTMLButtonElement>('[data-action="add-admin-change"]')?.click();
      await nextTick();

      expect(fetch_mock).not.toHaveBeenCalled();
      expect(host.querySelectorAll('[data-role="admin-draft-item"]')).toHaveLength(0);
      expect(host.textContent).toContain("有效的生效时间");
    } finally {
      unmount();
    }
  });

  it("bounds a legal 50,000-row plan to one inspectable page of DOM nodes", async () => {
    const issues = Array.from({ length: 50_000 }, (_, index) => ({
      source_slot: "formal_workbook",
      sheet_code: "02_人员",
      row_number: index + 1,
      column_code: "display_name",
      error_code: "fact.required",
    }));
    const large_plan = {
      ...plan_payload("sf3plan_1234567890", true),
      issues,
      changes: [],
      status_decisions: [],
    };
    const fetch_mock = vi.fn()
      .mockResolvedValueOnce(response(source_payload(), 201))
      .mockResolvedValueOnce(response(large_plan))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch_mock);
    const { host, unmount } = mount_component();
    try {
      set_file(host, '[data-role="formal-file"]', new File(["x"], "facts.xlsx"));
      await nextTick();
      host.querySelector<HTMLButtonElement>('[data-action="preflight-excel"]')?.click();

      await vi.waitFor(() => {
        expect(host.querySelectorAll('[data-list="issues"] > li')).toHaveLength(100);
      });
      expect(host.textContent).toContain("共 50000 条");
      host.querySelector<HTMLButtonElement>('[data-action="issues-next-page"]')?.click();
      await nextTick();
      expect(host.querySelector('[data-list="issues"]')?.textContent).toContain("第 101 行");
      expect(host.querySelectorAll('[data-list="issues"] > li')).toHaveLength(100);
    } finally {
      unmount();
    }
  });
});
