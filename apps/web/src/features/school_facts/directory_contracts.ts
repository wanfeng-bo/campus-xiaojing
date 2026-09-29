import { isProductReceipt, type ProductReceipt } from "./product_contracts";
export type FieldValue = string | number | boolean | null | string[] | Record<string, number>;
export interface DirectoryChoice { value: string; label: string }
export interface DirectoryField { name: string; label: string; type: "text" | "textarea" | "lines" | "datetime" | "number" | "boolean" | "select" | "reference" | "references" | "object"; required: boolean; reference_kind: string | null; options: DirectoryChoice[] }
export interface DirectoryRelatedField { field: string; kind: string; label: string }
export interface DirectoryQuerySupport { name_fields: string[]; name_match: "exact"; related_kinds: string[]; related_fields?: DirectoryRelatedField[]; reference: boolean; all_authorized: boolean; related_match: "direct"; guidance: string }
export interface DirectoryDescriptor { query_support?: DirectoryQuerySupport; fact_kind: string; label: string; fields: DirectoryField[]; creation_choices: { creation_ref: string; label: string }[] }
export interface DirectorySource { label: string; kind?: string; reference: string | null; published_at: string | null }
export interface DirectoryRecord { reference: string; fact_kind: string; label: string; fields: Record<string, FieldValue>; version: number; valid_from: string; valid_to: string | null; source: DirectorySource; original_text?: { excerpt: string; length: number; complete: boolean; notice: string } }
export interface DirectoryQuery { query_support?: DirectoryQuerySupport; quality?: DirectoryQuality; kind: "directory_facts"; status: string; fact_kind: string; records: DirectoryRecord[]; matched_count: number | null; returned_count: number; truncated: boolean; next_cursor: string | null; queried_at: string; factual_at: string; coverage: { status: string; description: string } }
export type DirectoryBundleIssue = { kind: "directory_facts"; fact_kind: string; records: []; query: Record<string, unknown> } & (
  { status: "denied" | "source_unavailable" | "unsupported_selection" | "error" | "unavailable" }
  | { status: "partial"; truncated: true; notice: string }
);
export interface DirectoryBundle { kind: "directory_bundle"; status: string; subject_label: string | null; results: Array<DirectoryQuery | DirectoryBundleIssue> }
export interface DirectoryQueryInput { cursor?: string; page_size?: number; academic_year?: number; campus_ref?: string; position_ref?: string; fact_kind: string; scope: "selected" | "all_authorized"; query_text?: string; reference?: string; related_field?: string; related_ref?: string; as_of?: string }
export interface DirectoryChange { cancel_from?: string; reschedule_from?: string; import_rows?: Record<string, FieldValue>[] | DirectoryChange[]; import_mode?: "incremental" | "replace"; import_target_refs?: string[]; import_replacement_refs?: string[]; fact_kind: string; target_ref?: string; creation_ref?: string; operation: "upsert" | "end" | "revoke" | "reactivate" | "correct" | "cancel"; additional_changes?: DirectoryChange[]; purpose?: "record" | "transfer" | "graduation" | "close_class" | "replace_assignment" | "cancel_future" | "historical_correction"; corrected_purpose?: string; student_ref?: string; old_class_ref?: string; replace_refs?: string[]; fields: Record<string, FieldValue>; effective_at?: string; reason: string }
export interface DirectoryTimelineSegment { effective_at: string; valid_to: string | null; fields: Record<string, FieldValue>; source?: { source_kind?: string | null; source_ref?: string | null; batch_id?: string | null; publication_reference?: string | null; published_at?: string | null }; cause_reference?: string | null; cause_version?: number | null }
export interface DirectoryProposal { original_notices?: string[]; kind?: "fact_proposal"; proposal_ref: string; summary: { reference_labels?: Record<string,string>; reference_labels_as_of?: string | null; regroup?: DirectoryRegroup | null; before_segments?: DirectoryTimelineSegment[]; after_segments?: DirectoryTimelineSegment[]; impacts?: DirectoryProposal["summary"][]; label: string; reference: string; fact_kind: string; operation: string; before: Record<string, FieldValue> | null; after: Record<string, FieldValue> | null; effective_at: string; reason: string; source_label: string }; expires_at: string }
export interface DirectoryOriginal { kind: "policy_original"; reference: string; version: number; text: string; offset: number; end: number; length: number; complete: boolean; has_more: boolean; source: DirectorySource; notice: string }
export type { ProductReceipt };
export { isProductReceipt };
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): v is string => typeof v === "string";
const date = (v: unknown) => str(v) && Number.isFinite(Date.parse(v));
const count = (v: unknown): v is number => Number.isInteger(v) && Number(v) >= 0;
const fields = (v: unknown) => obj(v) && Object.entries(v).every(([name,x]) => x === null || ["string", "boolean", "number"].includes(typeof x) || (Array.isArray(x) && x.every(str)) || (obj(x) && Object.keys(x).every(key => (name === "teaching_role_limits" ? ["lead", "co_teacher"] : ["primary", "deputy", "joint", "acting"]).includes(key)) && Object.values(x).every(n => Number.isInteger(n) && Number(n) > 0)));
const choice = (v: unknown) => obj(v) && str(v.value) && str(v.label);
const source = (v: unknown) => obj(v) && str(v.label) && (v.reference === null || str(v.reference)) && (v.published_at === null || date(v.published_at));
function isQuerySupport(v: unknown): v is DirectoryQuerySupport {
  if (!obj(v) || !Array.isArray(v.name_fields) || !v.name_fields.every(str) || v.name_match !== "exact" || !Array.isArray(v.related_kinds) || !v.related_kinds.every(str)) return false;
  const relatedKinds = v.related_kinds as string[];
  if (v.related_fields !== undefined && (!Array.isArray(v.related_fields) || !v.related_fields.every(item => obj(item) && str(item.field) && str(item.kind) && str(item.label) && relatedKinds.includes(item.kind)))) return false;
  return typeof v.reference === "boolean" && typeof v.all_authorized === "boolean" && v.related_match === "direct" && str(v.guidance);
}
export function isDirectoryDescriptor(v: unknown): v is DirectoryDescriptor { return obj(v) && (v.query_support === undefined || isQuerySupport(v.query_support)) && str(v.fact_kind) && str(v.label) && Array.isArray(v.fields) && v.fields.every(f => obj(f) && str(f.name) && str(f.label) && ["text", "textarea", "lines", "datetime", "number", "boolean", "select", "reference", "references", "object"].includes(String(f.type)) && typeof f.required === "boolean" && (f.reference_kind === null || str(f.reference_kind)) && Array.isArray(f.options) && f.options.every(choice)) && Array.isArray(v.creation_choices) && v.creation_choices.every(c => obj(c) && str(c.creation_ref) && str(c.label)); }
export function isDirectoryRecord(v: unknown): v is DirectoryRecord { return obj(v) && str(v.reference) && str(v.fact_kind) && str(v.label) && fields(v.fields) && count(v.version) && date(v.valid_from) && (v.valid_to === null || date(v.valid_to)) && source(v.source) && (v.original_text === undefined || (obj(v.original_text) && str(v.original_text.excerpt) && count(v.original_text.length) && typeof v.original_text.complete === "boolean" && str(v.original_text.notice))); }
export function isDirectoryQuery(v: unknown): v is DirectoryQuery { if (!obj(v) || v.kind !== "directory_facts" || !str(v.status) || !str(v.fact_kind) || !Array.isArray(v.records) || !v.records.every(isDirectoryRecord) || !count(v.returned_count) || v.returned_count !== v.records.length || !(v.matched_count === null || (count(v.matched_count) && v.matched_count >= v.returned_count)) || typeof v.truncated !== "boolean" || !(v.next_cursor === null || str(v.next_cursor)) || !date(v.queried_at) || !date(v.factual_at) || !obj(v.coverage) || !str(v.coverage.status) || !str(v.coverage.description)) return false; if (v.query_support !== undefined && !isQuerySupport(v.query_support)) return false; if (v.quality !== undefined && !isDirectoryQuality(v.quality)) return false; return !["denied", "source_unavailable", "unsupported_selection"].includes(v.status) || (v.records.length === 0 && v.matched_count === null); }
function isDirectoryBundleIssue(v: unknown): v is DirectoryBundleIssue {
  if (!obj(v) || v.kind !== "directory_facts" || !str(v.fact_kind) || !Array.isArray(v.records) || v.records.length !== 0 || !obj(v.query)) return false;
  if (v.status === "partial") return v.truncated === true && str(v.notice) && v.notice.trim().length > 0;
  return ["denied", "source_unavailable", "unsupported_selection", "error", "unavailable"].includes(String(v.status));
}
export function isDirectoryBundle(v: unknown): v is DirectoryBundle { return obj(v) && v.kind === "directory_bundle" && str(v.status) && (v.subject_label === null || str(v.subject_label)) && Array.isArray(v.results) && v.results.length <= 32 && v.results.every(item => isDirectoryQuery(item) || isDirectoryBundleIssue(item)); }
function timeline(v: unknown): boolean { return v === undefined || (Array.isArray(v) && v.length <= 1000 && v.every(s => obj(s) && date(s.effective_at) && (s.valid_to === null || (date(s.valid_to) && Date.parse(String(s.valid_to)) > Date.parse(String(s.effective_at)))) && fields(s.fields) && (s.source === undefined || (obj(s.source) && Object.entries(s.source).every(([k,v]) => ["source_kind","source_ref","batch_id","publication_reference","published_at"].includes(k) && (v === null || typeof v === "string")))) && (s.cause_version === undefined || s.cause_version === null || (count(s.cause_version) && s.cause_version > 0)) && (s.cause_reference === undefined || s.cause_reference === null || str(s.cause_reference)))); }
export function isDirectoryProposal(v: unknown): v is DirectoryProposal { return obj(v) && str(v.proposal_ref) && date(v.expires_at) && obj(v.summary) && str(v.summary.reference) && str(v.summary.fact_kind) && str(v.summary.label) && ["upsert", "end", "revoke", "reactivate", "correct", "cancel"].includes(String(v.summary.operation)) && (v.summary.before === null || fields(v.summary.before)) && (v.summary.after === null || fields(v.summary.after)) && date(v.summary.effective_at) && str(v.summary.reason) && str(v.summary.source_label) && (v.summary.reference_labels === undefined || (obj(v.summary.reference_labels) && Object.values(v.summary.reference_labels).every(str))) && (v.summary.reference_labels_as_of === undefined || v.summary.reference_labels_as_of === null || date(v.summary.reference_labels_as_of)) && regroup(v.summary.regroup) && timeline(v.summary.before_segments) && timeline(v.summary.after_segments) && (v.summary.impacts === undefined || (Array.isArray(v.summary.impacts) && v.summary.impacts.length <= 300 && v.summary.impacts.every(item => obj(item) && (item.impacts === undefined || (Array.isArray(item.impacts) && item.impacts.length === 0)) && isDirectoryProposal({proposal_ref:v.proposal_ref,expires_at:v.expires_at,summary:item})))); }
/** Chat may retain a bounded original segment; the full-original HTTP contract stays strict. */
export function isDirectoryOriginalSegment(v: unknown): v is DirectoryOriginal { return obj(v) && v.kind === "policy_original" && str(v.reference) && count(v.version) && str(v.text) && count(v.offset) && count(v.end) && count(v.length) && v.offset <= v.end && v.end <= v.length && Array.from(v.text).length === v.end - v.offset && v.complete === (v.offset === 0 && v.end === v.length) && v.has_more === (v.end < v.length) && source(v.source) && str(v.notice); }
export function isDirectoryOriginal(v: unknown): v is DirectoryOriginal { return isDirectoryOriginalSegment(v) && v.offset === 0 && v.complete === true && v.has_more === false; }
export function isDirectoryCatalog(v: unknown): v is { families: DirectoryChoice[] } { return obj(v) && Array.isArray(v.families) && v.families.every(choice); }
export function displayField(value: unknown): string { return value === null || value === undefined ? "未填写" : typeof value === "boolean" ? value ? "是" : "否" : Array.isArray(value) ? value.join("；") : typeof value === "object" ? Object.entries(value).map(([key, item]) => `${({primary:"班主任",deputy:"副班主任",joint:"共同班主任",acting:"代任老师",lead:"主讲",co_teacher:"共同任教"} as Record<string,string>)[key] ?? key}：${item}`).join("；") : String(value); }
export const operationLabel = (value: string) => ({ upsert: "发布或更新", end: "结束", revoke: "撤销", reactivate: "重新启用", correct: "补正历史资料", cancel: "撤销未来安排" }[value] ?? value);

/** Compact model cards are display-only; original_text objects never become editable fields. */
function originalExcerpt(value: unknown): value is { excerpt: string; length: number; complete: boolean; notice: string } { return obj(value) && str(value.excerpt) && count(value.length) && typeof value.complete === "boolean" && str(value.notice); }
export function normalizeDirectoryFact(value: unknown): unknown {
  if (!obj(value)) return value;
  if (value.kind === "directory_facts" && Array.isArray(value.records)) {
    return { ...value, records: value.records.map(row => {
      if (!obj(row) || !obj(row.fields) || row.fields.original_text === undefined) return row;
      if (!originalExcerpt(row.fields.original_text)) return row;
      const { original_text, ...rest } = row.fields;
      return { ...row, fields: rest, original_text };
    }) };
  }
  if (value.kind !== "fact_proposal" || !obj(value.summary)) return value;
  const notices: string[] = [];
  function side(raw: unknown) { if (!obj(raw) || raw.original_text === undefined) return raw; if (!originalExcerpt(raw.original_text)) return raw; const { original_text, ...rest } = raw; notices.push(`${original_text.notice}（原文共 ${original_text.length} 字）`); return rest; }
  return { ...value, summary: { ...value.summary, before: side(value.summary.before), after: side(value.summary.after) }, original_notices: notices };
}
export function isCompactDirectoryProposal(value: unknown): boolean { return obj(value) && value.kind === "fact_proposal" && isDirectoryProposal(normalizeDirectoryFact(value)); }



export interface DirectoryQuality { existence: string; coverage: string; source_availability: string; freshness: string; conflict: string; evidence_status: string; evidence_truncated: boolean; notice: string; statements: Array<Record<string, FieldValue> & { reference: string; kind: string; owner_kind: string; owner_ref: string | null; target_fact_kind: string; target_ref: string | null; applies_to_query: boolean; applies_to_records: string[]; valid_from: string; valid_to: string | null }> }
export function isDirectoryQuality(value: unknown): value is DirectoryQuality { return obj(value) && ["existence", "coverage", "source_availability", "freshness", "conflict", "evidence_status", "notice"].every(key => str(value[key])) && typeof value.evidence_truncated === "boolean" && Array.isArray(value.statements) && value.statements.every(row => obj(row) && fields(row) && ["reference", "kind", "owner_kind", "target_fact_kind"].every(key => str(row[key])) && (row.owner_ref === null || str(row.owner_ref)) && (row.target_ref === null || str(row.target_ref)) && typeof row.applies_to_query === "boolean" && Array.isArray(row.applies_to_records) && row.applies_to_records.every(str) && date(row.valid_from) && (row.valid_to === null || date(row.valid_to))); }
export function qualityLabel(value: string): string { return ({ known: "已有正式事实", unknown: "未确认", formal_negative: "正式否定声明", complete: "完整覆盖", partial: "部分覆盖", available: "来源可用", unavailable: "来源不可用", source_unavailable: "质量来源暂时不可用", denied: "无权读取质量声明", forbidden: "无权读取质量声明", not_queried: "未查询质量证据", no_current_records: "未返回有效质量声明", review_overdue: "已到核实期限", within_review_period: "仍在核实期限内", conflicting_statements: "声明之间不一致", contradictory_evidence: "存在相互矛盾的正式证据", open: "争议待核实", resolved: "争议已处置", negative: "正式否定声明", coverage: "覆盖声明", source: "来源与责任", dispute: "争议", review: "时效核实", correction: "纠错依据", school: "学校", organization: "组织", person: "人员", location: "地点" } as Record<string, string>)[value] ?? value; }


export type DirectoryAccess = { kind: "fact_access"; purpose?: "read" | "maintain"; requested_fact_kind?: string | null; notice: string; queried_at: string; families: Array<{ fact_kind: string; label: string; status: "authorized_scope" | "self_only" | "denied" | "source_unavailable" | "requires_prepare"; authorized_target_count?: number; authorized_creation_count?: number; target_in_scope?: boolean; field_check?: "schema_only"; next_step?: string }> };
export function isDirectoryAccess(value: unknown): value is DirectoryAccess {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  // Old in-memory snapshots omit this field; do not infer an overview from row count.
  const selection = v.requested_fact_kind;
  if (selection !== undefined && selection !== null && (typeof selection !== "string" || !selection.trim())) return false;
  if (v.purpose !== undefined && v.purpose !== "read" && v.purpose !== "maintain") return false;
  if (v.purpose === "maintain" && typeof selection !== "string") return false;
  return v.kind === "fact_access" && typeof v.notice === "string" && typeof v.queried_at === "string" && Array.isArray(v.families) && v.families.length <= 18 && v.families.every((row: unknown) => {
    if (typeof row !== "object" || row === null) return false;
    const r = row as Record<string, unknown>;
    const statuses = v.purpose === "maintain" ? ["authorized_scope", "requires_prepare", "denied", "source_unavailable"] : ["authorized_scope", "self_only", "denied", "source_unavailable"];
    return typeof r.fact_kind === "string" && typeof r.label === "string" && statuses.includes(String(r.status)) &&
      (r.status !== "requires_prepare" || (r.target_in_scope === true && r.field_check === "schema_only" && r.next_step === "prepare")) &&
      (r.status !== "authorized_scope" || (typeof r.authorized_target_count === "number" && Number.isInteger(r.authorized_target_count) && r.authorized_target_count >= 0));
  }) && (typeof selection !== "string" || (v.families.length === 1 && v.families[0].fact_kind === selection));
}
export function accessStatusLabel(status: DirectoryAccess["families"][number]["status"], purpose: DirectoryAccess["purpose"] = "read"): string {
  if (purpose === "maintain") return { authorized_scope: "已有限定范围维护职责，仍需指定对象", requires_prepare: "当前对象在维护范围，具体修改仍需预检确认", denied: "未获准维护此次范围", source_unavailable: "维护资格暂时无法核验", self_only: "维护资格暂时无法核验" }[status];
  return { authorized_scope: "已获明确范围读取授权", self_only: "仅本人联系方式", denied: "未取得此类读取授权", source_unavailable: "授权来源暂不可核验", requires_prepare: "仍需预检确认" }[status];
}


export interface OwnReceipt extends ProductReceipt { kind: "own_fact_receipt"; confirmed_at: string; targets: { fact_kind: string; label: string; reference: string; operation: string; target_version: number }[] }
export interface OwnReceiptPage { kind: "own_fact_receipts"; items: OwnReceipt[]; has_more: boolean; notice: string }
export function isOwnReceipt(v: unknown): v is OwnReceipt {
  return obj(v) && isProductReceipt(v) && (v as Record<string, unknown>).kind === "own_fact_receipt" && v.status === "succeeded" && /^sf2receipt_[0-9a-f]{64}$/.test(v.reference) && date((v as Record<string, unknown>).confirmed_at) && Array.isArray((v as Record<string, unknown>).targets) && ((v as Record<string, unknown>).targets as unknown[]).length > 0 && ((v as Record<string, unknown>).targets as unknown[]).every(t => obj(t) && str(t.fact_kind) && str(t.label) && str(t.reference) && str(t.operation) && count(t.target_version));
}
export function isOwnReceiptPage(v: unknown): v is OwnReceiptPage {
  return obj(v) && v.kind === "own_fact_receipts" && Array.isArray(v.items) && v.items.length <= 20 && v.items.every(isOwnReceipt) && typeof v.has_more === "boolean" && str(v.notice);
}

export interface DirectoryRegroup { teaching_cancellations?: {reference:string;effective_at:string;publication_reference:string}[]; teaching_notices?: string[]; teaching_impacts?: {reference:string;label:string;changed:boolean}[]; scope: "batch_only"; notice: string; periods: { effective_at: string; student_count: number; departures: { student_ref: string; from_class_ref: string | null; to_class_ref: string | null; status: "assigned" | "unassigned" }[]; classes: { class_ref: string; label: string | null; class_state: "unchanged" | "ended" | "recorded"; outgoing_count: number; incoming_count: number }[]; unassigned: string[]; duplicates: string[] }[] }

function regroup(v: unknown): boolean { return v === undefined || v === null || (obj(v) && (v.teaching_notices === undefined || (Array.isArray(v.teaching_notices) && v.teaching_notices.length <= 300 && v.teaching_notices.every(str))) && (v.teaching_impacts === undefined || (Array.isArray(v.teaching_impacts) && v.teaching_impacts.length <= 300 && v.teaching_impacts.every(i => obj(i) && str(i.reference) && str(i.label) && typeof i.changed === "boolean"))) && v.scope === "batch_only" && str(v.notice) && Array.isArray(v.periods) && v.periods.length <= 300 && v.periods.every(p => obj(p) && date(p.effective_at) && count(p.student_count) && Array.isArray(p.departures) && p.departures.length === p.student_count && p.departures.length <= 300 && p.departures.every(d => obj(d) && str(d.student_ref) && (d.from_class_ref === null || str(d.from_class_ref)) && ((d.status === "assigned" && str(d.to_class_ref)) || (d.status === "unassigned" && d.to_class_ref === null))) && Array.isArray(p.classes) && p.classes.length <= 600 && p.classes.every(c => obj(c) && str(c.class_ref) && (c.label === null || str(c.label)) && ["unchanged","ended","recorded"].includes(String(c.class_state)) && count(c.outgoing_count) && count(c.incoming_count)) && Array.isArray(p.unassigned) && p.unassigned.every(str) && Array.isArray(p.duplicates) && p.duplicates.length === 0)); }
