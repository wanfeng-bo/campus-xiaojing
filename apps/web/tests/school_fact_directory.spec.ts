import { createApp, h, nextTick, type Component } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
const modules = import.meta.glob<any>(["../src/features/school_facts/directory_*.ts", "../src/features/school_facts/Directory*.vue"]);
async function load(file: string) { const loader = modules[`../src/features/school_facts/${file}`]; expect(loader, file).toBeTypeOf("function"); return loader!(); }
const stamp = "2026-09-06T00:00:00Z";
const record = { reference: "fact.person.1", fact_kind: "person", label: "合成教师", fields: { display_name: "合成教师", person_kind: "staff" }, version: 2, valid_from: stamp, valid_to: null, source: { label: "正式发布", kind: "manual", reference: "source.1", published_at: stamp } };
const result = { kind: "directory_facts", status: "known", fact_kind: "person", records: [record], matched_count: 1, returned_count: 1, truncated: false, next_cursor: null, queried_at: stamp, factual_at: stamp, coverage: { status: "unknown", description: "当前获权范围" } };
const descriptor = { fact_kind: "person", label: "人员", fields: [{ name: "display_name", label: "正式名称", type: "text", required: true, reference_kind: null, options: [] }, { name: "person_kind", label: "人员类别", type: "select", required: true, reference_kind: null, options: [{ value: "staff", label: "教职工" }] }], creation_choices: [{ creation_ref: "create.1", label: "合成新增人员" }] };
const proposal = { proposal_ref: "proposal.f2.1", summary: { label: "合成教师", reference: record.reference, fact_kind: "person", operation: "upsert", before: record.fields, after: { ...record.fields, display_name: "新名称" }, effective_at: stamp, reason: "核实名称", source_label: "正式维护" }, expires_at: "2099-01-01T00:00:00Z" };
const receipt = { reference: proposal.proposal_ref, status: "succeeded", message: "正式修改已完成", target_version: 3, changed_count: 1, request_id: "request.1" };
function transport() { return { catalog: vi.fn().mockResolvedValue({ families: [{ value: "person", label: "人员" }] }), describe: vi.fn().mockResolvedValue(descriptor), query: vi.fn().mockResolvedValue(result), original: vi.fn(), prepare: vi.fn().mockResolvedValue(proposal), proposal: vi.fn().mockResolvedValue(proposal), confirm: vi.fn().mockResolvedValue(receipt), receipt: vi.fn().mockResolvedValue(receipt) }; }
const cleanups: (() => void)[] = [];

it.each(["succeeded", "rejected", "unknown", "transport-failure"])("emits only authoritative operation results after rendering the %s receipt", async (status) => {
  const { default: Component } = await load("DirectoryWorkbench.vue");
  const api = transport();
  if (status === "transport-failure") api.confirm.mockRejectedValue(new Error("offline"));
  else api.confirm.mockResolvedValue({ ...receipt, status });
  const host = document.createElement("div"); document.body.append(host);
  const events = vi.fn(); const app = createApp(Component, { identityLabel: "维护员", api, onOperationResult: events });
  const component = app.mount(host) as unknown as { reviewProposal(ref: string): Promise<void> }; cleanups.push(() => app.unmount());
  for (let i = 0; i < 15; i++) await nextTick();
  await component.reviewProposal(proposal.proposal_ref); await nextTick();
  const ack = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  ack.checked = true; ack.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
  host.querySelector<HTMLButtonElement>('[data-action="confirm-directory"]')!.click();
  for (let i = 0; i < 20; i++) await nextTick();
  expect(host.querySelector('[aria-label="事实正式操作回执"]')).not.toBeNull();
  expect(events.mock.calls).toEqual(["transport-failure", "unknown"].includes(status) ? [] : [[proposal.proposal_ref]]);
  const check = [...host.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "查询原操作回执")!;
  check.click(); for (let i = 0; i < 20; i++) await nextTick();
  expect(events).toHaveBeenLastCalledWith(proposal.proposal_ref);
  expect(api.confirm).toHaveBeenCalledOnce(); expect(api.receipt).toHaveBeenCalledOnce();
});

it.each(["succeeded", "rejected"])("keeps unknown receipts recoverable until the original operation becomes %s", async (terminal) => {
  const { default: Component } = await load("DirectoryWorkbench.vue");
  const api = transport();
  api.confirm.mockResolvedValue({ ...receipt, status: "unknown" });
  api.receipt.mockResolvedValueOnce({ ...receipt, status: "unknown" })
    .mockResolvedValue({ ...receipt, status: terminal });
  const host = document.createElement("div"); document.body.append(host);
  const events = vi.fn(); const app = createApp(Component, { identityLabel: "维护员", api, onOperationResult: events });
  const component = app.mount(host) as unknown as { reviewProposal(ref: string): Promise<void> }; cleanups.push(() => app.unmount());
  for (let i = 0; i < 15; i++) await nextTick();
  await component.reviewProposal(proposal.proposal_ref); await nextTick();
  const ack = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  ack.checked = true; ack.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
  host.querySelector<HTMLButtonElement>('[data-action="confirm-directory"]')!.click();
  for (let i = 0; i < 20; i++) await nextTick();
  expect(host.textContent).toContain("结果尚待核对");
  expect(events).not.toHaveBeenCalled();
  const check = [...host.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "查询原操作回执")!;
  check.click(); for (let i = 0; i < 20; i++) await nextTick();
  expect(events).not.toHaveBeenCalled();
  check.click(); for (let i = 0; i < 20; i++) await nextTick();
  expect(events.mock.calls).toEqual([[proposal.proposal_ref]]);
  expect(api.confirm).toHaveBeenCalledOnce();
  expect(api.receipt.mock.calls).toEqual([[proposal.proposal_ref], [proposal.proposal_ref]]);
});

it("keeps evidence-only cards read-only even when a caller supplies workbench action flags", async () => {
  const { default: Result } = await load("DirectoryResult.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Result, { result, evidenceOnly: true, editable: true, selectable: true });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  expect(host.querySelector('[data-action="edit-directory"]')).toBeNull();
  expect(host.textContent).not.toContain("用此对象查询关联事实");
});
async function setup() { const { createDirectoryController } = await load("directory_session.ts"); const api = transport(); const controller = createDirectoryController(api); cleanups.push(controller.dispose); await controller.open(); await controller.chooseFamily("person"); await controller.query({ fact_kind: "person", scope: "all_authorized" }); controller.edit(record.reference); controller.setField("display_name", "新名称"); controller.setReason("核实名称"); return { api, controller }; }
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); document.body.replaceChildren(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("stages a controlled package draft without preparing or executing the old entry", async () => {
  const { createDirectoryController } = await load("directory_session.ts");
  const api = transport(), stage = vi.fn();
  const controller = createDirectoryController(api, {stage}); cleanups.push(controller.dispose);
  await controller.open(); await controller.chooseFamily("person");
  await controller.query({fact_kind:"person",scope:"all_authorized"});
  controller.edit(record.reference); controller.setField("display_name", "已核实名字");
  controller.setReason("核实名字");
  expect(await controller.prepare()).toBe(true);
  expect(stage).toHaveBeenCalledWith(expect.objectContaining({target_ref:record.reference,
    fields:{display_name:"已核实名字"}}));
  expect(api.prepare).not.toHaveBeenCalled(); expect(api.confirm).not.toHaveBeenCalled();
  expect(controller.getState().proposal).toBeNull();
  controller.invalidateIdentity(); await controller.prepare(); expect(stage).toHaveBeenCalledTimes(1);
});
describe("F2 directory controlled product", () => {
  it("validates unauthorized responses without exposing embedded facts", async () => { const { isDirectoryQuery } = await load("directory_contracts.ts"); expect(isDirectoryQuery(result)).toBe(true); expect(isDirectoryQuery({ ...result, status: "denied" })).toBe(false); expect(isDirectoryQuery({ ...result, returned_count: 5 })).toBe(false); });
  it("uses same-origin narrow requests and empty confirm bodies", async () => { const { createDirectoryApi } = await load("directory_api.ts"); const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(receipt))); vi.stubGlobal("fetch", fetcher); await createDirectoryApi().confirm(proposal.proposal_ref); expect(fetcher.mock.calls[0]).toEqual([`/api/v1/school-fact-product/facts/proposals/${proposal.proposal_ref}/confirm`, { method: "POST", credentials: "same-origin" }]); });
  it("requires complete authorized diff review and submits only partial edits", async () => { const { api, controller } = await setup(); await controller.prepare(); expect(api.prepare.mock.calls[0][0]).toEqual({ fact_kind: "person", target_ref: record.reference, operation: "upsert", fields: { display_name: "新名称" }, reason: "核实名称" }); expect(await controller.confirm()).toBe(false); await controller.reviewProposal(); expect(controller.getState().reviewed).toBe(true); await controller.confirm(); expect(api.confirm).toHaveBeenCalledOnce(); expect(controller.getState().receipt.status).toBe("succeeded"); });
  it("locks all new submissions after unknown and only queries the original receipt", async () => { const { api, controller } = await setup(); await controller.prepare(); await controller.reviewProposal(); api.confirm.mockRejectedValue(new Error("network")); await controller.confirm(); expect(controller.getState().receipt.status).toBe("unknown"); expect(controller.edit(record.reference)).toBe(false); await controller.prepare(); await controller.confirm(); expect(api.prepare).toHaveBeenCalledOnce(); expect(api.confirm).toHaveBeenCalledOnce(); await controller.checkReceipt(); expect(api.receipt).toHaveBeenCalledWith(proposal.proposal_ref); expect(controller.getState().receipt.status).toBe("succeeded"); });
  it("disposal clears drafts/results and ignores late requests", async () => { const { api, controller } = await setup(); let resolve!: (value: unknown) => void; api.query.mockReturnValue(new Promise(done => { resolve = done; })); const pending = controller.query({ fact_kind: "person", scope: "all_authorized" }); controller.dispose(); resolve(result); await pending; expect(controller.getState().result).toBeNull(); expect(controller.getState().fields).toEqual({}); });
  it("rejects a mismatched prepared target and unauthorized reference edits", async () => { const { api, controller } = await setup(); api.prepare.mockResolvedValue({ ...proposal, summary: { ...proposal.summary, reference: "another" } }); await controller.prepare(); expect(controller.getState().proposal).toBeNull(); expect(controller.getState().error).toBeTruthy(); expect(controller.setField("organization_ref", "invented")).toBe(false); });
  it("renders typed workbench with identity, coverage and authorized creation options", async () => { const { default: Component } = await load("DirectoryWorkbench.vue"); const api = transport(); const host = document.createElement("div"); document.body.append(host); const app = createApp({ render: () => h(Component as Component, { identityLabel: "合成目录维护员", api }) }); app.mount(host); cleanups.push(() => app.unmount()); for (let i = 0; i < 15; i++) await nextTick(); expect(host.textContent).toContain("合成目录维护员"); expect(host.querySelector('[data-role="directory-family"]')).not.toBeNull(); expect(host.textContent).toContain("合成新增人员"); expect(host.textContent).not.toContain("source_key"); });
});

it("keeps compact proposal metadata separate from complete confirmable differences", async () => {
  const { isDirectoryProposal, isCompactDirectoryProposal } = await load("directory_contracts.ts");
  const compact = { ...proposal, kind: "fact_proposal", summary: { ...proposal.summary, fact_kind: "policy_version", after: { original_text: { excerpt: "开头", length: 9000, offset: 0, end: 2, complete: false, sha256: "a".repeat(64), notice: "仅节选，请核对完整原文" } } } };
  expect(isDirectoryProposal(compact)).toBe(false);
  expect(isCompactDirectoryProposal(compact)).toBe(true);
});
it("invalidates dependent references when the quality target family changes", async () => {
  const { api, controller } = await setup();
  const qualityDescriptor = { ...descriptor, fact_kind: "fact_quality", fields: [{ name: "target_fact_kind", label: "对应事实类型", type: "select", required: true, reference_kind: null, options: [{ value: "person", label: "人员" }, { value: "organization", label: "组织" }] }, { name: "target_ref", label: "对应事实对象", type: "reference", required: true, reference_kind: null, options: [] }, { name: "replacement_ref", label: "纠正后对象", type: "reference", required: false, reference_kind: null, options: [] }] };
  api.catalog.mockResolvedValue({ families: [{ value: "fact_quality", label: "事实来源与质量" }] }); await controller.open(); api.describe.mockResolvedValue(qualityDescriptor); await controller.chooseFamily("fact_quality"); controller.create("create.1"); controller.setField("target_fact_kind", "person"); await controller.loadChoices("target_ref"); expect(api.query).toHaveBeenLastCalledWith({ fact_kind: "person", scope: "all_authorized" }); expect(controller.setField("target_ref", record.reference)).toBe(true); controller.setField("target_fact_kind", "organization"); expect(controller.getState().fields.target_ref).toBeNull(); expect(controller.setField("target_ref", record.reference)).toBe(false);
});
it("checks full policy original completeness and codepoint length", async () => {
  const { isDirectoryOriginal } = await load("directory_contracts.ts"); const original = { kind: "policy_original", reference: "policy.1", version: 1, text: "制度😀全文", offset: 0, end: 5, length: 5, complete: true, has_more: false, source: record.source, notice: "正式原文" };
  expect(isDirectoryOriginal(original)).toBe(true); expect(isDirectoryOriginal({ ...original, complete: false, has_more: true })).toBe(false); expect(isDirectoryOriginal({ ...original, text: "截断" })).toBe(false);
});
it("routes compact chat proposals to full workbench review and shows policy excerpts safely", async () => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const excerpt = { excerpt: "正式制度开头", length: 9000, offset: 0, end: 6, complete: false, sha256: "a".repeat(64), notice: "仅为节选，请查看完整原文" };
  const compactQuery = { ...result, fact_kind: "policy_version", records: [{ ...record, fact_kind: "policy_version", fields: { original_text: excerpt } }] };
  const compactProposal = { ...proposal, kind: "fact_proposal", summary: { ...proposal.summary, fact_kind: "policy_version", after: { original_text: excerpt } } };
  const onReviewDirectory = vi.fn(); const host = document.createElement("div"); document.body.append(host); const app = createApp({ render: () => h(Answer, { payload: { text: "请核对", facts: [compactQuery, compactProposal] }, onReviewDirectory }) }); app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  expect(host.textContent).toContain("正式制度开头"); expect(host.textContent).toContain("9000"); expect(host.textContent).toContain("查看完整制度原文"); (host.querySelector('[data-action="review-directory-proposal"]') as HTMLButtonElement).click(); expect(onReviewDirectory).toHaveBeenCalledWith(proposal.proposal_ref); expect(host.textContent).not.toContain("[object Object]");
});
it("requires an explicit acknowledgement before the workbench confirmation click", async () => {
  const { default: Workbench } = await load("DirectoryWorkbench.vue"); const api = transport(); const host = document.createElement("div"); document.body.append(host); const app = createApp(Workbench, { identityLabel: "本次履职身份", api }); const instance = app.mount(host) as unknown as { reviewProposal(ref: string): Promise<void> }; cleanups.push(() => app.unmount()); for (let i = 0; i < 15; i++) await nextTick(); await instance.reviewProposal(proposal.proposal_ref); await nextTick(); const button = host.querySelector('[data-action="confirm-directory"]') as HTMLButtonElement; expect(button.disabled).toBe(true); expect(host.textContent).toContain("新名称"); const check = host.querySelector('input[type="checkbox"]') as HTMLInputElement; check.checked = true; check.dispatchEvent(new Event("change", { bubbles: true })); await nextTick(); expect(button.disabled).toBe(false); button.click(); for (let i = 0; i < 6; i++) await nextTick(); expect(api.confirm).toHaveBeenCalledExactlyOnceWith(proposal.proposal_ref);
});

it("localizes known proposal enums without concealing complete target and relationship differences", async () => {
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const before = { owner_kind: "location", location_ref: "location.same-name.1", contact_kind: "phone", contact_value: "0571-00000001", purpose: "location" };
  const after = { ...before, location_ref: "location.same-name.2", contact_value: "0571-10000002" };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Proposal, { proposal: { ...proposal, summary: { ...proposal.summary, fact_kind: "official_contact", before, after } }, complete: true });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  const rows = [...host.querySelectorAll("tbody tr")];
  expect(rows.find(row => row.querySelector("th")?.textContent === "所属对象类型")?.textContent).toBe("所属对象类型地点地点");
  expect(rows.find(row => row.querySelector("th")?.textContent === "联系类型")?.textContent).toBe("联系类型电话电话");
  expect(rows.find(row => row.querySelector("th")?.textContent === "地点用途")?.textContent).toBe("地点用途locationlocation");
  for (const value of [record.reference, before.location_ref, after.location_ref, before.contact_value, after.contact_value, "尚未执行", "正式维护"]) expect(host.textContent).toContain(value);
  expect(host.querySelector("table")?.closest("details")).toBeNull();
});

it.each(["single", "primary-copy", "distinct-effective-time"])("counts the complete %s change set without omitting the main item", async (shape) => {
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const main = structuredClone(proposal.summary);
  const impacts = shape === "single" ? [] : [structuredClone(main)];
  if (shape === "distinct-effective-time") impacts.push({ ...main, effective_at: "2027-01-01T00:00:00Z" });
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Proposal, { proposal: { ...proposal, summary: { ...main, impacts } }, complete: true });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  expect(host.querySelector('[aria-label="本次修改总数"]')?.textContent).toBe(`本次全部修改（${shape === "distinct-effective-time" ? 2 : 1}项）`);
  expect(host.querySelectorAll('[aria-label="全部联动修改"] article')).toHaveLength(impacts.length);
  expect(host.textContent).toContain("尚未执行");
});

it("does not describe the compact proposal as a complete counted change set", async () => {
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Proposal, { proposal, complete: false }); app.mount(host);
  cleanups.push(() => app.unmount()); await nextTick();
  expect(host.querySelector('[aria-label="本次修改总数"]')).toBeNull();
  expect(host.textContent).toContain("核对完整修改");
});

it("does not borrow labels from a different enum when a value is unknown for its field", async () => {
  const { directoryFieldValue } = await load("directory_labels.ts");
  expect(directoryFieldValue("owner_kind", "phone")).toBe("phone");
  expect(directoryFieldValue("contact_kind", "location")).toBe("location");
  expect(directoryFieldValue("owner_kind", "location")).toBe("地点");
  expect(directoryFieldValue("contact_kind", "phone")).toBe("电话");
  expect(directoryFieldValue("purpose", "phone")).toBe("phone");
});

it("uses descriptor option labels for current editing values and retains original confirmation binding", async () => {
  const { default: Workbench } = await load("DirectoryWorkbench.vue"); const api = transport();
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Workbench, { identityLabel: "本次履职身份", api });
  const instance = app.mount(host) as unknown as { reviewProposal(ref: string): Promise<void> }; cleanups.push(() => app.unmount());
  for (let i = 0; i < 15; i++) await nextTick();
  const queryButton = [...host.querySelectorAll("button")].find(button => button.textContent === "查询正式事实")!;
  queryButton.click(); for (let i = 0; i < 10; i++) await nextTick();
  (host.querySelector('[data-action="edit-directory"]') as HTMLButtonElement).click(); await nextTick();
  const currentValues = [...host.querySelectorAll(".current-value")].map(node => node.textContent).join(" ");
  expect(currentValues).toContain("当前：教职工");
  expect(currentValues).not.toContain("staff");
  await instance.reviewProposal(proposal.proposal_ref); await nextTick();
  expect(host.textContent).toContain("本次履职身份");
  expect(host.textContent).toContain("新名称");
  const confirm = host.querySelector('[data-action="confirm-directory"]') as HTMLButtonElement;
  expect(confirm.disabled).toBe(true);
  const check = host.querySelector('input[type="checkbox"]') as HTMLInputElement;
  check.checked = true; check.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
  confirm.click(); for (let i = 0; i < 10; i++) await nextTick();
  expect(api.confirm).toHaveBeenCalledExactlyOnceWith(proposal.proposal_ref);
  const card = host.querySelector('[aria-label="事实正式操作回执"]')!;
  const visible = card.cloneNode(true) as HTMLElement; visible.querySelectorAll("details").forEach(node => node.remove());
  expect(visible.textContent).toContain("正式修改已完成");
  expect(visible.textContent).toContain("实际变更 1 项");
  expect(visible.textContent).not.toContain(receipt.reference);
  expect(visible.textContent).not.toContain(receipt.request_id);
  const details = card.querySelector("details") as HTMLDetailsElement;
  expect(details.open).toBe(false);
  expect(details.textContent).toContain(receipt.reference);
  expect(details.textContent).toContain(receipt.request_id);
});
it("clears a late policy original when a reused card changes query", async () => {
  const { shallowRef } = await import("vue"); const { default: Result } = await load("DirectoryResult.vue");
  const current = shallowRef({ ...result, fact_kind: "policy_version", records: [{ ...record, fact_kind: "policy_version" }] });
  let resolve!: (value: Response) => void; vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(done => { resolve = done; })));
  const host = document.createElement("div"); document.body.append(host); const app = createApp({ render: () => h(Result, { result: current.value }) }); app.mount(host); cleanups.push(() => app.unmount());
  (Array.from(host.querySelectorAll("button")).find(button => button.textContent === "查看完整制度原文") as HTMLButtonElement).click(); current.value = { ...result, queried_at: "2026-09-07T00:00:00Z", records: [] }; await nextTick();
  resolve(new Response(JSON.stringify({ kind: "policy_original", reference: record.reference, version: 1, text: "旧原文不应复活", offset: 0, end: 7, length: 7, complete: true, has_more: false, source: record.source, notice: "正式原文" })));
  await new Promise(done => setTimeout(done, 0)); await nextTick(); expect(host.textContent).not.toContain("旧原文不应复活");
});

it("shows scoped formal-negative quality claims separately from empty records", async () => {
  const { default: Result } = await load("DirectoryResult.vue"); const quality = { existence: "formal_negative", coverage: "unknown", source_availability: "unavailable", freshness: "review_overdue", conflict: "unknown", evidence_status: "known", evidence_truncated: false, notice: "仅针对声明列明的对象和时点", statements: [{ reference: "quality.1", kind: "negative", target_fact_kind: "official_contact", target_ref: null, owner_kind: "organization", owner_ref: "org.1", applies_to_query: false, applies_to_records: [], valid_from: stamp, valid_to: null, purpose: "接待", contact_kind: "电话", negative_code: "未设接待电话", responsibility: "办公室", evidence_reference: "正式核验依据" }] };
  const host = document.createElement("div"); document.body.append(host); const app = createApp({ render: () => h(Result, { result: { ...result, status: "no_current_records", records: [], returned_count: 0, matched_count: 0, quality } }) }); app.mount(host); cleanups.push(() => app.unmount()); await nextTick(); expect(host.textContent).toContain("正式否定声明"); expect(host.textContent).toContain("未设接待电话"); expect(host.textContent).toContain("仅限列明对象及用途"); expect(host.textContent).toContain("org.1"); expect(host.textContent).toContain("已到核实期限");
});


it("shows the shared selector guidance without claiming unsupported selections are missing facts", async () => {
  const { default: Result } = await load("DirectoryResult.vue");
  const { isDirectoryQuery } = await load("directory_contracts.ts");
  const query_support = { name_fields: [], name_match: "exact", related_kinds: ["organization", "location"], reference: true, all_authorized: true, related_match: "direct", guidance: "先查询地点取得获权引用，再查询直接关联。" };
  const rejected = { ...result, status: "unsupported_selection", records: [], returned_count: 0, matched_count: null, query_support };
  expect(isDirectoryQuery(rejected)).toBe(true);
  expect(isDirectoryQuery({ ...rejected, query_support: { guidance: "unvalidated" } })).toBe(false);
  expect(isDirectoryQuery({ ...rejected, records: [record], returned_count: 1, matched_count: 1 })).toBe(false);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Result, { result: rejected }) }); app.mount(host); cleanups.push(() => app.unmount());
  expect(host.textContent).toContain("查询条件不受此事实类型支持");
  expect(host.textContent).toContain(query_support.guidance);
  expect(host.textContent).not.toContain("尚未录入");
});


it("uses descriptor capabilities in the workbench instead of offering unsupported keyword search", async () => {
  const { default: Workbench } = await load("DirectoryWorkbench.vue"); const api = transport();
  api.describe.mockResolvedValue({ ...descriptor, query_support: { name_fields: [], name_match: "exact", related_kinds: ["location"], reference: true, all_authorized: true, related_match: "direct", guidance: "请先取得地点引用，再查询直接关联。" } } as any);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Workbench, { identityLabel: "教师", api }) }); app.mount(host); cleanups.push(() => app.unmount());
  for (let i = 0; i < 10; i++) await nextTick();
  expect((host.querySelector('input[maxlength="1000"]') as HTMLInputElement).disabled).toBe(true);
  expect(host.textContent).toContain("请先取得地点引用，再查询直接关联。");
  expect(host.textContent).not.toContain("名称或关键词");
});

it.each([
  ["organization", "组织路径"],
  ["location", "地点路径"],
])("keeps %s path labels contextual in result and descriptor-free proposal cards", async (factKind, expected) => {
  const { default: Result } = await load("DirectoryResult.vue");
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const fields = { display_path: "合成上级/合成对象" };
  const contextualRecord = { ...record, fact_kind: factKind, fields };
  const contextualProposal = { ...proposal, summary: { ...proposal.summary, fact_kind: factKind, before: fields, after: fields } };
  for (const [view, props] of [[Result, { result: { ...result, fact_kind: factKind, records: [contextualRecord] } }], [Proposal, { proposal: contextualProposal, complete: true }]] as const) {
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp({ render: () => h(view as Component, props) });
    app.mount(host);
    cleanups.push(() => app.unmount());
    await nextTick();
    expect(host.textContent).toContain(expected);
    if (factKind === "organization") expect(host.textContent).not.toContain("地点路径");
  }
});


it("distinguishes authorization target counts from business records", async () => {
  const { isDirectoryAccess, accessStatusLabel } = await load("directory_contracts.ts");
  const scope = { kind: "fact_access", requested_fact_kind: "policy", notice: "授权目标不是资料条数", queried_at: "2026-09-06T00:00:00Z", families: [{ fact_kind: "policy", label: "制度", status: "authorized_scope", authorized_target_count: 2 }] };
  expect(isDirectoryAccess(scope)).toBe(true);
  expect(isDirectoryAccess({ ...scope, families: [{ ...scope.families[0], authorized_target_count: -1 }] })).toBe(false);
  expect(accessStatusLabel("source_unavailable")).not.toBe(accessStatusLabel("denied"));
  expect(isDirectoryAccess({ ...scope, requested_fact_kind: null })).toBe(true);
  expect(isDirectoryAccess({ ...scope, requested_fact_kind: "person" })).toBe(false);
  expect(isDirectoryAccess({ ...scope, requested_fact_kind: 1 })).toBe(false);
  expect(isDirectoryAccess({ ...scope, requested_fact_kind: "" })).toBe(false);
});

it("accepts a bounded authorized directory bundle and rejects invalid nested evidence", async () => {
  const { isDirectoryBundle } = await load("directory_contracts.ts");
  const bundle = { kind: "directory_bundle", status: "known", subject_label: "合成工程校区资料", results: [result] };
  expect(isDirectoryBundle(bundle)).toBe(true);
  expect(isDirectoryBundle({ ...bundle, status: "denied", subject_label: null, results: [] })).toBe(true);
  expect(isDirectoryBundle({ ...bundle, status: "partial", results: [{ kind: "directory_facts", fact_kind: "official_contact", status: "source_unavailable", records: [], query: { target: "合成工程校区" } }] })).toBe(true);
  expect(isDirectoryBundle({ ...bundle, results: [{ ...result, status: "denied" }] })).toBe(false);
  expect(isDirectoryBundle({ ...bundle, results: [{ kind: "directory_facts", fact_kind: "official_contact", status: "known", records: [], query: {} }] })).toBe(false);
  expect(isDirectoryBundle({ ...bundle, results: Array(33).fill(result) })).toBe(false);
});


it("accepts only explicit unread partial issues within the existing bundle bound", async () => {
  const { isDirectoryBundle } = await load("directory_contracts.ts");
  const unread = { kind: "directory_facts", fact_kind: "official_contact", status: "partial", records: [], truncated: true, query: { reference: "contact.1" }, notice: "本次读取范围尚未全部核实。" };
  const bundle = { kind: "directory_bundle", status: "partial", subject_label: "服务及联系方式", results: [result, unread] };
  expect(isDirectoryBundle(bundle)).toBe(true);
  for (const invalid of [
    { ...unread, truncated: false },
    { ...unread, truncated: undefined },
    { ...unread, notice: undefined },
    { ...unread, notice: " " },
    { ...unread, notice: 1 },
    { ...unread, query: [] },
    { ...unread, records: [record] },
    { ...unread, status: "known" },
  ]) expect(isDirectoryBundle({ ...bundle, results: [result, invalid] })).toBe(false);
  expect(isDirectoryBundle({ ...bundle, results: Array(32).fill(unread) })).toBe(true);
  expect(isDirectoryBundle({ ...bundle, results: Array(33).fill(unread) })).toBe(false);
});

it("keeps partial lookup risk and completed evidence visible when a dependent read is unfinished", async () => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const unread = { kind: "directory_facts", fact_kind: "official_contact", status: "partial", records: [], truncated: true, query: { reference: "contact.1" }, notice: "本次读取范围尚未全部核实。" };
  const payload = { text: "已取得部分资料，还有关联联系方式尚未核实。", facts: [{ kind: "directory_bundle", status: "partial", subject_label: "服务及联系方式", results: [result, unread] }] };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Answer as Component, { payload }) }); app.mount(host); cleanups.push(() => app.unmount());
  await nextTick();
  expect(host.textContent).toContain("服务及联系方式：只取得部分正式依据");
  expect(host.querySelectorAll(".directory-result")).toHaveLength(1);
  const disclosure = host.querySelector("details") as HTMLDetailsElement;
  expect(disclosure).not.toBeNull();
  expect(disclosure.open).toBe(false);
  disclosure.open = true;
  await nextTick();
  expect(host.textContent).toContain(record.label);
  expect(host.textContent).toContain(record.source.label);
});

it("keeps minimal bundle failures visible without rendering them as complete evidence", async () => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const payload = { text: "只取得了部分正式资料。", facts: [{ kind: "directory_bundle", status: "partial", subject_label: "校区联系", results: [
    result,
    { kind: "directory_facts", fact_kind: "official_contact", status: "source_unavailable", records: [], query: { target: "合成第二校区" } },
  ] }] };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Answer as Component, { payload }) }); app.mount(host); cleanups.push(() => app.unmount());
  await nextTick();
  expect(host.textContent).toContain("校区联系：只取得部分正式依据");
  expect(host.textContent).toContain("正式资料来源暂时不可用");
  expect(host.querySelectorAll(".directory-result")).toHaveLength(1);
});

it("groups assistant evidence behind one disclosure while keeping important notices visible", async () => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const conflict = { ...result, quality: { existence: "known", coverage: "unknown", source_availability: "available", freshness: "review_overdue", conflict: "conflicting_statements", evidence_status: "known", evidence_truncated: false, notice: "存在待核实质量事项", statements: [] } };
  const denied = { ...result, status: "denied", records: [], matched_count: null, returned_count: 0 };
  const payload = { text: "合成工程校区的正式名称是合成工程校区。", facts: [{ kind: "directory_bundle", status: "known", subject_label: "校区与组织", results: [conflict, denied] }] };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Answer as Component, { payload }) }); app.mount(host); cleanups.push(() => app.unmount());
  await nextTick();
  expect(host.textContent).toContain(payload.text);
  expect(host.textContent).toContain("当前身份无权查询此范围");
  expect(host.textContent).toContain("有相互矛盾的记录，请核实后使用");
  expect(host.textContent).toContain("已过登记的复核日期");
  expect(host.querySelectorAll("details.evidence")).toHaveLength(1);
  expect([...host.querySelectorAll("details.maintenance-information")].every(node => !(node as HTMLDetailsElement).open)).toBe(true);
  expect(host.querySelector("summary")?.textContent).toContain("查看依据");
  expect((host.querySelector("details") as HTMLDetailsElement).open).toBe(false);
  expect(host.textContent).not.toContain("staff");
  (host.querySelector("details") as HTMLDetailsElement).open = true;
  await nextTick();
  expect(host.textContent).toContain(record.reference);
  expect(host.textContent).toContain("教职工");
  expect(host.textContent).not.toContain("person_kind");
});

it("does not treat absent quality statements as an unconfirmed publication", async () => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const unknownQuality = { ...result, quality: { existence: "unknown", coverage: "unknown", source_availability: "unknown", freshness: "unknown", conflict: "unknown", evidence_status: "no_current_records", evidence_truncated: false, notice: "没有当前质量声明", statements: [] } };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Answer as Component, { payload: { text: "已取得正式资料。", facts: [unknownQuality] } }) }); app.mount(host); cleanups.push(() => app.unmount());
  await nextTick();
  expect(host.textContent).not.toContain("发布来源未确认");
  expect(host.textContent).not.toContain("来源暂时不可用");
  (host.querySelector("details") as HTMLDetailsElement).open = true;
  await nextTick();
  expect(host.textContent).toContain("来源：正式发布");
});


it.each([
  ["unavailable", "known", "原来源目前不可用", "本次无法核对资料依据"],
  ["unknown", "source_unavailable", "本次无法核对资料依据", "原来源目前不可用"],
  ["unknown", "forbidden", "核验依据不在你的可查看范围", "原来源目前不可用"],
])("separates declared source risk %s from current quality read %s", async (source, evidence, expected, absent) => {
  const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
  const quality = { existence: "known", coverage: "unknown", source_availability: source, freshness: "unknown", conflict: "unknown", evidence_status: evidence, evidence_truncated: false, notice: "声明适用本次对象", statements: [] };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Answer as Component, { payload: { text: "已取得正式资料。", facts: [{ ...result, quality }] } }) }); app.mount(host); cleanups.push(() => app.unmount());
  await nextTick();
  const notices = [...host.querySelectorAll(".important-notice")].map(node => node.textContent).join(" ");
  expect(notices).toContain(expected);
  expect(notices).not.toContain(absent);
});


it("locks old directory mutations on identity failure while retaining reviewed draft", async () => {
  const { ProductApiError } = await import("../src/features/school_facts/product_api");
  const { api, controller } = await setup(); await controller.prepare();
  const before = controller.getState();
  api.original.mockRejectedValue(new ProductApiError(401));
  await controller.readOriginal(record.reference);
  expect(controller.getState()).toMatchObject({ authentication_required: true, fields: before.fields, reason: before.reason, proposal: before.proposal });
  await controller.confirm(); await controller.prepare(); await controller.query({fact_kind:"person",scope:"all_authorized"});
  expect(api.confirm).not.toHaveBeenCalled(); expect(api.prepare).toHaveBeenCalledTimes(1); expect(api.query).toHaveBeenCalledTimes(1);
});


it("does not continue a late proposal lookup into another request after external identity invalidation", async () => {
  const { api, controller } = await setup();
  let complete!: (value: unknown) => void;
  api.proposal.mockReturnValue(new Promise(resolve => { complete = resolve; }));
  const before = api.describe.mock.calls.length;
  const reviewing = controller.reviewProposal(proposal.proposal_ref);
  controller.invalidateIdentity(); complete(proposal); await reviewing;
  expect(api.describe).toHaveBeenCalledTimes(before);
  expect(controller.getState().authentication_required).toBe(true);
});


it("keeps the original pending receipt visible when another surface invalidates identity", async () => {
  const { api, controller } = await setup(); await controller.prepare(); await controller.reviewProposal(proposal.proposal_ref);
  let complete!: (value: unknown) => void;
  api.confirm.mockReturnValue(new Promise(resolve => { complete = resolve; }));
  const confirming = controller.confirm(); expect(api.confirm).toHaveBeenCalledTimes(1); controller.invalidateIdentity();
  expect(controller.getState().receipt).toMatchObject({ reference: proposal.proposal_ref, status: "unknown" });
  complete(receipt); await confirming;
  expect(controller.getState().receipt?.status).toBe("unknown");
  await controller.confirm(); expect(api.confirm).toHaveBeenCalledTimes(1);
});


it.each(["answer", "workbench"])("propagates the real original-text button 401 through %s", async (surface) => {
  const policyQuery = { ...result, fact_kind: "policy_version", records: [{ ...record, fact_kind: "policy_version" }] };
  const onIdentityInvalid = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"PRIVATE"}', { status: 401 })));
  const host = document.createElement("div"); document.body.append(host);
  let app;
  if (surface === "answer") {
    const { default: Answer } = await import("../src/features/school_facts/product_answer.vue");
    app = createApp({ render: () => h(Answer, { payload: { text: "依据", facts: [policyQuery] }, onIdentityInvalid }) });
  } else {
    const { default: Workbench } = await load("DirectoryWorkbench.vue"); const api = transport(); api.query.mockResolvedValue(policyQuery);
    api.catalog.mockResolvedValue({ families: [{ value: "policy_version", label: "制度版本" }] });
    api.describe.mockResolvedValue({ ...descriptor, fact_kind: "policy_version" });
    app = createApp(Workbench, { identityLabel: "合成身份", api, onIdentityInvalid });
  }
  app.mount(host); cleanups.push(() => app.unmount());
  if (surface === "workbench") {
    await vi.waitFor(() => expect(host.querySelector<HTMLSelectElement>('[data-role="directory-family"]')?.value).toBe("policy_version"));
    const queryButton = [...host.querySelectorAll("button")].find(button => button.textContent === "查询正式事实")!;
    queryButton.click();
  }
  await vi.waitFor(() => expect([...host.querySelectorAll("button")].some(button => button.textContent === "查看完整制度原文")).toBe(true));
  [...host.querySelectorAll("button")].find(button => button.textContent === "查看完整制度原文")!.click();
  await vi.waitFor(() => expect(onIdentityInvalid).toHaveBeenCalledOnce());
  expect(host.textContent).not.toContain("PRIVATE");
});

it("rejects a late original after disabled changes even if the same card becomes enabled again", async () => {
  const { shallowRef } = await import("vue"); const { default: Result } = await load("DirectoryResult.vue");
  const disabled = shallowRef(false); const onIdentityInvalid = vi.fn();
  const policyQuery = { ...result, fact_kind: "policy_version", records: [{ ...record, fact_kind: "policy_version" }] };
  let complete!: (value: Response) => void;
  const fetcher = vi.fn().mockReturnValue(new Promise(resolve => { complete = resolve; })); vi.stubGlobal("fetch", fetcher);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(Result, { disabled: disabled.value, result: policyQuery, onIdentityInvalid }) });
  app.mount(host); cleanups.push(() => app.unmount());
  const button = [...host.querySelectorAll("button")].find(button => button.textContent === "查看完整制度原文")!;
  button.click(); disabled.value = true; await nextTick(); disabled.value = false; await nextTick();
  complete(new Response(JSON.stringify({ kind: "policy_original", reference: record.reference, version: 1, text: "旧原文不应复活", offset: 0, end: 7, length: 7, complete: true, has_more: false, source: record.source, notice: "正式原文" })));
  await new Promise(resolve => setTimeout(resolve, 0)); await nextTick();
  expect(host.textContent).not.toContain("旧原文不应复活"); expect(onIdentityInvalid).not.toHaveBeenCalled();
});

it("sends the descriptor-selected relationship field for F3 without changing F2 requests", async () => {
  const { default: Workbench } = await load("DirectoryWorkbench.vue");
  const api = transport();
  api.catalog.mockResolvedValue({ families: [{ value: "student_school_relationship", label: "Student enrollment" }] });
  api.describe.mockResolvedValue({ ...descriptor, fact_kind: "student_school_relationship", query_support: { name_fields: [], name_match: "exact", related_kinds: ["location"], related_fields: [{ field: "primary_campus_ref", kind: "location", label: "Primary campus" }, { field: "administrative_campus_ref", kind: "location", label: "Administrative campus" }], reference: true, all_authorized: true, related_match: "direct", guidance: "Choose one relationship field." } } as any);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Workbench, { identityLabel: "Teacher", api });
  const instance = app.mount(host) as unknown as { selectRelated(view: typeof result, reference: string): void };
  cleanups.push(() => app.unmount());
  for (let i = 0; i < 10; i++) await nextTick();
  instance.selectRelated({ ...result, fact_kind: "location", records: [{ ...record, fact_kind: "location" }] }, record.reference);
  await nextTick();
  const selector = host.querySelector('[data-role="related-field"]') as HTMLSelectElement;
  expect(selector).not.toBeNull();
  selector.value = "administrative_campus_ref";
  selector.dispatchEvent(new Event("change", { bubbles: true }));
  await nextTick();
  [...host.querySelectorAll("button")].find(button => button.textContent === "查询关联事实")!.click();
  await nextTick();
  expect(api.query).toHaveBeenLastCalledWith({ fact_kind: "student_school_relationship", scope: "selected", related_field: "administrative_campus_ref", related_ref: record.reference });
});


it("keeps creation handles out of the default maintenance preview", async () => {
  const { default: Workbench } = await load("DirectoryWorkbench.vue");
  const api = transport(); const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Workbench, { identityLabel: "Teacher", api });
  const instance = app.mount(host) as unknown as { };
  void instance; cleanups.push(() => app.unmount());
  for (let i = 0; i < 10; i++) await nextTick();
  const createSelect = [...host.querySelectorAll("select")].find(select => [...select.options].some(option => option.value === "create.1"))!;
  createSelect.value = "create.1"; createSelect.dispatchEvent(new Event("change", { bubbles: true })); await nextTick();
  [...host.querySelectorAll("button")].find(button => button.textContent === "填写新对象")!.click(); await nextTick();
  const visible = host.cloneNode(true) as HTMLElement; visible.querySelectorAll("details").forEach(node => node.remove());
  expect(visible.textContent).not.toContain("create.1");
  expect(visible.textContent).toContain("正式创建后生成");
  expect(host.querySelector("details")?.textContent).toContain("create.1");
});

it.each([
  ["student", { status: "active", person_ref: "person.explicit.1" }, ["学生状态", "有效", "人员"]],
  ["student_school_relationship", { student_ref: "student.explicit.1", enrollment_status: "enrolled", residence_mode: "day", primary_campus_ref: "campus.explicit.1", administrative_campus_ref: "campus.explicit.2" }, ["学生", "在校状态", "在读", "住校方式", "走读", "主要校区", "行政校区"]],
  ["student_contact", { contact_person_ref: "person.explicit.2", relationship_kind: "father", priority: 1, is_emergency: true }, ["联系人", "与学生关系", "父亲", "联系顺序", "紧急联系人", "是"]],
  ["guardian_relationship", { guardian_person_ref: "person.explicit.3", relationship_kind: "verified_guardian", access_purpose: "guardian_student_profile", status: "pending_verification", verification_reference: "人工核验记录", review_of_relationship_ref: "relation.explicit.1" }, ["家长或监护人", "家校关系类型", "已核实监护人", "家校访问用途", "监护人学生档案", "家校关系状态", "待核验", "核验依据", "被纠正家校关系"]],
  ["person_region", { person_ref: "person.explicit.1", district_code: "330000", district_name: "合成区县", region_purpose: "student_profile" }, ["人员", "区县代码", "区县名称", "地区用途", "学生档案"]],
  ["person", { display_name: "合成联系人", person_kind: "student_contact", lifecycle_status: "active" }, ["正式名称", "人员类别", "学生联系人", "有效状态", "有效"]],
  ["login_mapping", { provider_code: "local_test", provider_tenant_key: "synthetic", external_subject: "synthetic-account", status: "confirmed" }, ["登录提供方", "提供方租户", "外部账号标识", "登录关联状态", "已确认"]],
  ["assignment", { assignment_kind: "substantive", position_title: "合成岗位", status: "confirmed" }, ["任职性质", "正式任职", "岗位名称", "任职状态", "已确认"]],
])("labels %s fields and contract enums in default Reader results", async (factKind, fields, labels) => {
  const { default: Result } = await load("DirectoryResult.vue");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Result, { result: { ...result, fact_kind: factKind, records: [{ ...record, fact_kind: factKind, fields }] } });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  for (const label of labels) expect(host.textContent).toContain(label);
  expect(host.textContent).toContain(record.reference);
});

it("localizes descriptor-free F3 maintenance differences while preserving codes and exact references", async () => {
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const before = { person_ref: "person.explicit.original", district_code: "330000", district_name: "合成旧区县", region_purpose: "student_profile" };
  const after = { ...before, district_code: "331000", district_name: "合成新区县" };
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(Proposal, { proposal: { ...proposal, summary: { ...proposal.summary, fact_kind: "person_region", label: "区县资料", before, after } }, complete: true });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick();
  for (const value of ["人员", "区县代码", "区县名称", "地区用途", "学生档案", "合成旧区县", "合成新区县", "330000", "331000", before.person_ref, proposal.summary.reference, "尚未执行"]) expect(host.textContent).toContain(value);
  for (const raw of ["district_code", "district_name", "region_purpose", "student_profile"]) expect(host.textContent).not.toContain(raw);
  expect(host.querySelector("table")?.closest("details")).toBeNull();
});

it("does not translate free text, identifiers, or unrelated enum values as family facts", async () => {
  const { directoryFieldValue } = await load("directory_labels.ts");
  expect(directoryFieldValue("district_code", "student_profile")).toBe("student_profile");
  expect(directoryFieldValue("district_name", "student_profile")).toBe("student_profile");
  expect(directoryFieldValue("verification_source", "manual")).toBe("manual");
  expect(directoryFieldValue("region_purpose", "enrolled")).toBe("enrolled");
  expect(directoryFieldValue("residence_mode", "father")).toBe("father");
});


it("preserves only explicit education dependency conditions without changing F2 or F3 requests", async () => {
  const { createDirectoryApi } = await load("directory_api.ts");
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => result });
  vi.stubGlobal("fetch", fetchMock);
  const input = {fact_kind:"fact_quality",scope:"all_authorized",academic_year:2026,campus_ref:"campus.finite",position_ref:"position.finite",as_of:stamp,untrusted_extra:"not-a-filter"};
  await createDirectoryApi("education").query(input);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({fact_kind:"fact_quality",academic_year:2026,campus_ref:"campus.finite",position_ref:"position.finite",as_of:stamp});
  await createDirectoryApi("education").query({fact_kind:"policy_scope",scope:"all_authorized"});
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({fact_kind:"policy_scope"});
  await createDirectoryApi("directory").query(input);
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({fact_kind:"fact_quality",scope:"all_authorized",as_of:stamp});
  await createDirectoryApi("family").query(input);
  expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({fact_kind:"fact_quality",as_of:stamp});
});
it("shows the complete batch regroup roster without claiming whole-class coverage", async () => {
  const { default: Proposal } = await load("DirectoryProposal.vue");
  const { isDirectoryProposal } = await load("directory_contracts.ts");
  const regroup = {scope:"batch_only",notice:"人数仅统计本批明确名单，未列入的正式名单保持原安排",periods:[{effective_at:stamp,student_count:2,departures:[{student_ref:"学生甲",from_class_ref:"原甲班",to_class_ref:"新甲班",status:"assigned"},{student_ref:"学生乙",from_class_ref:"原甲班",to_class_ref:null,status:"unassigned"}],classes:[{class_ref:"原甲班",label:"原甲班",class_state:"unchanged",outgoing_count:2,incoming_count:0},{class_ref:"新甲班",label:"新甲班",class_state:"recorded",outgoing_count:0,incoming_count:1}],unassigned:["学生乙"],duplicates:[]}]};
  const value={...proposal,summary:{...proposal.summary,regroup}};
  expect(isDirectoryProposal(value)).toBe(true);
  expect(isDirectoryProposal({...value,summary:{...value.summary,regroup:{...regroup,scope:"whole_school"}}})).toBe(false);
  const host=document.createElement("div"); document.body.append(host);
  const app=createApp(Proposal,{proposal:value,complete:true});app.mount(host);cleanups.push(()=>app.unmount());await nextTick();
  expect(host.textContent).toContain("本批名单与去向");
  expect(host.textContent).toContain("学生乙");
  expect(host.textContent).toContain("正式待分班");
  expect(host.textContent).toContain("未列入的正式名单保持原安排");
  expect(host.textContent).toContain("本批进入人数");
  expect(host.textContent).toContain("本批没有更改班状态");
});
it("keeps an explicit F5 cancellation boundary separate from decision time", async()=>{
 const {createDirectoryController}=await load("directory_session.ts");const api=transport();
 const teaching={...record,fact_kind:"teaching_course",reference:"course.ref"};
 api.catalog.mockResolvedValue({families:[{value:"teaching_course",label:"课程"}]});
 api.describe.mockResolvedValue({...descriptor,fact_kind:"teaching_course"});
 api.query.mockResolvedValue({...result,fact_kind:"teaching_course",records:[teaching]});
 const c=createDirectoryController(api);cleanups.push(c.dispose);await c.open();await c.chooseFamily("teaching_course");await c.query({fact_kind:"teaching_course",scope:"all_authorized"});c.edit(teaching.reference);c.setOperation("cancel");c.setReason("取消未来安排");
 expect(await c.prepare()).toBe(false);expect(api.prepare).not.toHaveBeenCalled();
 c.setCancelFrom("2027-09-10T00:00");
 const boundary=new Date("2027-09-10T00:00").toISOString();
 api.prepare.mockResolvedValue({...proposal,summary:{...proposal.summary,reference:teaching.reference,fact_kind:"teaching_course",operation:"correct",reason:"取消未来安排",effective_at:boundary,regroup:{scope:"batch_only",notice:"取消未来安排",periods:[],teaching_cancellations:[{reference:teaching.reference,effective_at:boundary,publication_reference:"publication:exact-original"}]},before_segments:[{effective_at:boundary,valid_to:null,fields:{display_name:"未来课程"}}],after_segments:[]}});
 expect(await c.prepare()).toBe(true);expect(api.prepare.mock.calls[0][0].cancel_from).toBe(boundary);expect(api.prepare.mock.calls[0][0].effective_at).toBeUndefined();
 const accepted=await api.prepare.mock.results[0].value;c.setReason("取消未来安排");
 api.prepare.mockResolvedValue({...accepted,summary:{...accepted.summary,regroup:undefined}});
 expect(await c.prepare()).toBe(false);expect(c.getState().proposal).toBeNull();
});


it("loads the next directory page using the original selection and replaces previously authorized rows", async () => {
  const { createDirectoryController } = await load("directory_session.ts");
  const api = transport(); const controller = createDirectoryController(api); cleanups.push(controller.dispose);
  api.query.mockResolvedValueOnce({...result, matched_count:2, truncated:true, next_cursor:"opaque-page"});
  await controller.query({fact_kind:"person",scope:"all_authorized"});
  const second={...record,reference:"fact.person.2"};
  api.query.mockResolvedValueOnce({...result,records:[second],matched_count:2});
  expect(await controller.loadMore()).toBe(true);
  expect(api.query).toHaveBeenLastCalledWith({fact_kind:"person",scope:"all_authorized",cursor:"opaque-page"});
  expect(controller.getState().result.records).toEqual([second]);
  expect(controller.getState().result.matched_count).toBe(2);
});


it("sends directory continuation coordinates over the real query transport", async () => {
  const { createDirectoryApi } = await load("directory_api.ts");
  const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify(result))); vi.stubGlobal("fetch",fetcher);
  await createDirectoryApi().query({fact_kind:"person",scope:"all_authorized",cursor:"opaque-page",page_size:20});
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({fact_kind:"person",scope:"all_authorized",cursor:"opaque-page",page_size:20});
});
