import { createApp, h, nextTick, type Component } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

const modules = import.meta.glob<unknown>(
  ["../src/features/school_facts/product_*.ts", "../src/app/SchoolFactsApp.vue"],
);
const cleanups: (() => void)[] = [];

/** 测试直接使用真实客户端、控制器和组件，只在 HTTP 边界提供合成响应。 */
async function load(name: string): Promise<any> {
  const loader = modules[name];
  expect(loader, `应实现 ${name}`).toBeTypeOf("function");
  return loader!();
}

const contact = {
  reference: "contact.office.1", campus_ref: "campus:1", campus_name: "主校区",
  contact_kind: "办公电话", contact_value: "010-00000000",
  service_hours: "工作日 8:00—17:00",
  valid_from: "2026-09-01T00:00:00+08:00", valid_to: null, version: 1,
  source: { label: "合成学校正式发布", published_at: "2026-09-01T00:00:00+08:00", reference: "source.1" },
};
const signedOut = {
  status: "signed_out", accounts: [{ ref: "account.maintainer", label: "事实维护员（工程账号）" }],
  contexts: [], selected_context_ref: null, can_maintain: false,
};
const signedIn = {
  ...signedOut, status: "signed_in", contexts: [
    { ref: "context.office", label: "办公室事实维护" },
    { ref: "context.reader", label: "普通教职工" },
  ], selected_context_ref: "context.office", can_maintain: true,
};
const campus = { reference: "campus:1", label: "主校区", path: "合成学校 / 主校区" };
const query = {
  display_notice: null,
  query: { subject: "official_contacts", scope: "selected", campus_name: "主校区", campus_ref: null },
  resolved_campuses: [campus],
  listing: { entity: "contacts", returned_count: 1, matched_count: 1, truncated: false },
  status: "known", items: [contact], candidates: [],
  coverage: { status: "complete", description: "已覆盖本校区官方办公联系渠道" },
  queried_at: "2026-09-05T12:00:00+08:00",
};
const proposal = {
  proposal_ref: "proposal.1",
  summary: { before: contact, after: { contact_value: "010-00000001", service_hours: null }, reason: "更正合成办公号码" },
  expires_at: "2099-01-01T00:00:00Z",
};
const succeeded = {
  reference: "proposal.1", status: "succeeded", message: "官方联系已更新",
  changed_count: 1, target_version: 5, request_id: "request.1",
};

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function api() {
  return {
    getSession: vi.fn().mockResolvedValue(signedIn),
    login: vi.fn().mockResolvedValue(signedIn),
    changeContext: vi.fn().mockResolvedValue({ ...signedIn, selected_context_ref: "context.reader", can_maintain: false }),
    logout: vi.fn().mockResolvedValue(signedOut),
    queryContacts: vi.fn().mockResolvedValue(query),
    prepareContact: vi.fn().mockResolvedValue(proposal),
    confirmContact: vi.fn().mockResolvedValue(succeeded),
    getReceipt: vi.fn().mockResolvedValue(succeeded),
  };
}

async function readyController() {
  const { createProductSessionController } = await load("../src/features/school_facts/product_session.ts");
  const transport = api();
  const controller = createProductSessionController(transport);
  cleanups.push(() => controller.dispose());
  await controller.open();
  await controller.queryContacts("主校区");
  controller.editContact(contact.reference);
  controller.setDraft({ contact_value: "010-00000001", service_hours: "", reason: "更正合成办公号码" });
  return { controller, transport };
}

function conversation(payload: unknown = null) {
  return {
    version: "1", conversation_ref: "conversation.1", generation: payload ? 1 : 0,
    active_run: null,
    turns: payload ? [{
      turn_ref: "turn.1", run_ref: "run.1", generation: 1,
      teacher_utterance: "主校区的官方办公联系方式是什么",
      visible_state: "completed", display_kind: "card", display_payload: payload, controls: [],
    }] : [],
  };
}

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("school fact product HTTP contract", () => {
  it("accepts the legacy feature response but requires boolean capability flags", async () => {
    const { isProductFeatures } = await load("../src/features/school_facts/product_contracts.ts");
    expect(isProductFeatures({ f6_features_enabled: true, f7_packages_enabled: false })).toBe(true);
    expect(isProductFeatures({ assistant_enabled: false })).toBe(true);
    expect(isProductFeatures({ assistant_enabled: true })).toBe(true);
    for (const value of [null, [], {assistant_enabled: "true"}, {assistant_enabled: 1}]) {
      expect(isProductFeatures(value)).toBe(false);
    }
  });
  it("validates status, selection and totals together while retaining valid partial results", async () => {
    const { isProductQuery, isProductContact } = await load("../src/features/school_facts/product_contracts.ts");
    const empty = { ...query, items: [], listing: { entity: "contacts", returned_count: 0, matched_count: 0, truncated: false } };
    const selecting = { ...empty, status: "selection_required", resolved_campuses: [], candidates: [campus],
      query: { ...query.query, campus_name: null }, listing: { entity: "campuses", returned_count: 1, matched_count: 1, truncated: false } };
    const denied = { ...empty, status: "denied", resolved_campuses: [], listing: { ...empty.listing, matched_count: null } };
    const unmatched = { ...denied, status: "unmatched" };
    const bad = [
      { ...empty, status: "no_current_records", listing: { ...empty.listing, matched_count: 9 } },
      { ...selecting, query: { ...selecting.query, campus_ref: campus.reference } },
      { ...selecting, status: "ambiguous" },
      { ...selecting, status: "ambiguous", query: query.query },
      { ...denied, listing: { ...denied.listing, matched_count: 0 } },
      { ...denied, listing: { ...denied.listing, truncated: true } },
      { ...unmatched, listing: { ...unmatched.listing, matched_count: 0 } },
      { ...query, listing: { ...query.listing, matched_count: 2 } },
      { ...query, items: [{ ...contact, campus_ref: "campus:missing" }] },
      { ...query, candidates: [campus] },
      { ...query, items: Array.from({ length: 101 }, (_, i) => ({ ...contact, reference: "contact." + i })),
        listing: { entity: "contacts", returned_count: 101, matched_count: 101, truncated: false } },
    ];
    for (const value of bad) expect(isProductQuery(value), JSON.stringify(value)).toBe(false);
    expect(isProductContact({ ...contact, campus_ref: undefined })).toBe(false);
    for (const value of [denied, unmatched, selecting,
      { ...empty, status: "no_current_records" },
      { ...empty, status: "no_current_records", query: { ...query.query, scope: "all_authorized", campus_name: null },
        listing: { ...empty.listing, matched_count: null, truncated: true } },
      { ...selecting, status: "ambiguous", query: query.query, listing: { ...selecting.listing, matched_count: 2, truncated: true } },
      { ...query, listing: { ...query.listing, truncated: true } },
      { ...query, listing: { ...query.listing, matched_count: 100, truncated: true } },
    ]) expect(isProductQuery(value), JSON.stringify(value)).toBe(true);
  });

  it("rejects incomplete or inconsistent query context instead of guessing target and counts", async () => {
    const { isProductQuery } = await load("../src/features/school_facts/product_contracts.ts");
    expect(isProductQuery(query)).toBe(true);
    expect(isProductQuery({ ...query, query: undefined })).toBe(false);
    expect(isProductQuery({ ...query, listing: { ...query.listing, returned_count: 3 } })).toBe(false);
    expect(isProductQuery({ ...query, status: "denied", items: [], listing: { ...query.listing, returned_count: 0 } })).toBe(false);
    expect(isProductQuery({ ...query, query: { ...query.query, scope: "all_authorized" } })).toBe(false);
    expect(isProductQuery({ ...query, query: { ...query.query, campus_ref: campus.reference } })).toBe(false);
  });

  it("sends an explicit candidate reference and decodes campus listings independently of contact items", async () => {
    const { createProductApi } = await load("../src/features/school_facts/product_api.ts");
    const listing = { ...query, items: [], query: { subject: "campuses", scope: "all_authorized", campus_name: null, campus_ref: null },
      listing: { entity: "campuses", returned_count: 1, matched_count: 1, truncated: false } };
    const fetcher = vi.fn().mockImplementation(() => response(listing));
    vi.stubGlobal("fetch", fetcher);
    const client = createProductApi();
    expect(await client.queryContacts({ subject: "campuses", scope: "all_authorized" })).toEqual(listing);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ subject: "campuses", scope: "all_authorized" });
    await client.queryContacts({ campus_ref: campus.reference, school_id: "forged" });
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ campus_ref: campus.reference });
  });

  it("sends only allowlisted inputs with same-origin cookie and uses original receipt reference", async () => {
    const { createProductApi } = await load("../src/features/school_facts/product_api.ts");
    const fetcher = vi.fn().mockResolvedValueOnce(response(signedIn))
      .mockResolvedValueOnce(response(query)).mockResolvedValueOnce(response(proposal))
      .mockResolvedValueOnce(response(succeeded)).mockResolvedValueOnce(response(succeeded));
    vi.stubGlobal("fetch", fetcher);
    const client = createProductApi();
    await client.login("account.maintainer");
    await client.queryContacts("主校区");
    await client.prepareContact({
      contact_ref: contact.reference, contact_value: "010-00000001",
      service_hours: null, reason: "更正合成办公号码", school_id: "forged", actor: "forged",
    });
    await client.confirmContact("proposal.1");
    await client.getReceipt("proposal.1");
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/school-fact-product/login", "/api/v1/school-fact-product/contacts/query",
      "/api/v1/school-fact-product/contacts/prepare",
      "/api/v1/school-fact-product/contacts/proposals/proposal.1/confirm",
      "/api/v1/school-fact-product/receipts/proposal.1",
    ]);
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({
      contact_ref: contact.reference, contact_value: "010-00000001",
      service_hours: null, reason: "更正合成办公号码",
    });
    expect(fetcher.mock.calls[3][1].body).toBeUndefined();
    expect(fetcher.mock.calls[4][1].method).toBe("GET");
    expect(fetcher.mock.calls.every(([, init]) => init.credentials === "same-origin")).toBe(true);
  });

  it("rejects malformed data and hides unknown server error text", async () => {
    const { createProductApi } = await load("../src/features/school_facts/product_api.ts");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response({ ...query, status: "denied" }))
      .mockResolvedValueOnce(response({ detail: "PRIVATE_SERVER_SECRET" }, 500)));
    const client = createProductApi();
    await expect(client.queryContacts("主校区")).rejects.toThrow("返回的信息无法核验");
    await expect(client.getSession()).rejects.toThrow("学校事实服务暂时不可用");
  });
});

describe("school fact product controller", () => {
  it.each([
    ["reference", "contact.other"], ["campus_ref", "campus:other"],
    ["campus_name", "另一校区"], ["contact_kind", "另一渠道"],
    ["contact_value", "010-99999999"], ["service_hours", "另一服务时间"], ["version", 2],
  ])("rejects a proposal whose original %s differs from the selected contact before confirmation", async (field, value) => {
    const { controller, transport } = await readyController();
    transport.prepareContact.mockResolvedValueOnce({ ...proposal,
      summary: { ...proposal.summary, before: { ...contact, [field as string]: value } } });
    expect(await controller.prepare()).toBe(false);
    expect(controller.getState().proposal).toBeNull();
    expect(controller.getState().selected_contact).toEqual(contact);
    expect(await controller.confirm()).toBe(false);
    expect(transport.confirmContact).not.toHaveBeenCalled();
  });

  it("lists only explicitly requested authorized campuses and rejects a forged candidate choice", async () => {
    const { controller, transport } = await readyController();
    expect(await controller.listCampuses()).toBe(true);
    expect(transport.queryContacts).toHaveBeenLastCalledWith({ subject: "campuses", scope: "all_authorized" });
    const count = transport.queryContacts.mock.calls.length;
    expect(await controller.selectCampus(query, "campus:forged")).toBe(false);
    expect(transport.queryContacts).toHaveBeenCalledTimes(count);
  });

  it("requires explicit preparation and one confirm; unknown outcome can only query the original receipt", async () => {
    const { controller, transport } = await readyController();
    expect(await controller.confirm()).toBe(false);
    await controller.prepare();
    expect(controller.getState().proposal.summary.before.contact_value).toBe("010-00000000");
    expect(transport.confirmContact).not.toHaveBeenCalled();
    const pending = deferred<unknown>();
    transport.confirmContact.mockReturnValueOnce(pending.promise);
    const first = controller.confirm();
    expect(await controller.confirm()).toBe(false);
    pending.reject(new Error("network connection lost"));
    await first;
    expect(controller.getState().receipt.status).toBe("unknown");
    expect(await controller.confirm()).toBe(false);
    expect(await controller.prepare()).toBe(false);
    await controller.checkReceipt();
    expect(transport.confirmContact).toHaveBeenCalledTimes(1);
    expect(transport.getReceipt).toHaveBeenCalledExactlyOnceWith("proposal.1");
    expect(controller.getState().receipt).toEqual(succeeded);
  });

  it("rechecks a completed receipt with only its original reference and keeps the definitive result when lookup fails", async () => {
    const { controller, transport } = await readyController();
    await controller.prepare();
    await controller.confirm();
    expect(controller.getState().receipt).toEqual(succeeded);
    expect(await controller.checkReceipt()).toBe(true);
    expect(transport.getReceipt).toHaveBeenCalledExactlyOnceWith("proposal.1");
    transport.getReceipt.mockRejectedValueOnce(new Error("read unavailable"));
    expect(await controller.checkReceipt()).toBe(false);
    expect(controller.getState().receipt).toEqual(succeeded);
    expect(controller.getState().error_message).toContain("原回执暂时无法重新核对");
    expect(transport.confirmContact).toHaveBeenCalledTimes(1);
    expect(transport.prepareContact).toHaveBeenCalledTimes(1);
    await controller.changeContext("context.reader");
    expect(await controller.checkReceipt()).toBe(false);
    expect(transport.getReceipt).toHaveBeenCalledTimes(2);
  });

  it("invalidates prepared changes when form values change and rejects expired proposals", async () => {
    const { controller, transport } = await readyController();
    await controller.prepare();
    controller.setDraft({ contact_value: "010-00000002" });
    expect(controller.getState().proposal).toBeNull();
    expect(await controller.confirm()).toBe(false);
    transport.prepareContact.mockResolvedValueOnce({ ...proposal, expires_at: "2000-01-01T00:00:00Z" });
    await controller.prepare();
    expect(await controller.confirm()).toBe(false);
    expect(transport.confirmContact).not.toHaveBeenCalled();
  });

  it("ignores old context query and prepare responses and prevents untrusted references", async () => {
    const { controller, transport } = await readyController();
    expect(controller.editContact("forged.contact")).toBe(false);
    const pending = deferred<unknown>();
    transport.prepareContact.mockReturnValueOnce(pending.promise);
    const old = controller.prepare();
    const changing = controller.changeContext("context.reader");
    expect(controller.getState().session).toBeNull();
    expect(controller.getState().result).toBeNull();
    await changing;
    pending.resolve(proposal);
    await old;
    expect(controller.getState().proposal).toBeNull();
    expect(controller.getState().session.can_maintain).toBe(false);
    expect(await controller.prepare()).toBe(false);
    expect(await controller.changeContext("forged.context")).toBe(false);
  });

  it("drops all late responses after dispose", async () => {
    const { controller, transport } = await readyController();
    const pending = deferred<unknown>();
    transport.queryContacts.mockReturnValueOnce(pending.promise);
    const old = controller.queryContacts("城南校区");
    const listener = vi.fn();
    controller.subscribe(listener);
    controller.dispose();
    const count = listener.mock.calls.length;
    pending.resolve(query);
    await old;
    expect(listener).toHaveBeenCalledTimes(count);
    expect(await controller.confirm()).toBe(false);
  });

  it("does not call maintenance endpoints for a read-only identity", async () => {
    const { controller, transport } = await readyController();
    await controller.changeContext("context.reader");
    await controller.queryContacts("主校区");
    expect(controller.editContact(contact.reference)).toBe(false);
    expect(await controller.prepare()).toBe(false);
    expect(transport.prepareContact).not.toHaveBeenCalled();
  });
});

describe("SchoolFactsApp", () => {
  it("keeps direct query, maintenance confirmation and original receipts usable with the formal assistant disabled", async () => {
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith("/features")) return response({assistant_enabled: false});
      if (url.endsWith("/session")) return response(signedIn);
      if (url.endsWith("/contacts/query")) return response(query);
      if (url.endsWith("/contacts/prepare")) return response(proposal);
      if (url.endsWith("/confirm") || url.endsWith("/receipts/proposal.1")) return response(succeeded);
      throw new Error("unexpected request");
    });
    vi.stubGlobal("fetch", fetcher);
    const module = await load("../src/app/SchoolFactsApp.vue");
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp(module.default as Component, {formal: true}); app.mount(host); cleanups.push(() => app.unmount());
    await vi.waitFor(() => expect(host.querySelector('[data-action="query-contacts"]')).not.toBeNull());
    expect(host.querySelector('[data-role="assistant-input"]')).toBeNull();
    input(host, '[data-role="campus-name"]', "主校区");
    await nextTick(); click(host, "query-contacts");
    await vi.waitFor(() => expect(host.querySelector('[data-action="edit-contact"]')).not.toBeNull());
    click(host, "edit-contact"); await nextTick();
    input(host, '[data-role="contact-value"]', "010-00000001");
    input(host, '[data-role="service-hours"]', "");
    input(host, '[data-role="change-reason"]', "更正合成办公号码");
    await nextTick(); click(host, "prepare-contact");
    await vi.waitFor(() => expect(host.textContent).toContain("请核对本次修改"));
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(0);
    click(host, "confirm-contact");
    await vi.waitFor(() => expect(host.querySelector('[data-role="official-receipt"]')?.textContent).toContain("官方联系已更新"));
    click(host, "check-receipt");
    await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/receipts/proposal.1"))).toHaveLength(1));
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(1);
    expect(fetcher.mock.calls.some(([url]) => url.includes("/assistant/"))).toBe(false);
  });
  it("continues the chat after a workbench confirmation and preserves the official receipt when the answer fails", async () => {
    const directoryProposal = { proposal_ref: "proposal.directory.1", expires_at: "2099-01-01T00:00:00Z", summary: {
      label: "合成教师", reference: "person.1", fact_kind: "person", operation: "upsert", before: { display_name: "旧名称" }, after: { display_name: "新名称" }, effective_at: "2026-09-15T00:00:00Z", reason: "核实", source_label: "正式维护",
    } };
    const descriptor = { fact_kind: "person", label: "人员", fields: [], creation_choices: [] };
    const original = conversation({ text: "请核对本次修改", facts: [{ kind: "fact_proposal", ...directoryProposal }], continuations: [{ handle: "continuation.1", proposal_ref: directoryProposal.proposal_ref }] });
    const failed = { ...original, generation: 2, turns: [...original.turns, { ...original.turns[0]!, turn_ref: "turn.2", run_ref: "run.2", generation: 2, continuation_of: "turn.1", display_payload: { text: "后续回答未完成", error_code: "assistant.budget" } }] };
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith("/session")) return response({ ...signedIn, directory_enabled: true });
      if (url.endsWith("/conversations")) return response(original);
      if (url.endsWith("/catalog")) return response({ families: [{ value: "person", label: "人员" }] });
      if (url.endsWith("/describe/person")) return response(descriptor);
      if (url.endsWith("/proposals/" + directoryProposal.proposal_ref)) return response(directoryProposal);
      if (url.endsWith("/confirm")) return response({ ...succeeded, reference: directoryProposal.proposal_ref, message: "正式名称已修改" });
      if (url.endsWith("/continuations/continuation.1")) return response(failed);
      throw new Error("unexpected request");
    });
    vi.stubGlobal("fetch", fetcher);
    const module = await load("../src/app/SchoolFactsApp.vue");
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
    await vi.waitFor(() => expect(host.querySelector('[data-action="review-directory-proposal"]')).not.toBeNull());
    click(host, "review-directory-proposal");
    await vi.waitFor(() => expect(host.querySelector('[data-action="confirm-directory"]')).not.toBeNull());
    const ack = host.querySelector<HTMLInputElement>('.directory-workbench input[type="checkbox"]')!;
    ack.checked = true; ack.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
    const draft = host.querySelector<HTMLTextAreaElement>('[data-role="assistant-input"]')!;
    draft.value = "下一条草稿"; draft.dispatchEvent(new Event("input", { bubbles: true }));
    click(host, "confirm-directory");
    await vi.waitFor(() => expect(host.textContent).toContain("继续处理原任务"));
    expect(host.querySelector('[aria-label="事实正式操作回执"]')?.textContent).toContain("正式名称已修改");
    expect(host.textContent).toContain("正式操作结果仍以原回执为准");
    expect(host.querySelectorAll(".user-message")).toHaveLength(1);
    expect(draft.value).toBe("下一条草稿");
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url.includes("/continuations/"))).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/turns"))).toHaveLength(0);
  });

  it("does not restore a delayed child-switch claim after identity invalidation", async () => {
    const transport = api();
    const { createProductSessionController } = await load("../src/features/school_facts/product_session.ts");
    const controller = createProductSessionController(transport);
    cleanups.push(() => controller.dispose());
    await controller.open();
    const pending = deferred<unknown>();
    transport.changeContext.mockReturnValueOnce(pending.promise);
    const switching = controller.changeContext("context.reader");
    controller.invalidateIdentity();
    pending.resolve({
      ...signedIn, selected_context_ref: "context.reader",
      selection_transition_ref: "C".repeat(43),
    });
    expect(await switching).toBe(false);
    expect(controller.getState().authentication_required).toBe(true);
    expect(controller.getState().session?.selection_transition_ref).toBeUndefined();
  });
  it("keeps a same-name contact reference visible and unchanged from its owning campus through confirmation", async () => {
    const otherCampus = { ...campus, reference: "campus:2", path: "合成学校 / 南院 / 主校区" };
    const otherContact = { ...contact, reference: "contact.office.2", campus_ref: otherCampus.reference };
    const selectedQuery = { ...query, query: { ...query.query, campus_name: null, campus_ref: otherCampus.reference },
      resolved_campuses: [otherCampus], items: [otherContact] };
    const campusQuery = { ...query, query: { subject: "campuses", scope: "all_authorized", campus_name: null, campus_ref: null },
      items: [], resolved_campuses: [campus, otherCampus], listing: { entity: "campuses", returned_count: 2, matched_count: 2, truncated: false } };
    const otherProposal = { ...proposal, proposal_ref: "proposal.2", summary: { ...proposal.summary, before: otherContact } };
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/session")) return response(signedIn);
      if (url.endsWith("/conversations")) return response(conversation());
      if (url.endsWith("/contacts/query")) return response(JSON.parse(String(init?.body)).subject === "campuses" ? campusQuery : selectedQuery);
      if (url.endsWith("/contacts/prepare")) return response(otherProposal);
      if (url.endsWith("/confirm")) return response({ ...succeeded, reference: otherProposal.proposal_ref });
      throw new Error("unexpected request");
    });
    vi.stubGlobal("fetch", fetcher);
    const module = await load("../src/app/SchoolFactsApp.vue");
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
    await vi.waitFor(() => expect(host.querySelector('[data-action="list-campuses"]')).not.toBeNull());
    click(host, "list-campuses");
    await vi.waitFor(() => expect(host.querySelectorAll('[data-action="select-campus"]')).toHaveLength(2));
    host.querySelectorAll<HTMLButtonElement>('[data-action="select-campus"]')[1]!.click();
    await vi.waitFor(() => expect(host.querySelector('[data-action="edit-contact"]')).not.toBeNull());
    const card = host.querySelector('.facts-panel .contact-card')!;
    expect(card.textContent).toContain(otherCampus.path);
    expect(card.textContent).toContain("联系引用：" + otherContact.reference);
    click(host, "edit-contact"); await nextTick();
    expect(host.querySelector('.maintenance-form')!.textContent).toContain("联系引用：" + otherContact.reference);
    input(host, '[data-role="contact-value"]', proposal.summary.after.contact_value);
    input(host, '[data-role="service-hours"]', "");
    input(host, '[data-role="change-reason"]', proposal.summary.reason);
    await nextTick(); click(host, "prepare-contact");
    await vi.waitFor(() => expect(host.querySelector('.confirmation-card')).not.toBeNull());
    const prepareCalls = fetcher.mock.calls.filter(([url]) => url.endsWith("/contacts/prepare"));
    expect(prepareCalls).toHaveLength(1);
    expect(JSON.parse(String(prepareCalls[0]![1]?.body)).contact_ref).toBe(otherContact.reference);
    expect(host.querySelector('.confirmation-card')!.textContent).toContain("联系引用：" + otherContact.reference);
    expect(host.querySelector('.confirmation-card')!.textContent).not.toContain(contact.reference);
    input(host, '[data-role="campus-name"]', "尚未查询的另一校区"); await nextTick();
    expect(host.querySelector('.confirmation-card')!.textContent).toContain("联系引用：" + otherContact.reference);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(0);
    expect(otherContact).toEqual({ ...contact, reference: "contact.office.2", campus_ref: otherCampus.reference });
    click(host, "confirm-contact");
    await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(1));
    expect(fetcher.mock.calls.find(([url]) => url.endsWith("/confirm"))![0]).toBe("/api/v1/school-fact-product/contacts/proposals/proposal.2/confirm");
  });

  it("shows a standalone assistant contact's own public reference without substituting another query owner", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const other = { ...contact, reference: "contact.other", campus_ref: "campus:other" };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [query, other] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    const standalone = host.querySelector('.fact-card')!;
    expect(standalone.textContent).toContain("联系引用：" + other.reference);
    expect(standalone.textContent).not.toContain(contact.reference);
    expect(standalone.textContent).not.toContain(campus.path);
  });

  it("collapses legacy read evidence, localizes contact kinds and keeps missing or failed reads visible", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const phoneQuery = { ...query, items: query.items.map(item => ({ ...item, contact_kind: "phone" })) };
    const missing = { ...query, status: "not_recorded", items: [],
      listing: { entity: "contacts", returned_count: 0, matched_count: 0, truncated: false } };
    const failed = { ...query, status: "source_unavailable", items: [], resolved_campuses: [],
      listing: { entity: "contacts", returned_count: 0, matched_count: null, truncated: false } };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: {
      text: "办公室电话已列出。", facts: [phoneQuery, { ...contact, contact_kind: "email" }, missing, failed],
    } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    await nextTick();
    expect(host.querySelectorAll("details")).toHaveLength(1);
    expect((host.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    expect(host.textContent).toContain("所查校区尚未录入官方联系");
    expect(host.textContent).toContain("正式资料来源暂时不可用");
    expect(host.textContent).toContain("电话");
    expect(host.textContent).toContain("电子邮箱");
    expect(host.textContent).not.toContain("phone");
    expect(host.textContent).not.toContain("email");
    expect(host.querySelectorAll(".contact-result")).toHaveLength(3);
  });

  it("does not describe an empty campus listing as missing contact records", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const result = { ...query, status: "no_current_records", items: [], resolved_campuses: [],
      query: { subject: "campuses", scope: "all_authorized", campus_name: null, campus_ref: null },
      listing: { entity: "campuses", returned_count: 0, matched_count: 0, truncated: false } };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [result] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    expect(host.textContent).toContain("当前查询范围内没有可提供的正式校区记录");
    expect(host.textContent).not.toContain("没有可提供的正式联系记录");
    expect(host.textContent).not.toContain("该联系渠道不存在");
  });

  it("shows a display limit notice without turning unavailable presentation into empty facts", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const result = { ...query, status: "display_limited", items: [], resolved_campuses: [],
      display_notice: "资料过长，本条结果暂时无法完整展示，请缩小查询范围。",
      listing: { entity: "contacts", returned_count: 0, matched_count: 9, truncated: true } };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [result] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    expect(host.textContent).toContain(result.display_notice);
    expect(host.textContent).toContain("校区名称：主校区");
    expect(host.textContent).not.toContain("本次显示 0");
    expect(host.textContent).not.toContain("尚未录入");
    expect(host.textContent).not.toContain("没有可提供");
  });

  it("renders each contact under its referenced campus path when campus names are the same", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const other = { reference: "campus:2", label: campus.label, path: "合成学校 / 南院 / 主校区" };
    const result = { ...query, query: { ...query.query, scope: "all_authorized", campus_name: null },
      resolved_campuses: [campus, other], items: [contact, { ...contact, reference: "contact.2", campus_ref: other.reference }],
      listing: { entity: "contacts", returned_count: 2, matched_count: 2, truncated: false } };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [result] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    const headings = [...host.querySelectorAll(".contact-card h3")].map(node => node.textContent);
    expect(headings).toEqual([campus.path + " · 办公电话", other.path + " · 办公电话"]);
    const cards = [...host.querySelectorAll(".contact-card")];
    expect(cards[0]!.textContent).toContain("联系引用：" + contact.reference);
    expect(cards[1]!.textContent).toContain("联系引用：contact.2");
  });

  it.each([
    { status: "unmatched", resolved: [], expected: "未匹配到校区" },
    { status: "no_current_records", resolved: [campus], expected: "当前查询范围内没有可提供的正式联系记录" },
    { status: "not_recorded", resolved: [campus], expected: "所查校区尚未录入官方联系" },
    { status: "source_unavailable", resolved: [], expected: "暂时无法取得正式来源" },
  ])("keeps $status distinct and retains the actual request target", async ({ status, resolved, expected }) => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const result = { ...query, status, items: [], resolved_campuses: resolved,
      listing: { entity: "contacts", returned_count: 0, matched_count: ["source_unavailable", "unmatched"].includes(status) ? null : 0, truncated: false } };
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [result] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    expect(host.textContent).toContain(expected);
    expect(host.textContent).toContain("校区名称：主校区");
    if (status === "not_recorded") expect(host.textContent).toContain(campus.path);
    if (status === "source_unavailable") expect(host.textContent).not.toContain("本次显示");
  });

  it("shows query target, current result count, truncation and source coverage separately in assistant cards", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const result = { ...query, coverage: { status: "unknown", description: "正式来源未声明完整覆盖" },
      listing: { entity: "contacts", returned_count: 1, matched_count: 101, truncated: true } };
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "请核对正式结果。", facts: [result] } }) });
    app.mount(host); cleanups.push(() => app.unmount());
    expect(host.textContent).toContain("查询对象");
    expect(host.textContent).toContain(campus.path);
    expect(host.textContent).toContain("本次显示 1 条，共匹配 101 条");
    expect(host.textContent).toContain("结果未全部显示");
    expect(host.textContent).toContain("来源覆盖未声明");
  });

  it("requires an explicit campus choice even when one candidate is available", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const result = { ...query, status: "selection_required", items: [], candidates: [campus], resolved_campuses: [],
      query: { ...query.query, campus_name: null }, listing: { entity: "campuses", returned_count: 1, matched_count: 1, truncated: false } };
    const selected = vi.fn();
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp({ render: () => h(module.default, { payload: { text: "", facts: [result] }, selectable: true, onSelectCampus: selected }) });
    app.mount(host); cleanups.push(() => app.unmount());
    expect(host.textContent).toContain("请选择查询校区");
    expect(host.textContent).toContain("未指定校区");
    expect(selected).not.toHaveBeenCalled();
    host.querySelector<HTMLButtonElement>('[data-action="select-campus"]')!.click();
    expect(selected).toHaveBeenCalledExactlyOnceWith(result, campus.reference);
  });

  it("selects same-name campuses by their returned references from the sidebar", async () => {
    const choices = [{ reference: "campus:1", label: "同名校区", path: "学校 / 北侧" },
      { reference: "campus:2", label: "同名校区", path: "学校 / 南侧" }];
    const ambiguous = { ...query, status: "ambiguous", items: [], candidates: choices, resolved_campuses: [],
      query: { ...query.query, campus_name: "同名校区" }, listing: { entity: "campuses", returned_count: 2, matched_count: 2, truncated: false } };
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith("/session")) return response(signedIn);
      if (url.endsWith("/conversations")) return response(conversation());
      if (url.endsWith("/contacts/query")) return response(JSON.parse(String(init.body)).campus_ref ? query : ambiguous);
      throw new Error("unexpected request");
    });
    vi.stubGlobal("fetch", fetcher);
    const module = await load("../src/app/SchoolFactsApp.vue");
    const host = document.createElement("div"); document.body.append(host);
    const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
    await vi.waitFor(() => expect(host.querySelector('[data-role="campus-name"]')).not.toBeNull());
    input(host, '[data-role="campus-name"]', "同名校区");
    host.querySelector('form[data-role="contact-query"]')!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(host.querySelectorAll('[data-action="select-campus"]')).toHaveLength(2));
    expect(host.textContent).toContain("学校 / 南侧");
    host.querySelectorAll<HTMLButtonElement>('[data-action="select-campus"]')[1]!.click();
    await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/contacts/query"))).toHaveLength(2));
    const calls = fetcher.mock.calls.filter(([url]) => url.endsWith("/contacts/query"));
    expect(JSON.parse(String(calls[1]![1].body))).toEqual({ subject: "official_contacts", scope: "selected", campus_ref: "campus:2" });
  });

  it.each(["succeeded", "unknown"] as const)("walks through synthetic login, a readable answer, %s confirmation, and a read-only receipt lookup", async (initialOutcome) => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    let published = contact;
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/session")) return response(signedOut);
      if (url.endsWith("/login")) return response(signedIn);
      if (url.endsWith("/contacts/query")) return response({ ...query, items: [published] });
      if (url.endsWith("/contacts/prepare")) return response(proposal);
      if (url.endsWith("/confirm")) {
        published = { ...contact, contact_value: "010-00000001", version: 2 };
        return response(initialOutcome === "succeeded" ? succeeded : {
          ...succeeded, status: "unknown", message: "结果尚待核对", changed_count: null, target_version: null,
        });
      }
      if (url.endsWith("/receipts/proposal.1")) return response({ ...succeeded, request_id: null });
      if (url.endsWith("/conversations")) return response(conversation());
      if (url.endsWith("/turns")) return response(conversation({
        kind: "assistant_answer", text: "主校区官方办公电话如下。", facts: [query],
      }));
      throw new Error(`unexpected request ${init?.method} ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    const module = await load("../src/app/SchoolFactsApp.vue");
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(module.default as Component);
    app.mount(host);
    cleanups.push(() => app.unmount());
    await vi.waitFor(() => expect(host.textContent).toContain("合成学校工程身份"));
    await vi.waitFor(() => expect(host.querySelector('[data-action="login"]')).not.toBeNull());
    click(host, "login");
    await vi.waitFor(() => expect(host.querySelector('[data-role="assistant-input"]')).not.toBeNull());
    const campusInput = host.querySelector<HTMLInputElement>('[data-role="campus-name"]')!;
    expect(campusInput.value).toBe("");
    expect(campusInput.placeholder).toBe("工程示例：合成工程校区");
    expect(host.querySelector<HTMLButtonElement>('[data-action="query-contacts"]')!.disabled).toBe(true);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/contacts/query"))).toHaveLength(0);
    input(host, '[data-role="assistant-input"]', "主校区的官方办公联系方式是什么");
    await nextTick();
    click(host, "send");
    await vi.waitFor(() => expect(host.textContent).toContain("主校区官方办公电话如下。"));
    expect(host.querySelector(".assistant-conversation pre")).toBeNull();
    expect(host.querySelector(".assistant-conversation")?.textContent).toContain("合成学校正式发布");
    input(host, '[data-role="campus-name"]', "主校区");
    host.querySelector("form[data-role='contact-query']")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(host.querySelector('[data-action="edit-contact"]')).not.toBeNull());
    click(host, "edit-contact");
    await nextTick();
    input(host, '[data-role="contact-value"]', "010-00000001");
    input(host, '[data-role="service-hours"]', "");
    input(host, '[data-role="change-reason"]', "更正合成办公号码");
    await nextTick();
    click(host, "prepare-contact");
    await vi.waitFor(() => expect(host.textContent).toContain("请核对本次修改"));
    expect(host.textContent).toContain("010-00000000");
    expect(host.textContent).toContain("010-00000001");
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(0);
    click(host, "confirm-contact");
    const receiptText = () => host.querySelector('[data-role="official-receipt"]')?.textContent;
    await vi.waitFor(() => expect(receiptText()).toContain(initialOutcome === "succeeded" ? "官方联系已更新" : "结果尚待核对"));
    expect(receiptText()).toContain("请求编号：request.1");
    expect(receiptText()).toContain("回执引用：proposal.1");
    if (initialOutcome === "succeeded") expect(receiptText()).toContain("学校事实总版本 5");
    expect(receiptText()).not.toContain("记录版本 5");
    click(host, "check-receipt");
    await vi.waitFor(() => expect(receiptText()).toContain("官方联系已更新"));
    expect(receiptText()).toContain("学校事实总版本 5");
    expect(receiptText()).toContain("回执引用：proposal.1");
    await vi.waitFor(() => expect(receiptText()).not.toContain("请求编号："));
    const reads = fetcher.mock.calls.filter(([url]) => url.endsWith("/receipts/proposal.1"));
    expect(reads).toHaveLength(1);
    expect(reads[0]![1]?.method).toBe("GET");
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/confirm"))).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/contacts/prepare"))).toHaveLength(1);
    expect(host.textContent).not.toContain("已通过真实认证");
    click(host, "query-contacts");
    await vi.waitFor(() => expect(host.querySelector('[data-role="contact-results"]')?.textContent).toContain("版本 2"));
    expect(storage).not.toHaveBeenCalled();
  });

  it.each([
    { name: "待确认修改", fact: proposal, expected: ["请核对本次修改", "010-00000000", "010-00000001", "更正合成办公号码", proposal.expires_at, "联系引用：" + contact.reference] },
    { name: "正式操作回执", fact: succeeded, expected: ["已完成", "官方联系已更新", "学校事实总版本 5", "请求编号：request.1", "回执引用：proposal.1"] },
  ])("shows $name inside assistant facts without adding an execution button", async ({ fact, expected }) => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp({ render: () => h(module.default, {
      payload: { kind: "assistant_answer", text: "请查看正式结果。", facts: [fact] },
    }) });
    app.mount(host);
    cleanups.push(() => app.unmount());
    for (const value of expected) expect(host.textContent).toContain(value);
    expect(host.textContent).not.toContain("这条资料暂时无法展示");
    expect(host.querySelector("button")).toBeNull();
    if ("proposal_ref" in fact) expect(host.textContent).not.toContain("proposal.1");
  });

  it("shows the official source channel without confusing a publication with an execution receipt", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const module = await componentModules["../src/features/school_facts/product_answer.vue"]!() as { default: Component };
    const host = document.createElement("div");
    document.body.append(host);
    const withSource = { ...query, items: query.items.map(item => ({ ...item, source: {
      ...item.source, kind: "admin", reference: "publication:source-batch",
    } })) };
    const app = createApp({ render: () => h(module.default, {
      payload: { kind: "assistant_answer", text: "", facts: [withSource] },
    }) });
    app.mount(host);
    cleanups.push(() => app.unmount());
    expect(host.textContent).toContain("来源通道");
    expect(host.textContent).toContain("正式维护");
    expect(host.textContent).not.toContain("执行回执");
    expect(host.querySelector("button")).toBeNull();
  });

  it("renders unfamiliar answer data safely without leaking raw JSON or executing HTML", async () => {
    const componentModules = import.meta.glob("../src/features/school_facts/product_answer.vue");
    const loader = componentModules["../src/features/school_facts/product_answer.vue"];
    expect(loader).toBeTypeOf("function");
    const module = await loader!() as { default: Component };
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp({ render: () => h(module.default, {
      payload: { kind: "assistant_answer", text: '<img src=x onerror="alert(1)">', facts: [
        { ...query, status: "denied", items: [], candidates: [], resolved_campuses: [], listing: { entity: "contacts", returned_count: 0, matched_count: null, truncated: false }, coverage: { status: "unknown", description: "当前身份无权查看" } },
        { unsupported: "PRIVATE_UNKNOWN_PAYLOAD" },
      ] },
    }) });
    app.mount(host);
    cleanups.push(() => app.unmount());
    expect(host.querySelector("img")).toBeNull();
    expect(host.textContent).toContain("当前身份无权查看");
    expect(host.textContent).not.toContain("PRIVATE_UNKNOWN_PAYLOAD");
  });
});

function click(host: HTMLElement, action: string) {
  const button = host.querySelector<HTMLButtonElement>(`[data-action="${action}"]`);
  expect(button, action).not.toBeNull();
  button!.click();
}

function input(host: HTMLElement, selector: string, value: string) {
  const element = host.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
  expect(element, selector).not.toBeNull();
  element.value = value;
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

const parentSession = {
  ...signedIn, can_maintain: false, family_enabled: true,
  family_conversation_available: true, assistant_context_key: "family-scope.1",
  contexts: [
    {ref:"child.a",label:"合成孩子甲",kind:"guardian_relationship"},
    {ref:"child.b",label:"合成孩子乙",kind:"guardian_relationship"},
    {ref:"staff.1",label:"普通教师",kind:"staff_assignment"},
  ], selected_context_ref:"child.a", selected_context_kind:"guardian_relationship",
};

it("validates public conversation scope and typed focus choices without treating them as credentials", async () => {
  const {isProductSession} = await load("../src/features/school_facts/product_contracts.ts");
  expect(isProductSession(parentSession)).toBe(true);
  expect(isProductSession({...parentSession,accounts:[{ref:"account.parent",label:"家长",kind:null}]})).toBe(true);
  expect(isProductSession({...parentSession,selected_context_ref:null,selected_context_kind:null})).toBe(true);
  for(const change of [
    {family_conversation_available:"yes"}, {assistant_context_key: {}}, {assistant_context_key:""},
    {assistant_context_key:"scope\nforged"}, {assistant_context_key:undefined},
    {contexts:[{ref:"child.a",label:"合成孩子甲",kind:"administrator"}]},
    {status:"signed_out",selected_context_ref:null},
  ]) expect(isProductSession({...parentSession,...change})).toBe(false);
  expect(isProductSession(signedIn)).toBe(true);
});

it.each(["success","failure","expired"])("retains only known parent conversation scope during card focus switching (%s)", async outcome => {
  const {createProductSessionController} = await load("../src/features/school_facts/product_session.ts");
  const transport=api(); transport.getSession.mockResolvedValue(parentSession);
  const controller=createProductSessionController(transport); cleanups.push(()=>controller.dispose());
  await controller.open(); await controller.queryContacts("主校区");
  const before=controller.getState(); const pending=deferred<any>(); transport.changeContext.mockReturnValueOnce(pending.promise);
  const switching=controller.changeContext("child.b");
  expect(controller.getState()).toMatchObject({session:parentSession,busy:"session",result:null});
  expect(controller.getState().context_epoch).toBeGreaterThan(before.context_epoch);
  if(outcome==="expired")controller.invalidateIdentity();
  if(outcome==="failure")pending.reject(new Error("offline"));
  else pending.resolve({...parentSession,selected_context_ref:"child.b"});
  await switching;
  if(outcome==="success")expect(controller.getState().session?.selected_context_ref).toBe("child.b");
  if(outcome==="failure")expect(controller.getState().session).toBeNull();
  if(outcome==="expired"){
    expect(controller.getState().authentication_required).toBe(true);
    expect(controller.getState().session?.selected_context_ref).not.toBe("child.b");
  }
});

it("does not preserve the parent conversation for a staff role change",async()=>{
  const {createProductSessionController}=await load("../src/features/school_facts/product_session.ts");
  const transport=api();transport.getSession.mockResolvedValue(parentSession);
  const controller=createProductSessionController(transport);cleanups.push(()=>controller.dispose());await controller.open();
  const pending=deferred<any>();transport.changeContext.mockReturnValueOnce(pending.promise);
  const switching=controller.changeContext("staff.1");expect(controller.getState().session).toBeNull();
  pending.resolve({...parentSession,selected_context_ref:"staff.1",selected_context_kind:"staff_assignment",assistant_context_key:"staff-scope.1"});await switching;
});

it("lets a parent start dialogue without selecting a child card",async()=>{
  const fetcher=vi.fn(async(url:string)=>{
    if(url.endsWith("/session"))return response({...parentSession,selected_context_ref:null,selected_context_kind:null});
    if(url.endsWith("/conversations"))return response(conversation());
    throw new Error("unexpected request");
  });vi.stubGlobal("fetch",fetcher);
  const module=await load("../src/app/SchoolFactsApp.vue");const host=document.createElement("div");document.body.append(host);
  const app=createApp(module.default as Component);app.mount(host);cleanups.push(()=>app.unmount());
  await vi.waitFor(()=>expect(host.querySelector('[data-action="send"]')).not.toBeNull());
  expect(host.textContent).toContain("资料卡查看孩子");
  expect(host.querySelector('[data-action="query-family-profile"]')).toBeNull();
  expect(fetcher.mock.calls.some(([url])=>url.endsWith("/family/profile"))).toBe(false);
  expect(host.querySelector('[data-role="staff-maintenance"]')).toBeNull();
});

it.each(["different_scope","logout","expired"])("keeps same-parent focus history but clears it on %s",async ending=>{
  const switching=deferred<Response>();const lateProfile=deferred<Response>();let conversationCount=0;let contextCount=0;
  const fetcher=vi.fn(async(url:string,init?:RequestInit)=>{
    if(url.endsWith("/session"))return response(parentSession);
    if(url.endsWith("/conversations"))return response({...conversation(conversationCount++===0?{text:"两个孩子的独立旧回答",facts:[]}:null),conversation_ref:`conversation.${conversationCount}`});
    if(url.endsWith("/context")){
      if(contextCount++===0)return switching.promise;
      return response({...parentSession,selected_context_ref:"child.a",assistant_context_key:"family-scope.new-login"});
    }
    if(url.endsWith("/family/profile"))return lateProfile.promise;
    if(url.endsWith("/logout"))return response(signedOut);
    if(url.endsWith("/close"))return response({detail:"expired"},401);
    throw new Error("unexpected request");
  });vi.stubGlobal("fetch",fetcher);
  const module=await load("../src/app/SchoolFactsApp.vue");const host=document.createElement("div");document.body.append(host);
  const app=createApp(module.default as Component);app.mount(host);cleanups.push(()=>app.unmount());
  await vi.waitFor(()=>expect(host.textContent).toContain("两个孩子的独立旧回答"));
  const assistant=host.querySelector('.assistant-conversation');
  input(host,'.assistant-conversation textarea',"等待中写好的新问题");
  click(host,"query-family-profile");await nextTick();
  const select=host.querySelector<HTMLSelectElement>('#engineering-context')!;
  select.value="child.b";select.dispatchEvent(new Event("change",{bubbles:true}));await nextTick();
  expect(host.querySelector('.assistant-conversation')).toBe(assistant);
  expect(host.querySelector('[data-action="query-family-profile"]')).toBeNull();
  const profileCall=fetcher.mock.calls.find(([url])=>url.endsWith("/family/profile"));
  expect((profileCall?.[1]?.signal as AbortSignal)?.aborted).toBe(true);
  switching.resolve(response({...parentSession,selected_context_ref:"child.b"}));
  await vi.waitFor(()=>expect(host.querySelector('[aria-label="孩子资料"]')?.textContent).toContain("合成孩子乙"));
  lateProfile.resolve(response({kind:"family_student_profile",status:"available",relationship_kind:"verified_guardian",profile:{name:"迟到的旧孩子"},
    query_scope:"本次获准孩子资料",sources:[],missing:[],authorized_fields:["name"],field_restrictions:{},
    as_of:"2026-09-23T00:00:00Z",queried_at:"2026-09-23T00:00:00Z",coverage:{status:"unknown",meaning:"未声明完整覆盖"}}));
  await nextTick();await nextTick();
  expect(host.querySelector('.assistant-conversation')).toBe(assistant);
  expect(host.textContent).toContain("两个孩子的独立旧回答");expect(host.textContent).not.toContain("迟到的旧孩子");
  expect((host.querySelector('.assistant-conversation textarea') as HTMLTextAreaElement).value).toBe("等待中写好的新问题");
  expect(conversationCount).toBe(1);
  if(ending==="different_scope"){
    const next=host.querySelector<HTMLSelectElement>('#engineering-context')!;next.value="child.a";next.dispatchEvent(new Event("change",{bubbles:true}));
    await vi.waitFor(()=>expect(conversationCount).toBe(2));
    expect(host.querySelector('.assistant-conversation')).not.toBe(assistant);
    expect((host.querySelector('.assistant-conversation textarea') as HTMLTextAreaElement).value).toBe("");
  }else{
    click(host,ending==="logout"?"logout":"new-conversation");
    await vi.waitFor(()=>expect(host.querySelector('.assistant-conversation')).toBeNull());
  }
  expect(host.textContent).not.toContain("两个孩子的独立旧回答");
});


it.each([false, true])("shows only the server-enabled workbench (directory=%s)", async (directoryEnabled) => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/session")) return response({ ...signedIn, directory_enabled: directoryEnabled, can_maintain: !directoryEnabled });
    if (url.endsWith("/conversations")) return response(conversation());
    throw new Error("unexpected request");
  }));
  const module = await load("../src/app/SchoolFactsApp.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
  await vi.waitFor(() => expect(host.querySelector(".workspace")).not.toBeNull());
  expect(host.querySelector('[data-role="contact-query"]') !== null).toBe(!directoryEnabled);
  expect(host.querySelector('.directory-workbench') !== null).toBe(directoryEnabled);
  if (directoryEnabled) expect(host.textContent).not.toContain("当前仅可查询获准信息");
});

it.each(["family", "directory"])("opens the Staff maintenance area only on intent and preserves complete %s confirmation", async owner => {
  const stamp = "2026-09-13T00:00:00Z";
  const family = owner === "family";
  const base = family ? "/family-facts" : "/facts";
  const factKind = family ? "person_region" : "person";
  const familyProposal = { kind: "fact_proposal", ...(family ? { family_owner: "school_facts.family_staff" } : {}), proposal_ref: "proposal.1", summary: { label: family ? "合成学生（学号A001）的区县资料" : "合成教师", reference: "fact.1", fact_kind: factKind, operation: "upsert", before: family ? { district_name: "原区县", district_code: "123456" } : { display_name: "原姓名", business_code: "123456" }, after: family ? { district_name: "新区县", district_code: "654321" } : { display_name: "新姓名", business_code: "654321" }, effective_at: stamp, reason: "核对资料", source_label: "正式维护" }, expires_at: "2099-01-01T00:00:00Z" };
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/session")) return response({ ...signedIn, directory_enabled: true, family_enabled: true, selected_context_kind: "staff_assignment" });
    if (url.endsWith("/conversations")) return response(conversation({ text: "请核对这次修改。", facts: [familyProposal] }));
    if (url.endsWith("/catalog")) return response({ families: [] });
    if (url.endsWith(base + "/proposals/proposal.1")) return response(familyProposal);
    if (url.endsWith(base + "/describe/" + factKind)) return response({ fact_kind: factKind, label: family ? "区县资料" : "人员", fields: [], creation_choices: [] });
    throw new Error("unexpected request");
  });
  vi.stubGlobal("fetch", fetcher);
  const module = await load("../src/app/SchoolFactsApp.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
  await vi.waitFor(() => expect(host.querySelector('[data-action="review-directory-proposal"]')).not.toBeNull());
  const maintenance = host.querySelector('[data-role="staff-maintenance"]') as HTMLDetailsElement;
  expect(maintenance).not.toBeNull();
  expect(maintenance.open).toBe(false);
  expect(host.querySelectorAll(".directory-workbench")).toHaveLength(2);
  for (const workbench of host.querySelectorAll(".directory-workbench")) expect(workbench.closest("details")).toBe(maintenance);
  click(host, "review-directory-proposal");
  await vi.waitFor(() => expect(host.querySelector('[data-action="confirm-directory"]')).not.toBeNull());
  expect(maintenance.open).toBe(true);
  expect(maintenance.textContent).toContain(familyProposal.summary.label);
  for (const value of [family ? "原区县" : "原姓名", family ? "新区县" : "新姓名", "123456", "654321"]) expect(maintenance.textContent).toContain(value);
  expect((host.querySelector('[data-action="confirm-directory"]') as HTMLButtonElement).disabled).toBe(true);
  const acknowledgement = maintenance.querySelector('input[type="checkbox"]') as HTMLInputElement;
  acknowledgement.checked = true; acknowledgement.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
  expect((host.querySelector('[data-action="confirm-directory"]') as HTMLButtonElement).disabled).toBe(false);
  expect(fetcher.mock.calls.some(([url]) => url.endsWith("/confirm"))).toBe(false);
});

it("opens the explicitly owned family candidate in its family workbench without submitting a query", async () => {
  const stamp = "2026-09-13T00:00:00Z";
  const candidate = { kind: "directory_facts", family_owner: "school_facts.family_staff", status: "ambiguous", fact_kind: "person", records: [1, 2].map(index => ({ reference: `family.person.${index}`, fact_kind: "person", label: "合成学生", fields: { display_name: "合成学生" }, version: 1, valid_from: stamp, valid_to: null, source: { label: "正式发布", reference: null, published_at: stamp } })), matched_count: 2, returned_count: 2, truncated: false, next_cursor: null, queried_at: stamp, factual_at: stamp, coverage: { status: "unknown", description: "当前获准范围" } };
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/session")) return response({ ...signedIn, directory_enabled: true, family_enabled: true, selected_context_kind: "staff_assignment" });
    if (url.endsWith("/conversations")) return response(conversation({ text: "请明确查询对象。", facts: [candidate] }));
    if (url.endsWith("/catalog")) return response({ families: [] });
    throw new Error("unexpected request");
  });
  vi.stubGlobal("fetch", fetcher);
  const module = await load("../src/app/SchoolFactsApp.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
  await vi.waitFor(() => expect(host.querySelectorAll('[aria-label="候选对象入口"] button')).toHaveLength(2));
  const maintenance = host.querySelector('[data-role="staff-maintenance"]') as HTMLDetailsElement;
  expect(maintenance.open).toBe(false);
  (host.querySelectorAll('[aria-label="候选对象入口"] button')[1] as HTMLButtonElement).click(); await nextTick();
  expect(maintenance.open).toBe(true);
  expect(host.querySelector('[aria-label="学生与家校资料"]')?.textContent).toContain("family.person.2");
  expect(host.querySelector('[aria-label="基础目录与事实维护"]')?.textContent).not.toContain("family.person.2");
  expect(fetcher.mock.calls.some(([url]) => url.endsWith("/query") || url.endsWith("/prepare") || url.endsWith("/confirm"))).toBe(false);
});


it("keeps old draft read-only through identity recheck until explicit selection", async () => {
  const { controller, transport } = await readyController();
  const before = controller.getState();
  controller.invalidateIdentity();
  expect(controller.getState()).toMatchObject({ authentication_required: true, draft: before.draft, result: before.result, context_epoch: before.context_epoch });
  await controller.prepare(); expect(transport.prepareContact).not.toHaveBeenCalled();
  transport.getSession.mockResolvedValue({ ...signedIn, selected_context_ref: null });
  await controller.recheckIdentity();
  expect(controller.getState().authentication_required).toBe(true);
  expect(controller.getState().context_epoch).toBe(before.context_epoch);
  expect(controller.getState().draft).toEqual(before.draft);
  await controller.changeContext("context.reader");
  expect(controller.getState().authentication_required).toBe(false);
  expect(controller.getState().context_epoch).toBeGreaterThan(before.context_epoch);
});


it.each([false, true])("propagates assistant 401 to the whole visible workspace without losing the draft (directory=%s)", async (directoryEnabled) => {
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/session")) return response({ ...signedIn, directory_enabled: directoryEnabled, can_maintain: !directoryEnabled });
    if (url.endsWith("/conversations")) return response(conversation());
    if (url.endsWith("/close")) return response({ detail: "PRIVATE" }, 401);
    if (url.endsWith("/catalog")) return response({ families: [] });
    throw new Error("unexpected request");
  });
  vi.stubGlobal("fetch", fetcher);
  const module = await load("../src/app/SchoolFactsApp.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(module.default as Component); app.mount(host); cleanups.push(() => app.unmount());
  await vi.waitFor(() => expect(host.querySelector('[data-action="new-conversation"]')).not.toBeNull());
  const textarea = host.querySelector('.assistant-conversation textarea') as HTMLTextAreaElement;
  textarea.value = "COPY_OLD_DRAFT"; textarea.dispatchEvent(new Event("input", { bubbles: true })); await nextTick();
  click(host, "new-conversation");
  await vi.waitFor(() => expect(host.querySelector('[data-action="recheck-identity"]')).not.toBeNull());
  expect(textarea.value).toBe("COPY_OLD_DRAFT");
  expect(host.textContent).not.toContain("再次点击新对话");
  expect((host.querySelector('[data-action="send"]') as HTMLButtonElement).disabled).toBe(true);
  expect((host.querySelector('[data-action="new-conversation"]') as HTMLButtonElement).disabled).toBe(true);
  const workbench = host.querySelector(directoryEnabled ? '.directory-workbench' : '[data-role="contact-query"]')!;
  expect([...workbench.querySelectorAll('button')].every(button => button.disabled)).toBe(true);
});
