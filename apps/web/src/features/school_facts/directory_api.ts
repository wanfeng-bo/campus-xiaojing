import { ProductApiError } from "./product_api";
import { isDirectoryCatalog, isDirectoryDescriptor, isDirectoryQuery, isDirectoryProposal, isDirectoryOriginal, isProductReceipt, type DirectoryChoice, type DirectoryDescriptor, type DirectoryQuery, type DirectoryQueryInput, type DirectoryChange, type DirectoryProposal, type DirectoryOriginal, type ProductReceipt } from "./directory_contracts";
// Only the owner-reviewed finite public language is accepted, never exception text.
const preflightReasons = [
  "可用床位超过房间容量，请在同一修改方案中处理相关床位。",
  "房间有效期或可用状态未覆盖相关床位，请在同一修改方案中处理。",
  "床位有效期或可用状态未覆盖相关住宿，请在同一修改方案中处理。",
  "正式寄宿资格未覆盖完整住宿期间，请核对住校属性和有效期，并处理相关住宿。",
  "学生在同一期间有重复住宿安排，请在同一修改方案中处理。",
  "床位在同一期间被重复安排，请在同一修改方案中处理。",
  "不能直接改写床位所属房间，请核对原床位并单独登记正确床位。",
  "相关有效期或已有安排尚未核对完整，请补齐明确的同批处理方案。",
] as const;
class DirectoryPreflightError extends ProductApiError {
  readonly code = "school_facts.preflight_rejected";
  constructor(reason: string) { super(409); this.message = reason; }
}
function approvedPreflightReason(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || !("code" in value) ||
      value.code !== "school_facts.preflight_rejected" || !("message" in value) ||
      typeof value.message !== "string" || value.message.length > 800) return;
  let remaining = value.message;
  const accepted: string[] = [];
  for (const reason of preflightReasons) {
    if (remaining.startsWith(reason)) { accepted.push(reason); remaining = remaining.slice(reason.length); }
  }
  if (!accepted.length) return;
  const additional = "另有事项未通过核对，请检查本次对象、引用和版本。";
  if (remaining.startsWith(additional)) { accepted.push(additional); remaining = remaining.slice(additional.length); }
  if (remaining !== "未生成可确认方案。") return;
  return accepted.join("") + remaining;
}
export interface DirectoryApi { choices?(factKind: string, fieldName: string): Promise<DirectoryQuery>; catalog(): Promise<{ families: DirectoryChoice[] }>; describe(kind: string): Promise<DirectoryDescriptor>; query(input: DirectoryQueryInput): Promise<DirectoryQuery>; original(reference: string, asOf?: string): Promise<DirectoryOriginal>; prepare(input: DirectoryChange): Promise<DirectoryProposal>; proposal(reference: string): Promise<DirectoryProposal>; confirm(reference: string): Promise<ProductReceipt>; receipt(reference: string): Promise<ProductReceipt> }
export function createDirectoryApi(family: "directory" | "family" | "education" | "residence" = "directory"): DirectoryApi {
  const base = family === "residence" ? "/api/v1/school-fact-product/residence-facts" : family === "education" ? "/api/v1/school-fact-product/education-facts" : family === "family" ? "/api/v1/school-fact-product/family-facts" : "/api/v1/school-fact-product/facts";
  async function request<T>(path: string, method: "GET" | "POST", decode: (v: unknown) => v is T, body?: unknown): Promise<T> { let response: Response; try { response = await fetch(base + path, { method, credentials: "same-origin", ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) }); } catch { throw new ProductApiError(0); } if (!response.ok) {
    if (response.status === 409) {
      const reason = approvedPreflightReason(await response.json().catch(() => undefined));
      if (reason !== undefined) throw new DirectoryPreflightError(reason);
    }
    throw new ProductApiError(response.status);
  } const value: unknown = await response.json().catch(() => undefined); if (!decode(value)) throw new ProductApiError(200, true); return value; }
  async function receipt(path: string, method: "GET" | "POST", ref: string) { const value = await request(path, method, isProductReceipt); if (value.reference !== ref) throw new ProductApiError(200, true); return value; }
  return {
    ...(family === "directory" ? {} : { choices: (factKind: string, fieldName: string) => request("/choices", "POST", isDirectoryQuery, { fact_kind: factKind, field_name: fieldName }) }),
    catalog: () => request("/catalog", "GET", isDirectoryCatalog), describe: kind => request(`/describe/${encodeURIComponent(kind)}`, "GET", isDirectoryDescriptor),
    query: ({ fact_kind, scope, query_text, reference, related_field, related_ref, as_of, academic_year, campus_ref, position_ref, cursor, page_size }) => request("/query", "POST", isDirectoryQuery, family !== "directory" ? { fact_kind, query_text, reference, related_field, related_ref, as_of, ...(family === "residence" ? {cursor, limit: page_size} : {}), ...(family === "education" ? { academic_year, campus_ref, position_ref } : {}) } : { fact_kind, scope, query_text, reference, related_ref, as_of, cursor, page_size }),
    original: async (ref, asOf) => { const value = await request(`/original/${encodeURIComponent(ref)}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`, "GET", isDirectoryOriginal); if (value.reference !== ref) throw new ProductApiError(200, true); return value; },
    prepare: input => request("/prepare", "POST", isDirectoryProposal, input),
    proposal: async ref => { const value = await request(`/proposals/${encodeURIComponent(ref)}`, "GET", isDirectoryProposal); if (value.proposal_ref !== ref) throw new ProductApiError(200, true); return value; },
    confirm: ref => receipt(`/proposals/${encodeURIComponent(ref)}/confirm`, "POST", ref), receipt: ref => receipt(`/receipts/${encodeURIComponent(ref)}`, "GET", ref),
  };
}
