export interface ProductChoice { ref: string; label: string; kind?: "staff_assignment" | "guardian_relationship" | null }
export interface ProductFeatures {
  f6_features_enabled?: boolean;
  f7_packages_enabled?: boolean;
  /** Older synthetic products omit this flag; formal products require explicit enablement. */
  assistant_enabled?: boolean;
}
export interface ProductSession {
  status: "signed_out" | "signed_in";
  accounts: ProductChoice[];
  contexts: ProductChoice[];
  selected_context_ref: string | null;
  can_maintain: boolean;
  directory_enabled?: boolean;
  family_enabled?: boolean;
  family_conversation_available?: boolean;
  /** Public lifecycle scope only; never an authentication or authorization credential. */
  assistant_context_key?: string | null;
  education_enabled?: boolean;
  selected_context_kind?: "staff_assignment" | "guardian_relationship" | null;
  selection_transition_ref?: string | null;
}
export interface ProductContact {
  reference: string;
  campus_ref: string;
  campus_name: string;
  contact_kind: string;
  contact_value: string;
  service_hours: string | null;
  valid_from: string;
  valid_to: string | null;
  version: number;
  source: { kind?: string; label: string; published_at: string | null; reference: string | null };
}
export interface ProductQuerySelection {
  subject?: "official_contacts" | "campuses";
  scope?: "selected" | "all_authorized";
  campus_name?: string;
  campus_ref?: string;
}
export interface ProductCampus { reference: string; label: string; path: string }
export interface ProductQuery {
  status: "known" | "display_limited" | "not_recorded" | "no_current_records" | "selection_required" | "unmatched" | "ambiguous" | "source_unavailable" | "denied";
  display_notice: string | null;
  items: ProductContact[];
  candidates: ProductCampus[];
  query: { subject: "official_contacts" | "campuses"; scope: "selected" | "all_authorized"; campus_name: string | null; campus_ref: string | null };
  resolved_campuses: ProductCampus[];
  listing: { entity: "contacts" | "campuses"; returned_count: number; matched_count: number | null; truncated: boolean };
  coverage: { status: "unknown" | "partial" | "complete"; description: string };
  queried_at: string;
}
export interface ProductDraft { contact_value: string; service_hours: string; reason: string }
export interface ProductPrepareInput {
  contact_ref: string; contact_value: string; service_hours: string | null; reason: string;
}
export interface ProductProposal {
  proposal_ref: string;
  summary: {
    before: ProductContact;
    after: { contact_value: string; service_hours: string | null };
    reason: string;
  };
  expires_at: string;
}
export interface ProductReceipt {
  reference: string;
  status: "succeeded" | "rejected" | "unknown";
  message: string;
  changed_count: number | null;
  target_version: number | null;
  request_id: string | null;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const nullableText = (value: unknown) => value === null || text(value);
const reference = (value: unknown): value is string => text(value) && value.length > 0;
const integer = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;
const date = (value: unknown): value is string => text(value) && Number.isFinite(Date.parse(value));
const nullableDate = (value: unknown) => value === null || date(value);
const choice = (value: unknown) => record(value) && reference(value.ref) && text(value.label) &&
  (value.kind === undefined || value.kind === null || ["staff_assignment", "guardian_relationship"].includes(String(value.kind)));
const contextKey = (value: unknown): value is string => reference(value) && value.length <= 256 && !/[\u0000-\u0020\u007f]/.test(value);

export function isProductFeatures(value: unknown): value is ProductFeatures {
  return record(value) && ["f6_features_enabled", "f7_packages_enabled", "assistant_enabled"].every(
    key => value[key] === undefined || typeof value[key] === "boolean",
  );
}

/** The server distinguishes the parent's dialogue scope from the selected child card. */
export function hasFamilyConversation(session: ProductSession | null | undefined): boolean {
  return session?.status === "signed_in" && session.family_conversation_available === true &&
    contextKey(session.assistant_context_key) && session.selected_context_kind !== "staff_assignment";
}

/** 验证产品会话；只有服务端发布的候选引用能进入选择控件。 */
export function isProductSession(value: unknown): value is ProductSession {
  return record(value) && ["signed_out", "signed_in"].includes(String(value.status)) &&
    Array.isArray(value.accounts) && value.accounts.every(choice) &&
    Array.isArray(value.contexts) && value.contexts.every(choice) &&
    (value.selected_context_ref === null || reference(value.selected_context_ref)) &&
    typeof value.can_maintain === "boolean" &&
    (value.directory_enabled === undefined || typeof value.directory_enabled === "boolean") &&
    (value.education_enabled === undefined || typeof value.education_enabled === "boolean") &&
    (value.family_enabled === undefined || typeof value.family_enabled === "boolean") &&
    (value.family_conversation_available === undefined || typeof value.family_conversation_available === "boolean") &&
    (value.assistant_context_key === undefined || value.assistant_context_key === null || contextKey(value.assistant_context_key)) &&
    (value.family_conversation_available !== true || (value.status === "signed_in" && contextKey(value.assistant_context_key))) &&
    (value.selected_context_kind === undefined || value.selected_context_kind === null || ["staff_assignment","guardian_relationship"].includes(String(value.selected_context_kind))) &&
    (value.selection_transition_ref === undefined || value.selection_transition_ref === null ||
      (text(value.selection_transition_ref) && /^[A-Za-z0-9_-]{32,128}$/.test(value.selection_transition_ref))) &&
    (value.selected_context_ref === null ||
      value.contexts.some((item) => item.ref === value.selected_context_ref)) &&
    (value.status !== "signed_out" || (value.selected_context_ref === null && !value.can_maintain));
}

/** 官方联系的可展示字段保持固定，不把任意响应对象当作卡片。 */
export function isProductContact(value: unknown): value is ProductContact {
  return record(value) && reference(value.reference) && reference(value.campus_ref) && text(value.campus_name) &&
    text(value.contact_kind) && text(value.contact_value) && nullableText(value.service_hours) &&
    date(value.valid_from) && nullableDate(value.valid_to) && integer(value.version) &&
    record(value.source) && text(value.source.label) &&
    (value.source.kind === undefined || text(value.source.kind)) &&
    nullableDate(value.source.published_at) && nullableText(value.source.reference);
}

/** 未获正式结果时不能夹带联系明细，拒绝状态与空缺状态分别展示。 */
export function isProductQuery(value: unknown): value is ProductQuery {
  const campus = (item: unknown): item is ProductCampus => record(item) && reference(item.reference) && text(item.label) && text(item.path);
  const unique = (items: { reference: string }[]) => new Set(items.map(item => item.reference)).size === items.length;
  if (!record(value) ||
    !["known", "display_limited", "not_recorded", "no_current_records", "selection_required", "unmatched", "ambiguous", "source_unavailable", "denied"].includes(String(value.status)) ||
    !nullableText(value.display_notice) ||
    !Array.isArray(value.items) || !value.items.every(isProductContact) ||
    !Array.isArray(value.candidates) || !value.candidates.every(campus) ||
    !Array.isArray(value.resolved_campuses) || !value.resolved_campuses.every(campus) ||
    !record(value.query) || !["official_contacts", "campuses"].includes(String(value.query.subject)) ||
    !["selected", "all_authorized"].includes(String(value.query.scope)) ||
    !nullableText(value.query.campus_name) || !nullableText(value.query.campus_ref) ||
    !record(value.listing) || !["contacts", "campuses"].includes(String(value.listing.entity)) ||
    !integer(value.listing.returned_count) || value.listing.returned_count > 100 ||
    !(value.listing.matched_count === null || integer(value.listing.matched_count)) ||
    typeof value.listing.truncated !== "boolean" ||
    !record(value.coverage) || !["unknown", "partial", "complete"].includes(String(value.coverage.status)) ||
    !text(value.coverage.description) || !date(value.queried_at)) return false;
  const query = value.query as ProductQuery["query"];
  const listing = value.listing as ProductQuery["listing"];
  const { items, candidates, resolved_campuses: resolved } = value;
  const selected = query.campus_name !== null || query.campus_ref !== null;
  const needsSelection = ["ambiguous", "selection_required"].includes(String(value.status));
  const empty = !items.length && !candidates.length && !resolved.length;
  if ((query.scope === "all_authorized" && selected) ||
    (query.campus_name !== null && query.campus_ref !== null) ||
    (query.campus_name !== null && !(text(query.campus_name) && query.campus_name.trim().length > 0 && query.campus_name.length <= 100)) ||
    (query.campus_ref !== null && !reference(query.campus_ref)) ||
    [items, candidates, resolved].some(rows => rows.length > 100 || !unique(rows))) return false;
  if (value.status === "display_limited") return empty && listing.returned_count === 0 && listing.truncated &&
    text(value.display_notice) && value.display_notice.trim().length > 0;
  if (listing.entity !== (query.subject === "campuses" || needsSelection ? "campuses" : "contacts")) return false;
  const listed = listing.entity === "contacts" ? items : needsSelection ? candidates : resolved;
  if (listing.returned_count !== listed.length || (listing.matched_count !== null &&
    (listing.matched_count < listed.length || (!listing.truncated && listing.matched_count !== listed.length)))) return false;
  if (["denied", "source_unavailable"].includes(String(value.status))) {
    return empty && listing.matched_count === null && !listing.truncated;
  }
  if (value.status === "unmatched") return empty && query.scope === "selected" && selected && !listing.truncated &&
    listing.matched_count === (query.subject === "campuses" ? 0 : null);
  if (needsSelection) {
    if (query.scope !== "selected" || items.length || resolved.length || listing.matched_count === null) return false;
    return value.status === "selection_required" ? !selected :
      query.campus_name !== null && query.campus_ref === null && listing.matched_count >= 2 &&
      (candidates.length >= 2 || (candidates.length === 1 && listing.truncated));
  }
  if (candidates.length || (query.scope === "selected" && (!selected || resolved.length !== 1)) ||
    (query.campus_ref !== null && resolved.some(item => item.reference !== query.campus_ref))) return false;
  if (items.some(item => !resolved.some(place => place.reference === item.campus_ref))) return false;
  if (["no_current_records", "not_recorded"].includes(String(value.status))) {
    return items.length === 0 && (query.subject !== "campuses" || resolved.length === 0) &&
      (listing.matched_count === 0 || listing.matched_count === null);
  }
  return listed.length > 0 && (query.subject !== "campuses" ||
    (items.length === 0 && listing.matched_count !== null));
}


export function isProductProposal(value: unknown): value is ProductProposal {
  return record(value) && reference(value.proposal_ref) && date(value.expires_at) &&
    record(value.summary) && isProductContact(value.summary.before) &&
    record(value.summary.after) && text(value.summary.after.contact_value) &&
    nullableText(value.summary.after.service_hours) && text(value.summary.reason);
}

export function isProductReceipt(value: unknown): value is ProductReceipt {
  return record(value) && reference(value.reference) &&
    ["succeeded", "rejected", "unknown"].includes(String(value.status)) &&
    text(value.message) && (value.changed_count === null || integer(value.changed_count)) &&
    (value.target_version === null || integer(value.target_version)) && nullableText(value.request_id);
}

/** 渠道编号由正式来源发布；来源批次不是执行回执。 */
export function productSourceKind(kind: string): string {
  return ({ manual: "人工录入", excel: "文件导入", admin: "正式维护", import: "正式导入", initialization: "受控初始化" } as Record<string, string>)[kind] ?? kind;
}

export function productContactKind(kind: string): string {
  return ({ phone: "电话", mobile: "手机", email: "电子邮箱" } as Record<string, string>)[kind] ?? kind;
}
