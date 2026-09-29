import { createApp, nextTick } from "vue";
import { afterEach, expect, it } from "vitest";
import ProductAnswer from "../src/features/school_facts/product_answer.vue";

const staffProfile = { kind: "f3_student_profile", status: "known", profile: { campus: { status: "known", value: "合成教学区" }, region: { status: "no_current_records" } }, sources: { campus: {label:"正式发布"} }, evidence: [], limitations: [], coverage: {status:"unknown",description:"仅当前获权资料"}, tool_context: {person_ref:"private-person-reference"} };
it("preserves the separate enrollment and campus-name sources in combined evidence", async () => {
  const host = await render([{...staffProfile, sources:{campus:{relationship:{label:"合成学籍核验发布",reference:"enrollment-source",published_at:"2026-09-13T00:00:00Z"}, location:{label:"正式地点目录发布",reference:"campus-source",published_at:"2026-09-12T00:00:00Z"}}}}]);
  const card = host.querySelector('[aria-label="学生资料依据"]')!;
  expect(card.textContent).toContain("校区关系来源：合成学籍核验发布");
  expect(card.textContent).toContain("校区名称来源：正式地点目录发布");
  const maintained = card.querySelector("details")!;
  expect(maintained.open).toBe(false);
  expect(maintained.textContent).toContain("campus-source");
  expect(defaultText(host)).not.toContain("campus-source");
});
it("keeps staff combined facts readable with maintenance evidence folded", async () => {
  const host = await render([staffProfile], "在合成教学区，居住地区目前没有有效记录。");
  expect(host.querySelector(".unavailable-card")).toBeNull();
  expect(defaultText(host)).not.toContain("private-person-reference");
  const profile = host.querySelector('[aria-label="学生资料依据"]')!;
  expect(profile.closest("details")?.open).toBe(false);
  expect(profile.textContent).toContain("当前校区：合成教学区");
  expect(profile.textContent).toContain("居住地区：当前没有有效记录");
});
it.each([ ["denied", "无权"], ["conflict", "不一致"], ["ambiguous", "区分"], ["source_unavailable", "无法读取"] ])("keeps combined field %s clear outside details", async (status, label) => {
  const host = await render([{...staffProfile, profile:{region:{status}}, status:"partial"}], "部分资料已取得。");
  expect(outsideDetails(host)).toContain(label);
});

const stamp = "2026-09-12T00:00:00Z";
const source = { label: "正式发布", reference: "source.1", published_at: stamp };
const access = { kind: "fact_access", requested_fact_kind: null, notice: "读取权不代表维护权；授权目标不是资料条数。", queried_at: stamp,
  families: Array.from({ length: 18 }, (_, i) => ({ fact_kind: `family.${i}`, label: `资料类型${i}`, status: "authorized_scope", authorized_target_count: 2 })) };
const fields = { kind: "fact_fields", fact_kind: "organization", label: "组织", fields: [{ name: "display_name", label: "正式名称", type: "text", required: true, reference_kind: null, options: [] }], creation_choices: [] };
it("separates maintenance eligibility evidence from read access and does not claim execution permission", async () => {
  const host = await render([{ ...access, purpose: "maintain", requested_fact_kind: "school",
    notice: "具体修改仍须预检和确认。", families: [{ fact_kind: "school", label: "学校", status: "requires_prepare", target_in_scope: true, field_check: "schema_only", next_step: "prepare" }] }]);
  const evidence = host.querySelector('[aria-label="当前身份维护资格"]')!;
  expect(evidence).not.toBeNull();
  expect(evidence.closest("details")?.open).toBe(false);
  expect(evidence.textContent).toContain("具体修改仍需预检确认");
  expect(host.querySelector('[aria-label="当前身份读取授权范围"]')).toBeNull();
  expect(host.querySelector(".unavailable-card")).toBeNull();
  expect(outsideDetails(host)).toBe("");
});
it.each(["denied", "source_unavailable"])("keeps maintenance %s distinct and visible", async status => {
  const host = await render([{ ...access, purpose: "maintain", requested_fact_kind: "school", families: [{ fact_kind: "school", label: "学校", status }] }]);
  expect(outsideDetails(host)).toContain(status === "denied" ? "学校：未获准维护此次范围" : "学校：维护资格暂时无法核验");
  expect(host.querySelector('[aria-label="当前身份读取授权范围"]')).toBeNull();
});
const original = { kind: "policy_original", reference: "policy.1", version: 3, text: "片段原文", offset: 2, end: 6, length: 10, complete: false, has_more: true, source, notice: "正式原文，区别于模型解释。" };
const record = { reference: "org.1", fact_kind: "organization", label: "综合组", fields: { display_name: "综合组" }, version: 2, valid_from: stamp, valid_to: null, source };
const query = { kind: "directory_facts", status: "known", fact_kind: "organization", records: [record], matched_count: 1, returned_count: 1, truncated: false, next_cursor: null, queried_at: stamp, factual_at: stamp, coverage: { status: "unknown", description: "当前查询范围" } };
const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); document.body.replaceChildren(); });
async function render(facts: unknown[], text = "两个组织分别属于已说明的校区。", options: Record<string, unknown> = {}) {
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(ProductAnswer, { payload: { kind: "assistant_answer", text, facts }, ...options });
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick(); return host;
}
function outsideDetails(host: HTMLElement) {
  return [...host.querySelectorAll(".important-notice")].filter(node => !node.closest("details")).map(node => node.textContent).join(" ");
}

it("keeps access, field guidance and original excerpts in one closed evidence disclosure", async () => {
  const host = await render([access, fields, original, query]);
  const disclosure = host.querySelector("details") as HTMLDetailsElement;
  expect(host.querySelectorAll("details.evidence")).toHaveLength(1);
  expect(disclosure.open).toBe(false);
  expect(host.querySelector('[aria-label="当前身份读取授权范围"]')?.closest("details")).toBe(disclosure);
  expect(disclosure.textContent).toContain("资料类型17");
  expect(disclosure.textContent).toContain("正式名称");
  expect(disclosure.textContent).toContain("片段原文");
  expect(disclosure.textContent).toContain("原文片段");
  expect(disclosure.textContent).toContain("共 10 字");
  expect(host.querySelector(".unavailable-card")).toBeNull();
  disclosure.open = true;
  expect(disclosure.textContent).toContain("读取权不代表维护权");
  expect(disclosure.textContent).toContain("source.1");
});

it("keeps permission overview details folded and aggregates unavailable authority once", async () => {
  const host = await render([{ ...access, families: [
    { fact_kind: "person", label: "人员", status: "denied" },
    { fact_kind: "policy", label: "制度", status: "source_unavailable" },
    { fact_kind: "location", label: "地点", status: "source_unavailable" },
    { fact_kind: "person_contact", label: "个人联系方式", status: "self_only" },
  ] }]);
  expect(outsideDetails(host)).toBe("部分读取权限暂时无法核验，可展开查看详情。");
  for (const label of ["人员", "制度", "地点", "个人联系方式"]) expect(outsideDetails(host)).not.toContain(label);
  expect(host.querySelectorAll(".important-notice")).toHaveLength(1);
  expect(host.querySelector('[aria-label="当前身份读取授权范围"]')?.closest("details")).not.toBeNull();
  for (const detail of ["人员：未取得此类读取授权", "制度：授权来源暂不可核验", "个人联系方式：仅本人联系方式"]) expect(host.textContent).toContain(detail);
});

it.each(["denied", "source_unavailable"])("keeps a specifically requested %s access result visible", async status => {
  const host = await render([{ ...access, requested_fact_kind: "policy", families: [{ fact_kind: "policy", label: "制度", status }] }]);
  expect(outsideDetails(host)).toContain(status === "denied" ? "制度：未取得此类读取授权" : "制度：授权来源暂不可核验");
});

it.each([null, "person_contact"])("treats self-only access as a valid scope without a failure alert (%s)", async requested_fact_kind => {
  const host = await render([{ ...access, requested_fact_kind, families: [{ fact_kind: "person_contact", label: "个人联系方式", status: "self_only" }] }]);
  expect(outsideDetails(host)).toBe("");
  expect(host.querySelector('[aria-label="当前身份读取授权范围"]')?.textContent).toContain("仅本人联系方式");
});

it("preserves legacy access refusals when the old snapshot has no explicit selection", async () => {
  const { requested_fact_kind: _oldSelection, ...legacy } = access;
  const host = await render([{ ...legacy, families: [{ fact_kind: "policy", label: "制度", status: "denied" }] }]);
  expect(outsideDetails(host)).toContain("制度：未取得此类读取授权");
});

it.each([
  ["error", "正式资料来源暂时不可用"],
  ["unavailable", "正式资料来源暂时不可用"],
  ["unsupported_selection", "目前不能按这个条件查询"],
  ["partial", "本次查询尚未完成"],
])("keeps directory %s visible independently of answer wording", async (status, notice) => {
  const host = await render([{ ...query, status, records: [], returned_count: 0, matched_count: null }]);
  expect(outsideDetails(host)).toContain(notice);
});

it("keeps incomplete record and quality evidence visible", async () => {
  const host = await render([{ ...query, truncated: true, quality: {
    existence: "known", coverage: "partial", source_availability: "available", freshness: "unknown", conflict: "unknown", evidence_status: "known", evidence_truncated: true, notice: "仅本次范围", statements: [],
  } }]);
  expect(outsideDetails(host)).toContain("查询结果未全部显示");
  expect(outsideDetails(host)).toContain("没有查全核验记录");
});

it("tells a teacher when a known answer covers only part of the requested set", async () => {
  const host = await render([{ ...query, coverage: {
    status: "partial", description: "已发布的部分校区组织",
  } }], "综合组位于合成甲校区。");
  const ordinary = defaultText(host);
  expect(ordinary).toContain("综合组位于合成甲校区");
  expect(ordinary).toContain("只查到部分资料");
  expect(ordinary).not.toContain("质量声明");
  expect(ordinary).not.toContain("org.1");
});

it("uses plain but distinct wording for a declared source outage and failed evidence read", async () => {
  const base = {
    existence: "known", coverage: "unknown", source_availability: "unavailable",
    freshness: "unknown", conflict: "unknown", evidence_status: "known",
    evidence_truncated: false, notice: "来源状态需核对", statements: [],
  };
  const declared = await render([{ ...query, quality: base }], "已查到综合组。");
  expect(defaultText(declared)).toContain("原来源目前不可用");
  expect(defaultText(declared)).not.toContain("正式质量声明记载");
  expect(defaultText(declared)).not.toContain("整项资料无法读取");
  const failed = await render([{ ...query, quality: {
    ...base, source_availability: "unknown", evidence_status: "source_unavailable",
  } }], "已查到综合组。");
  expect(defaultText(failed)).toContain("本次无法核对资料依据");
  expect(defaultText(failed)).not.toContain("原来源目前不可用");
});

it("keeps conflict, refusal and incomplete evidence understandable outside technical details", async () => {
  const quality = {
    existence: "known", coverage: "partial", source_availability: "available",
    freshness: "review_overdue", conflict: "contradictory_evidence", evidence_status: "denied",
    evidence_truncated: true, notice: "内部质量说明", statements: [],
  };
  const host = await render([
    { ...query, quality },
    { ...query, status: "denied", records: [], returned_count: 0, matched_count: null },
  ], "已查到综合组。");
  const ordinary = defaultText(host);
  expect(ordinary).toContain("有相互矛盾的记录");
  expect(ordinary).toContain("当前身份无权");
  expect(ordinary).toContain("没有查全核验记录");
  expect(ordinary).not.toContain("质量声明");
  expect(ordinary).not.toContain("org.1");
  expect(host.querySelector("details.evidence")?.textContent).toContain("内部质量说明");
});

it("keeps compact candidate navigation, proposal and receipt outside evidence", async () => {
  const proposal = { kind: "fact_proposal", proposal_ref: "proposal.1", summary: { label: "综合组", reference: "org.1", fact_kind: "organization", operation: "upsert", before: { display_name: "综合组" }, after: { display_name: "综合事务组" }, effective_at: stamp, reason: "正式核对", source_label: "正式维护" }, expires_at: "2099-01-01T00:00:00Z" };
  const receipt = { reference: "receipt.1", status: "unknown", message: "原操作结果尚待核对", target_version: null, changed_count: null, request_id: "request.1" };
  const host = await render([access, { ...query, status: "ambiguous" }, proposal, receipt]);
  const selection = host.querySelector(".directory-result");
  expect(selection?.closest("details")).not.toBeNull();
  expect(host.querySelector('[aria-label="候选对象入口"]')?.closest("details")).toBeNull();
  expect(host.querySelector('[data-action="review-directory-proposal"]')?.closest("details")).toBeNull();
  const receiptCard = host.querySelector('[aria-label="正式操作回执"]');
  expect(receiptCard?.closest("details")).toBeNull();
  expect(receiptCard?.textContent).toContain("原操作结果尚待核对");
});

it("keeps invalid or unknown evidence as a visible safe fallback", async () => {
  const host = await render([{ ...original, end: 9 }, { kind: "unrecognized", secret: "do-not-render" }]);
  expect(host.querySelectorAll(".unavailable-card")).toHaveLength(2);
  for (const node of host.querySelectorAll(".unavailable-card")) expect(node.closest("details")).toBeNull();
  expect(host.textContent).not.toContain("do-not-render");
  expect(host.textContent).not.toContain("片段原文");
});

it("renders paragraphs, emphasis and short lists without changing the text meaning", async () => {
  const host = await render([], "已找到**正式邮箱**，请*核对对象*。\n\n- 第一项\n- **第二项**\n\n1. 联系办公室\n2. 说明来意");
  const answer = host.querySelector(".answer-text")!;
  expect(answer.querySelector("strong")?.textContent).toBe("正式邮箱");
  expect(answer.querySelector("em")?.textContent).toBe("核对对象");
  expect(answer.querySelectorAll("ul > li")).toHaveLength(2);
  expect(answer.querySelectorAll("ol > li")).toHaveLength(2);
  expect(answer.querySelectorAll("p")).toHaveLength(1);
  expect(answer.textContent).not.toContain("**");
});

it("keeps HTML, links, unmatched markers and inline code literal", async () => {
  const text = '<img src=x onerror="alert(1)"><script>alert(1)</script>\n**安全文字**和未闭合*\n[查看](javascript:alert(1)) 与 `**原样**`';
  const host = await render([], text);
  const answer = host.querySelector(".answer-text")!;
  expect(answer.querySelectorAll("img,script,a,iframe")).toHaveLength(0);
  expect(answer.querySelector("strong")?.textContent).toBe("安全文字");
  expect(answer.textContent).toContain('<img src=x onerror="alert(1)">');
  expect(answer.textContent).toContain("未闭合*");
  expect(answer.textContent).toContain("[查看](javascript:alert(1))");
  expect(answer.textContent).toContain("`**原样**`");
});

it("renders a pipe table as safe cells while preserving escaped and inline-code pipes", async () => {
  const host = await render([], "已核对两项服务。\n\n| 服务 | 联系方式 |\n| :--- | ---: |\n| **接待** | 办公室\\|值班室 |\n| 查询 | `编号|原文` |\n\n1. 核对来源");
  const answer = host.querySelector(".answer-text")!;
  expect([...answer.querySelectorAll("thead th")].map(cell => cell.textContent)).toEqual(["服务", "联系方式"]);
  expect([...answer.querySelectorAll("tbody td")].map(cell => cell.textContent)).toEqual(["接待", "办公室|值班室", "查询", "`编号|原文`"]);
  expect(answer.querySelector("tbody strong")?.textContent).toBe("接待");
  expect(answer.textContent).not.toContain(":---");
  expect(answer.querySelector("ol > li")?.textContent).toBe("核对来源");
});

it.each([
  "| 服务 | 渠道 |\n| 接待 | 电话 |",
  "| 服务 | 渠道 |\n| --- | --- |\n| 接待 | 电话 | 额外事实 |",
  "| 服务 | 渠道 |\n| --- | --- |\n| 缺少渠道 |",
  "```text\n| 服务 | 渠道 |\n| --- | --- |\n| 接待 | 电话 |\n```",
])("keeps unsupported or inconsistent table text intact: %s", async text => {
  const host = await render([], text);
  const answer = host.querySelector(".answer-text")!;
  expect(answer.querySelector("table")).toBeNull();
  expect(answer.textContent).toBe(text);
});

it("does not execute HTML or links inside table cells", async () => {
  const host = await render([], '| 对象 | 说明 |\n| --- | --- |\n| <img src=x onerror="alert(1)"> | [打开](javascript:alert(1)) |');
  const answer = host.querySelector(".answer-text")!;
  expect(answer.querySelectorAll("tbody td")).toHaveLength(2);
  expect(answer.querySelectorAll("img,script,a,iframe")).toHaveLength(0);
  expect(answer.textContent).toContain('<img src=x onerror="alert(1)">');
  expect(answer.textContent).toContain("[打开](javascript:alert(1))");
});


it.each(["error", "unavailable", "unsupported_selection"])("keeps an empty bundle %s visible", async status => {
  const host = await render([{ kind: "directory_bundle", status, subject_label: "组织关系", results: [] }]);
  expect(outsideDetails(host)).toContain(status === "unsupported_selection" ? "目前不能按这个条件查询" : "正式资料来源暂时不可用");
});

it("keeps full-original validation strict while accepting exact Unicode segments", async () => {
  const { isDirectoryOriginal, isDirectoryOriginalSegment } = await import("../src/features/school_facts/directory_contracts");
  const segment = { ...original, text: "😀原文", offset: 1, end: 4, length: 6 };
  expect(isDirectoryOriginalSegment(segment)).toBe(true);
  expect(isDirectoryOriginal(segment)).toBe(false);
  for (const invalid of [{ ...segment, end: 5 }, { ...segment, complete: true }, { ...segment, has_more: false }, { ...segment, offset: -1 }]) {
    expect(isDirectoryOriginalSegment(invalid)).toBe(false);
  }
  expect(isDirectoryOriginal({ ...segment, offset: 0, end: 3, length: 3, complete: true, has_more: false })).toBe(true);
});

it("does not invent a reversed range for an empty original segment", async () => {
  const host = await render([{ ...original, text: "", offset: 10, end: 10, has_more: false }]);
  expect(host.querySelector('[aria-label="正式原文依据"]')?.textContent).toContain("本次原文片段没有文字");
  expect(host.textContent).not.toContain("第 11 至 10 字");
});


function defaultText(host: HTMLElement) {
  const visible = host.cloneNode(true) as HTMLElement;
  visible.querySelectorAll("details").forEach(node => node.remove());
  return visible.textContent ?? "";
}

it("keeps ordinary evidence readable while exposing complete record details only on demand", async () => {
  const technical = { ...record, fact_kind: "person", label: "人员 · 合成学生", fields: { display_name: "合成学生", business_code: "PERSON-PRIVATE-CODE", person_kind: "student" }, source: { ...source, reference: "publication:private-trace" } };
  const host = await render([{ ...query, fact_kind: "person", records: [technical] }], "已核对姓名。", { selectable: true });
  const evidence = host.querySelector("details") as HTMLDetailsElement;
  evidence.open = true;
  const result = host.querySelector(".directory-result") as HTMLElement;
  expect(defaultText(result)).toContain("人员 · 合成学生");
  expect(defaultText(result)).toContain("正式发布");
  for (const text of ["PERSON-PRIVATE-CODE", "publication:private-trace", "org.1", "对象引用", "版本", "查询时间", stamp, "业务编码", "关联事实"]) expect(defaultText(result)).not.toContain(text);
  expect(result.querySelector("button")).toBeNull();
  const details = result.querySelector("details") as HTMLDetailsElement;
  expect(details).not.toBeNull();
  expect(details.querySelector("summary")?.textContent).toBe("维护核查信息");
  expect(details.open).toBe(false);
  details.open = true;
  for (const text of ["PERSON-PRIVATE-CODE", "publication:private-trace", "org.1", "版本 2", "学生"]) expect(details.textContent).toContain(text);
});

it.each([
  ["forbidden", "当前身份无权查询此范围"],
  ["conflict", "正式资料存在不一致，需核验"],
])("keeps directory %s risks visible outside folded evidence", async (status, notice) => {
  const host = await render([{ ...query, status, records: [], matched_count: null, returned_count: 0 }]);
  expect(outsideDetails(host)).toContain(notice);
  expect(outsideDetails(host)).not.toContain("尚未录入");
  expect(defaultText(host.querySelector(".directory-result") as HTMLElement)).toContain(notice);
});

it.each([
  ["object_unresolved", "当前获准资料中尚未明确查询对象"],
  ["no_current_records", "当前查询范围内没有可提供的有效记录"],
])("keeps a directory %s outcome with its own evidence, not as a final task instruction", async (status, notice) => {
  for (const text of ["已查到老师。", "还需要你提供校区。", "当前条件下没有查到。"] ) {
    const empty = { ...query, status, records: [], matched_count: null, returned_count: 0 };
    const host = await render([empty, query], text);
    expect(outsideDetails(host)).toBe("");
    expect(host.querySelector(".answer-text")?.textContent).toBe(text);
    const evidence = host.querySelector(".directory-result")!;
    expect(evidence.textContent).toContain(notice);
    expect(evidence.closest("details")?.open).toBe(false);
    expect(host.querySelectorAll(".directory-result")).toHaveLength(2);
  }
});

it.each(["ambiguous", "object_unresolved", "no_current_records", "not_recorded"])(
  "keeps F3 %s profile and limitation outcomes in their original evidence", async status => {
    const host = await render([{ ...staffProfile, status, profile: {}, limitations: [{ status }], candidates: [{ name: "同名学生", student_number: "S01" }] }, staffProfile], "请按这次问题继续核对。");
    expect(outsideDetails(host)).toBe("");
    const evidence = host.querySelector('[aria-label="学生资料依据"]')!;
    expect(evidence.closest("details")?.open).toBe(false);
    expect(evidence.querySelector('[data-role="query-outcome"]')?.textContent).toContain("该次查询");
    expect(evidence.textContent).toContain("同名学生");
    expect(evidence.textContent).toContain("S01");
    expect(host.querySelectorAll('[data-role="query-limitation"]')).toHaveLength(1);
  }
);

it("retains F3 field ambiguity, truncation and real risks even alongside a successful result", async () => {
  const host = await render([{ ...staffProfile, status: "ambiguous", profile: { region: { status: "conflict" } }, limitations: [{ status: "ambiguous", truncated: true }, { status: "source_unavailable" }] }, staffProfile]);
  expect(outsideDetails(host)).toContain("居住地区：正式资料存在不一致");
  expect(outsideDetails(host)).toContain("候选未完整返回");
  expect(outsideDetails(host)).toContain("正式资料暂时无法读取");
  expect(outsideDetails(host)).not.toContain("存在多项匹配，需要进一步区分");
});

const contactQuery = {
  status: "no_current_records", display_notice: null, items: [], candidates: [],
  query: { subject: "official_contacts", scope: "selected", campus_name: "甲校区", campus_ref: null },
  resolved_campuses: [{ reference: "campus.1", label: "甲校区", path: "甲校区" }],
  listing: { entity: "contacts", returned_count: 0, matched_count: null, truncated: false },
  coverage: { status: "unknown", description: "仅该次所选校区" }, queried_at: stamp,
};
it.each(["no_current_records", "not_recorded", "unmatched"])(
  "keeps F1 %s query outcomes in evidence without losing the original status", async status => {
    const fact = { ...contactQuery, status, resolved_campuses: status === "unmatched" ? [] : contactQuery.resolved_campuses };
    const host = await render([fact], "已核对你要问的对象。");
    expect(host.querySelector(".unavailable-card")).toBeNull();
    expect(outsideDetails(host)).toBe("");
    const evidence = host.querySelector(".contact-result")!;
    expect(evidence.closest("details")?.open).toBe(false);
    expect(evidence.querySelector(".result-status")?.textContent).toBeTruthy();
    expect(evidence.textContent).toContain("甲校区");
  }
);

it.each(["ambiguous", "not_recorded"])("preserves the existing F4 %s evidence without adding a final task alert", async status => {
  const text = status === "ambiguous" ? "该次查询有多个符合条件的班级。" : "该次条件下未查到班级。";
  const host = await render([{ family_owner: "school_facts.education", status, text, evidence: [] }], "请告诉我校区。");
  expect(outsideDetails(host)).toBe("");
  expect(host.querySelector(".answer-text")?.textContent).toBe("请告诉我校区。");
  const evidence = host.querySelector('[aria-label="班级与教育资料依据"]')!;
  expect(evidence.closest("details")?.open).toBe(false);
  expect(evidence.textContent).toContain(text);
});

it("keeps the pending object and review action visible while folding its tracking reference", async () => {
  const proposal = { kind: "fact_proposal", proposal_ref: "proposal.private.1", summary: { label: "接待电话", reference: "contact.private.1", fact_kind: "official_contact", operation: "upsert", before: { contact_value: "0571-00000001" }, after: { contact_value: "0571-10000002" }, effective_at: stamp, reason: "核实电话", source_label: "正式维护" }, expires_at: "2099-01-01T00:00:00Z" };
  const onReviewDirectory = (...args: unknown[]) => selected.push(args);
  const selected: unknown[][] = [];
  const host = await render([proposal], "请核对修改。", { onReviewDirectory });
  for (const text of ["接待电话", "发布或更新", "核实电话", "正式维护", "尚未执行", "核对完整修改"]) expect(defaultText(host)).toContain(text);
  expect(defaultText(host)).not.toContain("contact.private.1");
  const details = host.querySelector(".proposal-card details") as HTMLDetailsElement;
  expect(details.open).toBe(false);
  expect(details.textContent).toContain("contact.private.1");
  (host.querySelector('[data-action="review-directory-proposal"]') as HTMLButtonElement).click();
  expect(selected).toEqual([[proposal.proposal_ref]]);
});

it("uses the same readable pending summary for the contact proposal path", async () => {
  const before = { reference: "contact.private.1", campus_ref: "campus.private.1", campus_name: "东校区", contact_kind: "phone", contact_value: "0571-00000001", service_hours: "工作日", valid_from: stamp, valid_to: null, version: 2, source };
  const proposal = { proposal_ref: "proposal.private.1", summary: { before, after: { contact_value: "0571-10000002", service_hours: "工作日" }, reason: "核实电话" }, expires_at: "2099-01-01T00:00:00Z" };
  const host = await render([proposal], "请核对修改。");
  const visible = defaultText(host);
  for (const text of ["东校区 · 电话", before.contact_value, proposal.summary.after.contact_value, "核实电话", "尚未执行", "正式确认控件"]) expect(visible).toContain(text);
  expect(visible).not.toContain("phone");
  expect(visible).not.toContain(before.reference);
  const details = host.querySelector('[aria-label="待确认修改"] details') as HTMLDetailsElement;
  expect(details.open).toBe(false);
  expect(details.textContent).toContain(before.reference);
});

it.each([
  ["succeeded", "已完成", 1, 10],
  ["rejected", "未执行", 0, null],
  ["unknown", "结果待核对", null, null],
] as const)("keeps %s receipt status visible and tracking details folded in both answer paths", async (status, label, changed_count, target_version) => {
  const receipt = { reference: "receipt.private.1", status, message: "正式操作状态说明", changed_count, target_version, request_id: "request.private.1" };
  for (const options of [{}, { payload: receipt }]) {
    const host = await render([receipt], "", options);
    expect(defaultText(host)).toContain(label);
    expect(defaultText(host)).toContain(receipt.message);
    if (changed_count !== null) expect(defaultText(host)).toContain(`实际变更 ${changed_count} 项`);
    if (target_version !== null) { expect(defaultText(host)).not.toContain(`学校事实总版本 ${target_version}`); expect(host.querySelector("details")?.textContent).toContain(`学校事实总版本 ${target_version}`); }
    for (const reference of [receipt.reference, receipt.request_id]) expect(defaultText(host)).not.toContain(reference);
    const details = host.querySelector('[aria-label="正式操作回执"] details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain(receipt.reference);
    expect(details.textContent).toContain(receipt.request_id);
  }
});
const ambiguous = { ...query, status: "ambiguous", records: [
  { ...record, label: "内部/路径/一", fields: { display_name: "综合组", display_path: "内部/路径/一" } },
  { ...record, reference: "org.2", label: "内部/路径/二", fields: { display_path: "内部/路径/二" } },
], returned_count: 2, matched_count: 2 };

it("keeps ordinary ambiguous queries neutral, compact and bound to their original reference", async () => {
  const selected: unknown[][] = [];
  const host = await render([ambiguous], "已有比较结果。", { selectable: true,
    onRelatedDirectory: (...args: unknown[]) => selected.push(args) });
  const navigation = host.querySelector('[aria-label="候选对象入口"]')!;
  expect(navigation).not.toBeNull();
  expect(defaultText(host)).toContain("可按需选择候选对象继续查询");
  expect(defaultText(host)).toContain("综合组");
  expect(defaultText(host)).toContain("候选对象2（名称未提供）");
  for (const hidden of ["请明确对象", "定位待补", "org.1", "org.2", "内部/路径", "查询时间", "对象引用"]) expect(defaultText(host)).not.toContain(hidden);
  const buttons = navigation.querySelectorAll("button");
  expect(buttons).toHaveLength(2);
  (buttons[1] as HTMLButtonElement).click(); await nextTick();
  expect(selected).toEqual([[ambiguous, "org.2"]]);
  expect(host.querySelector("details")?.textContent).toContain("org.2");
  expect(host.querySelector("details")?.textContent).toContain("内部/路径/二");
  expect(host.querySelector("details")?.textContent).not.toContain("请明确对象");
});

it("folds only same-bundle resolution evidence and preserves requested risks", async () => {
  const requested = { ...query, fact_kind: "organization_profile", status: "denied", records: [], returned_count: 0, matched_count: null };
  const host = await render([{ kind: "directory_bundle", status: "denied", subject_label: "综合组", subject_reference: "org.1", requested_fact_kinds: ["organization_profile"], query_completion: "incomplete", results: [ambiguous, requested] }]);
  expect(host.querySelector('[aria-label="候选对象入口"]')).toBeNull();
  expect(outsideDetails(host)).toContain("当前身份无权查询此范围");
  expect(host.querySelector("details")?.textContent).toContain("org.1");
  expect(host.querySelector("details")?.textContent).toContain("org.2");
  expect(host.querySelector("details")?.textContent).not.toContain("请明确对象");
});

it("marks only an explicitly unresolved lookup target as pending and keeps candidate subset", async () => {
  const host = await render([{ kind: "directory_bundle", status: "ambiguous", subject_label: "综合组", requested_fact_kinds: ["organization_profile"], query_completion: "subject_unresolved", candidates: [ambiguous.records[1]], results: [ambiguous] }], "已经回答。", { selectable: true });
  const navigation = host.querySelector('[aria-label="候选对象入口"]')!;
  expect(defaultText(host)).toContain("查询对象尚待定位");
  expect(navigation.querySelectorAll("button")).toHaveLength(1);
  expect(defaultText(host)).toContain("候选对象1（名称未提供）");
  expect(defaultText(host)).not.toContain("内部/路径");
});

it.each([
  { subject_reference: "org.absent", requested_fact_kinds: ["organization_profile"], query_completion: "completed" },
  { subject_reference: "org.1", requested_fact_kinds: ["organization"], query_completion: "completed" },
  { subject_reference: "org.1", requested_fact_kinds: [], query_completion: "completed" },
  { subject_reference: "org.1", requested_fact_kinds: ["organization_profile"], query_completion: "subject_unresolved" },
])("keeps neutral candidates when bundle resolution metadata is insufficient: %j", async metadata => {
  const host = await render([{ kind: "directory_bundle", status: "known", subject_label: "综合组", results: [ambiguous], ...metadata }]);
  expect(host.querySelector('[aria-label="候选对象入口"]')).not.toBeNull();
});

it("never consumes earlier ambiguity from answer wording or a later unrelated record", async () => {
  const later = { ...query, records: [ambiguous.records[0]], factual_at: "2026-09-13T00:00:00Z" };
  const host = await render([ambiguous, later], "两个候选都已核对，任务完成。", { selectable: true, disabled: true });
  expect(host.querySelector('[aria-label="候选对象入口"]')).not.toBeNull();
  expect([...host.querySelectorAll('[aria-label="候选对象入口"] button')].every(button => (button as HTMLButtonElement).disabled)).toBe(true);
});

it("keeps candidate truncation and quality failures visible outside evidence", async () => {
  const host = await render([{ ...ambiguous, truncated: true, quality: { existence: "known", coverage: "partial", source_availability: "available", freshness: "unknown", conflict: "open", evidence_status: "source_unavailable", evidence_truncated: true, notice: "质量待核对", statements: [] } }]);
  expect(outsideDetails(host)).toContain("查询结果未全部显示");
  expect(outsideDetails(host)).toContain("本次无法核对资料依据");
  expect(outsideDetails(host)).toContain("有待处理的争议");
});


it.each([0, 1])("does not assert multiple candidates when selection_required returns %i", async count => {
  const host = await render([{ ...query, status: "selection_required", records: query.records.slice(0, count), returned_count: count, matched_count: count }], "请核对查询。", { selectable: true });
  const navigation = host.querySelector('[aria-label="候选对象入口"]')!;
  expect(navigation).not.toBeNull();
  expect(navigation.textContent).not.toContain("多个");
  expect(navigation.querySelectorAll("button")).toHaveLength(count);
  if (count === 0) expect(navigation.textContent).toContain("没有可展示的候选对象");
});

const neutralQuality = { existence: "known", coverage: "unknown", source_availability: "available", freshness: "unknown", conflict: "unknown", evidence_status: "known", evidence_truncated: false, notice: "质量范围依据", statements: [] };
it.each([
  { coverage: { status: "partial", description: "仅部分获准范围" } },
  { quality: { ...neutralQuality, coverage: "partial" } },
])("keeps partial candidate coverage visible even without truncation or other risks: %j", async coverage => {
  const candidate = { ...ambiguous, ...coverage };
  const host = await render([candidate]);
  expect(outsideDetails(host)).toContain("只查到部分候选资料");
  expect(host.querySelector("details")?.textContent).toContain("org.1");
});

it.each(["partial", "conflicting_statements"])("keeps %s coverage from resolved candidate evidence visible", async coverage => {
  const candidate = { ...ambiguous, quality: { ...neutralQuality, coverage } };
  const host = await render([{ kind: "directory_bundle", status: "known", subject_label: "综合组", subject_reference: "org.1", requested_fact_kinds: ["organization_profile"], query_completion: "completed", results: [candidate] }]);
  expect(host.querySelector('[aria-label="候选对象入口"]')).toBeNull();
  expect(outsideDetails(host)).toContain(coverage === "partial" ? "只查到部分候选资料" : "资料范围说法不一致");
});

it("keeps conflicting candidate coverage visible without a separate conflict marker", async () => {
  const host = await render([{ ...ambiguous, quality: { ...neutralQuality, coverage: "conflicting_statements" } }]);
  expect(outsideDetails(host)).toContain("资料范围说法不一致");
});

it("does not turn unknown candidate coverage into an alert", async () => {
  const host = await render([{ ...ambiguous, quality: neutralQuality }]);
  expect(outsideDetails(host)).toBe("");
});

it("keeps an optional history notice in closed conversation details even without facts", async () => {
  const notice = '较早的对话内容当前不可复用，请按需重新核对。<img src=x> **原样文字**';
  const host = await render([], "当前回答。", { payload: {
    kind: "assistant_answer", text: "当前回答。", facts: [],
    history_notice: notice, history_retention: "candidate-history-internal-code",
  } });
  const displayed = host.querySelector(".history-notice");
  expect(displayed?.textContent).toBe(notice);
  const details = displayed?.closest("details") as HTMLDetailsElement;
  expect(details).not.toBeNull();
  expect(details.open).toBe(false);
  expect(details.querySelector("summary")?.textContent).toBe("对话说明");
  expect(defaultText(host)).not.toContain(notice);
  details.open = true;
  expect(details.textContent).toContain(notice);
  expect(displayed?.querySelectorAll("img,strong,script,a")).toHaveLength(0);
  expect(host.textContent).not.toContain("candidate-history-internal-code");
  expect(host.querySelector(".answer-text")?.textContent).toBe("当前回答。");
});

it.each([
  ["F1", { reference: "contact.1", campus_ref: "campus.1", campus_name: "东校区", contact_kind: "phone", contact_value: "0571-00000001", service_hours: null, valid_from: stamp, valid_to: null, version: 1, source }],
  ["F2", query],
  ["F3", staffProfile],
  ["F4", { family_owner: "school_facts.education", status: "known", name: "当前班级", text: "查询到所选班级", evidence: [] }],
  ["F5", { family_owner: "school_facts.teaching", status: "available", items: [], coverage: "unknown" }],
])("separates %s answer and fact evidence from conversation recovery details", async (_stage, fact) => {
  const notice = "后续问题可能需要重新核对资料。";
  const host = await render([], "这次问题的答案。", { payload: {
    kind: "assistant_answer", text: "这次问题的答案。", facts: [fact], history_notice: notice,
  } });
  expect(host.querySelector(".unavailable-card")).toBeNull();
  expect(host.querySelector(".answer-text")?.textContent).toBe("这次问题的答案。");
  expect(defaultText(host)).not.toContain(notice);
  const explanation = host.querySelector(".history-notice")?.closest("details") as HTMLDetailsElement;
  const evidence = host.querySelector("details.evidence") as HTMLDetailsElement;
  expect(explanation.open).toBe(false);
  expect(evidence.open).toBe(false);
  expect(explanation).not.toBe(evidence);
  expect(evidence.textContent).not.toContain(notice);
});

it("labels visible system risks separately without folding them with recovery information", async () => {
  const risks = { family_owner: "school_facts.teaching", status: "denied", participation: "休学期间保留的教学关联不能证明实际参加课程。", items: [{ notices: ["课程适用年级不一致，需要学校核对。"] }], quality: [{ dispute_status: "open", source_status: "unavailable" }] };
  const host = await render([], "这次查询没有完成。", { payload: {
    kind: "assistant_answer", text: "这次查询没有完成。", facts: [risks],
    history_notice: "后续问题可能需要重新核对资料。",
  } });
  const system = host.querySelector('[aria-label="系统提示"]');
  expect(system).not.toBeNull();
  expect(system?.closest("details")).toBeNull();
  expect(system?.querySelector(".system-notice-label")?.textContent).toBe("系统提示");
  const notices = [...system!.querySelectorAll('[role="alert"]')];
  expect(notices.length).toBe(5);
  for (const text of ["教学查询 1", "当前身份无权", "休学期间", "年级不一致", "尚未解决的争议", "原来源目前不可用"]) {
    expect(system?.textContent).toContain(text);
    expect(defaultText(host)).toContain(text);
  }
  expect(host.querySelector(".answer-text")?.textContent).toBe("这次查询没有完成。");
  expect(system?.textContent).not.toContain("后续问题可能");
});

it("keeps an actual failure answer visible and creates no risk label for recovery alone", async () => {
  const host = await render([], "本轮处理未完成，请重新核对身份。", { payload: {
    kind: "assistant_answer", text: "本轮处理未完成，请重新核对身份。", error_code: "assistant.authentication_required", facts: [],
    history_notice: "后续问题可能需要重新核对资料。",
  } });
  expect(defaultText(host)).toContain("本轮处理未完成，请重新核对身份。");
  expect(host.querySelector('[aria-label="系统提示"]')).toBeNull();
  expect(host.querySelector(".answer-text")?.closest("details")).toBeNull();
});

it.each([undefined, null, "", " \n\t ", 7, false, {}, []])(
  "keeps the existing output unchanged for an absent, blank or invalid history notice: %j",
  async notice => {
    const baseline = await render([], "当前回答。");
    const host = await render([], "当前回答。", { payload: {
      kind: "assistant_answer", text: "当前回答。", facts: [],
      history_notice: notice, history_retention: "candidate-history-internal-code",
    } });
    expect(host.querySelector(".history-notice")).toBeNull();
    expect(host.innerHTML).toBe(baseline.innerHTML);
  },
);


it("shows own historical receipts as read-only evidence with exact refs folded", async () => {
  const reference = "sf2receipt_" + "a".repeat(64);
  const item = { kind: "own_fact_receipt", reference, status: "succeeded", message: "原修改已完成；本次只读核对。", target_version: 13, changed_count: 1, request_id: null, confirmed_at: stamp,
    targets: [{ fact_kind: "official_contact", label: "官方联系方式", reference: "f2:official_contact:" + "b".repeat(64), operation: "upsert", target_version: 13 }] };
  const host = await render([{ kind: "own_fact_receipts", items: [item], has_more: true, notice: "仅当前可读的本人最近回执，不代表全部历史。" }], "查到一笔原修改回执。");
  expect(host.querySelector(".unavailable-card")).toBeNull();
  const history = host.querySelector('[data-role="receipt-history-evidence"]') as HTMLDetailsElement;
  expect(history).not.toBeNull();
  expect(history.open).toBe(false);
  expect(history.querySelector("summary")?.textContent).toBe("维护核查信息：本人历史回执");
  history.open = true;
  const section = host.querySelector('[aria-label="本人历史正式回执"]')!;
  expect(section.textContent).toContain("官方联系方式");
  expect(section.textContent).toContain("还有更早的可读回执");
  expect(section.textContent).toContain("不代表全部历史");
  expect(section.querySelector("button")).toBeNull();
  const details = section.querySelector("details")!;
  expect(details.open).toBe(false);
  expect(details.textContent).toContain(reference);
  const visibleClone = section.cloneNode(true) as HTMLElement;
  visibleClone.querySelectorAll("details").forEach(node => node.remove());
  expect(visibleClone.textContent).not.toContain("sf2receipt_");
  expect(visibleClone.textContent).not.toContain("official_contact");
});

const historicalItem = { kind: "own_fact_receipt", reference: "sf2receipt_" + "a".repeat(64), status: "succeeded", message: "原修改已完成；本次只读核对。", target_version: 13, changed_count: 1, request_id: null, confirmed_at: stamp,
  targets: [{ fact_kind: "person_region", label: "历史区县资料", reference: "f3:person_region:" + "b".repeat(64), operation: "upsert", target_version: 2 }] };
const historicalPage = { kind: "own_fact_receipts", items: Array.from({ length: 10 }, (_, index) => ({ ...historicalItem, reference: "sf2receipt_" + index.toString(16).padStart(64, "a") })), has_more: true, notice: "仅本次可读历史，不代表全部操作。" };

it("keeps single recovered receipt trace and technical boundary in maintenance details", async () => {
  const host = await render([historicalItem], "原修改已成功。");
  expect(defaultText(host)).toContain("已完成");
  expect(defaultText(host)).not.toContain("学校事实总版本");
  expect(defaultText(host)).not.toContain(historicalItem.confirmed_at);
  expect(defaultText(host)).not.toContain(historicalItem.message);
  const details = host.querySelector('[aria-label="正式操作回执"] details') as HTMLDetailsElement;
  expect(details.open).toBe(false);
  for(const value of [historicalItem.message,historicalItem.confirmed_at,"学校事实总版本 13",historicalItem.reference]) expect(details.textContent).toContain(value);
});

it("folds two intermediate history pages without replacing the final concise answer or losing their records", async () => {
  const text = "15:42 的原修改已成功。";
  const host = await render([historicalPage, { ...historicalPage, has_more: false }], text);
  expect(defaultText(host)).toBe(text);
  const history = host.querySelector('[data-role="receipt-history-evidence"]') as HTMLDetailsElement;
  expect(history).not.toBeNull();
  expect(history.open).toBe(false);
  expect(history.querySelectorAll('[aria-label="本人历史正式回执"]')).toHaveLength(2);
  expect(history.querySelectorAll("article")).toHaveLength(20);
  history.open = true;
  for (const value of [historicalPage.notice, historicalItem.targets[0].label, stamp, "学校事实总版本 13", "还有更早的可读回执"]) expect(history.textContent).toContain(value);
  expect(history.querySelector("button")).toBeNull();
});

it.each([
  { status: "succeeded", label: "已完成", target_version: 14, changed_count: 1 },
  { status: "rejected", label: "未执行", target_version: null, changed_count: null },
  { status: "unknown", label: "结果待核对", target_version: null, changed_count: null },
])("keeps a single $status official receipt visible beside folded historical pages", async receipt => {
  const item = { ...receipt, reference: "proposal.current", request_id: "request.current", message: "本次正式操作结果说明" };
  const host = await render([historicalPage, item], "请核对原操作结果。");
  const displayed = host.querySelector('[aria-label="正式操作回执"]')!;
  expect(displayed.closest("details")).toBeNull();
  expect(defaultText(host)).toContain(receipt.label);
  expect(defaultText(host)).toContain(item.message);
  expect(defaultText(host)).not.toContain("历史区县资料");
});

it("retains a requested readable history list and a single exact historical result outside page evidence", async () => {
  const answer = "已查到一笔原修改：区县资料已更新。可展开核查历史清单。";
  const host = await render([historicalPage, historicalItem], answer);
  expect(defaultText(host)).toContain(answer);
  const result = host.querySelector('[aria-label="正式操作回执"]')!;
  expect(result.closest("details")).toBeNull();
  expect(result.textContent).toContain(historicalItem.message);
  expect(result.textContent).toContain("历史区县资料");
});

it("never renders malformed or uncertain history entries as successful receipts", async () => {
  const host = await render([{ kind: "own_fact_receipts", items: [{status: "unknown"}], has_more: false, notice: "" }]);
  expect(host.querySelector('[aria-label="本人历史正式回执"]')).toBeNull();
  expect(host.querySelector(".unavailable-card")).not.toBeNull();
});


it("rejects malformed single historical receipts without changing legacy receipts", async () => {
  const malformed = { kind: "own_fact_receipt", reference: "sf2receipt_" + "a".repeat(64), status: "succeeded", message: "原操作已完成", target_version: 13, changed_count: 1, request_id: null };
  const host = await render([malformed]);
  expect(host.querySelector('[aria-label="正式操作回执"]')).toBeNull();
  expect(host.querySelector(".unavailable-card")).not.toBeNull();
});

it('limits supplementary directory and student denials to partial queries alongside granted records', async () => {
 const host = await render([query,
  {...staffProfile,status:'denied',profile:{}},
  {kind:'directory_bundle',status:'denied',subject_label:'另一组织',results:[]},
 ]);
 const notices=[...host.querySelectorAll('.important-notice')].map(n=>n.textContent).filter(n=>n?.includes('无权'));
 expect(notices.length).toBeGreaterThan(0);
 expect(notices.every(n=>n?.startsWith('部分查询未获准：'))).toBe(true);
 expect(notices.join(' ')).toContain('另一组织');
 expect(notices.join(' ')).toContain('学生资料');
 expect(host.querySelector('details')?.textContent).toContain('综合组');
});

const residenceAnswer = {
  kind: "family_residence", status: "available", as_of: stamp, queried_at: stamp,
  subject: { child_ref: "private-child", label: "林同学" },
  coverage: { status: "unknown" }, limitations: ["formal_arrangement_not_actual_presence"],
  records: [{ fact_kind: "student_bed_residence", student_name: "林同学", building_name: "甲楼", room_name: "301室", bed_name: "2号床", valid_from: stamp, valid_to: null }],
};

it("keeps normal or unknown maintenance explanations folded without claiming verified completeness", async () => {
  const text = "林同学住在甲楼301室2号床。";
  const host = await render([residenceAnswer, { ...query, quality: neutralQuality }], text);
  expect(defaultText(host)).toBe(text);
  expect(host.querySelector('[aria-label="系统提示"]')).toBeNull();
  expect(host.querySelector("details.evidence")?.textContent).toContain("正式安排不代表实际到宿");
  expect(host.querySelector("details.evidence")?.textContent).toContain("质量范围依据");
  expect(defaultText(host)).not.toContain("已经全部核实");
});

it("shows a concise residence risk while preserving object, time and maintenance detail", async () => {
  const fact = { ...residenceAnswer, records: [{ ...residenceAnswer.records[0], quality: [{ source_status: "unavailable", review_due_at: "2026-09-01T00:00:00Z" }] }] };
  const host = await render([fact], "按目前登记，林同学住在甲楼301室2号床。");
  const notice = outsideDetails(host);
  for (const text of ["林同学", "甲楼", "301室", "2号床", "来源暂时无法核实", "需要再次核对"]) expect(notice).toContain(text);
  for (const text of [stamp, "结果范围", "核实期限", "private-child"]) expect(notice).not.toContain(text);
  const details = host.querySelector('[aria-label="住宿安排依据"]')!;
  expect(details.closest("details")?.open).toBe(false);
  expect(details.textContent).toContain("核实期限");
  expect(details.textContent).toContain(stamp);
});

it.each([{ has_more: true }, { coverage: { status: "partial" } }])(
  "keeps actual incomplete residence results visible regardless of answer wording: %j",
  async incomplete => {
    const host = await render([{ ...residenceAnswer, ...incomplete }], "资料齐全，没有其他需要说明的内容。");
    expect(outsideDetails(host)).toContain("住宿资料还没有查全");
    expect(host.querySelector(".answer-text")?.textContent).toBe("资料齐全，没有其他需要说明的内容。");
  },
);

it("does not turn a formal day-student result into a generic source or missing-data warning", async () => {
  const text = "周同学目前走读，没有校内床位安排。";
  const host = await render([{ ...residenceAnswer, subject: { child_ref: "private-child-b", label: "周同学" }, residence_mode: "day_student", records: [], limitations: [], quality: [{ declaration: "not_exists", target_kind: "student_bed_residence", source_status: "available" }] }], text);
  expect(defaultText(host)).toBe(text);
  expect(outsideDetails(host)).toBe("");
});
