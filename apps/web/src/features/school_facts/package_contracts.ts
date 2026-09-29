/** F7 package transport is independent of the retained legacy admin contract. */
export type PackageValue = null | boolean | number | string | PackageValue[] | { [key: string]: PackageValue };
export interface PackageSource {
  logical_source_code: string; source_sha256: string; size_bytes: number;
  mapping_version: string; mapping_config_sha256: string; responsibility_reference: string;
  domain_options?:{effective_at:string;fact_kind?:string|null;target_refs:string[];reason:string}|null;
  purpose: "facts" | "policy_text"; attached_policy_source_key: string | null; fact_kinds: string[];
}
export interface PackageSelection { fact_kind: string; source_keys: string[]; effective_at: string }
export interface PackageManifest {
  input_version: "school_facts_v5"; school_id: string; base_version: number;
  mode: "incremental" | "replace"; coverage_status: "unknown";
  replacement_scopes: PackageSelection[]; sources: PackageSource[];
}
export interface PackageField { field_code: string; new_value: PackageValue }
export interface PackageChange {
  fact_kind: string; source_key: string; operation: string; expected_version: number;
  sheet_code: string; row_number: number; fields: PackageField[]; effective_at: string | null;
  replacement_segments: { effective_at: string; valid_to: string | null; fields: PackageField[];
    cause_source_key: string | null; cause_version: number | null }[];
  correction_reason: string | null; cause_source_key: string | null; cause_version: number | null;
}
export interface PackagePlan {
  plan_ref: string; plan_sha256: string; is_clean: boolean;
  issues: { source_slot: string; sheet_code: string; row_number: number; column_code: string; error_code: string }[];
  changes: PackageChange[]; status_decisions: Record<string, PackageValue>[];
  replacement_preview: { fact_kind: string; source_key: string; effective_at: string;
    expected_version: number; state: string; selection_action: string; before: Record<string, PackageValue> }[];
  preview_status?: "complete" | "incomplete" | "unavailable";
  preview_issues?: string[];
  change_preview?: PackageChangePreview[];
}
export interface PackageChangePreview {
  fact_kind:string;source_key:string;operation:string;effective_at:string;expected_version:number;
  origin:"explicit"|"replacement_end"|"linked";
  before:Record<string,PackageValue>|null;after:Record<string,PackageValue>|null;
  before_segments:Record<string,PackageValue>[];after_segments:Record<string,PackageValue>[];
  domain_preview:Record<string,PackageValue>|null;
}
export const PACKAGE_MAX_BYTES = 10 * 1024 * 1024;
const fail = (): never => { throw new Error("资料包格式或服务端响应无效，请核对已批准的输入清单。"); };
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown, max=1000): v is string => typeof v === "string" && v.length > 0 && v.length <= max;
const integer = (v: unknown, min=0): v is number => Number.isSafeInteger(v) && Number(v) >= min;
export const packageRef = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{10,128}$/.test(v);
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const date = (v: unknown): v is string => text(v,64) && /(?:Z|[+-]\d\d:\d\d)$/.test(v) && Number.isFinite(Date.parse(v));
function keys(v: Record<string, unknown>, allowed: string[]) { return Object.keys(v).every(k=>allowed.includes(k)); }
function json(v: unknown, depth=0): v is PackageValue {
  return depth <= 12 && (v === null || typeof v === "boolean" || typeof v === "string" && v.length <= PACKAGE_MAX_BYTES ||
    typeof v === "number" && Number.isFinite(v) || Array.isArray(v) && v.length <= 50000 && v.every(x=>json(x,depth+1)) ||
    obj(v) && Object.keys(v).length <= 200 && Object.values(v).every(x=>json(x,depth+1)));
}
export function parsePackageManifest(value: unknown): PackageManifest {
  if (!obj(value) || !keys(value,["input_version","school_id","base_version","mode","coverage_status","replacement_scopes","sources"]) ||
    value.input_version !== "school_facts_v5" || !text(value.school_id,36) || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value.school_id) ||
    !integer(value.base_version,1) || ![undefined,"incremental","replace"].includes(value.mode as string) ||
    ![undefined,"unknown"].includes(value.coverage_status as string) || !Array.isArray(value.sources) || value.sources.length < 1 || value.sources.length > 32) return fail();
  const sources: PackageSource[] = value.sources.map(raw=>{
    if (!obj(raw) || !keys(raw,["logical_source_code","source_sha256","size_bytes","mapping_version","mapping_config_sha256","responsibility_reference","purpose","attached_policy_source_key","fact_kinds","domain_options"]) ||
      !text(raw.logical_source_code,64) || !/^[a-z][a-z0-9_]*$/.test(raw.logical_source_code) || raw.logical_source_code.startsWith("f7_replacement_") ||
      !hash(raw.source_sha256) || !integer(raw.size_bytes,1) || raw.size_bytes > PACKAGE_MAX_BYTES || !text(raw.mapping_version,120) ||
      !hash(raw.mapping_config_sha256) || !text(raw.responsibility_reference,10000) || ![undefined,"facts","policy_text"].includes(raw.purpose as string) ||
      !Array.isArray(raw.fact_kinds) || raw.fact_kinds.length > 53 || !raw.fact_kinds.every(k=>text(k,120)) || new Set(raw.fact_kinds).size!==raw.fact_kinds.length) return fail();
    const options=raw.domain_options;
    if(options!==undefined&&options!==null&&(!obj(options)||!keys(options,["effective_at","fact_kind","target_refs","reason"])||!date(options.effective_at)||!text(options.reason,10000)||
      !(options.fact_kind===undefined||options.fact_kind===null||text(options.fact_kind,120))||!Array.isArray(options.target_refs)||!options.target_refs.length||options.target_refs.length>300||!options.target_refs.every(v=>text(v,1000))))return fail();
    const purpose=raw.purpose ?? "facts", target=raw.attached_policy_source_key ?? null;
    if (purpose === "facts" ? raw.fact_kinds.length===0 || target!==null : raw.fact_kinds.length!==0 || !text(target,255)) return fail();
    return {...raw,purpose,attached_policy_source_key:target} as unknown as PackageSource;
  });
  if (new Set(sources.map(s=>s.logical_source_code)).size!==sources.length || sources.reduce((n,s)=>n+s.size_bytes,0)>PACKAGE_MAX_BYTES) return fail();
  const scopes=value.replacement_scopes ?? [];
  if (!Array.isArray(scopes) || scopes.length>32) return fail();
  const declared=new Set(sources.flatMap(s=>s.fact_kinds)), selected=new Set<string>();
  for (const s of scopes) {
    if (!obj(s) || !keys(s,["fact_kind","source_keys","effective_at"]) || !text(s.fact_kind,120) || !declared.has(s.fact_kind) || !date(s.effective_at) ||
      !Array.isArray(s.source_keys) || !s.source_keys.length || s.source_keys.length>50000) return fail();
    for (const key of s.source_keys) {
      const coordinate=JSON.stringify([s.fact_kind,key]);
      if (!text(key,255) || selected.has(coordinate)) return fail();
      selected.add(coordinate);
    }
  }
  const mode=value.mode ?? "incremental";
  if (selected.size>50000 || (mode==="replace" ? !scopes.length : scopes.length!==0)) return fail();
  return {input_version:"school_facts_v5",school_id:value.school_id,base_version:value.base_version,
    mode:mode as PackageManifest["mode"],coverage_status:"unknown",sources,replacement_scopes:scopes as PackageSelection[]};
}
function fields(v: unknown): boolean {
  return Array.isArray(v) && v.length<=200 && v.every(f=>obj(f)&&text(f.field_code,120)&&json(f.new_value));
}
export function decodePackagePlan(value: unknown): PackagePlan {
  if (!obj(value) || !packageRef(value.plan_ref) || !hash(value.plan_sha256) || typeof value.is_clean!=="boolean" ||
    !Array.isArray(value.issues) || value.issues.length>250001 || !value.issues.every(v=>obj(v)&&text(v.source_slot,64)&&text(v.sheet_code,255)&&integer(v.row_number,1)&&text(v.column_code,255)&&text(v.error_code,255)) ||
    value.is_clean !== (value.issues.length===0) || !Array.isArray(value.changes) || !value.changes.length || value.changes.length>50000 ||
    !value.changes.every(c=>obj(c)&&text(c.fact_kind,120)&&text(c.source_key,255)&&["upsert","end","revoke","reactivate","correct","cancel"].includes(String(c.operation))&&integer(c.expected_version)&&text(c.sheet_code,255)&&integer(c.row_number,1)&&fields(c.fields)&&
      (c.effective_at===null||date(c.effective_at))&&Array.isArray(c.replacement_segments)&&c.replacement_segments.length<=1000&&c.replacement_segments.every(s=>obj(s)&&date(s.effective_at)&&(s.valid_to===null||date(s.valid_to))&&fields(s.fields))) ||
    !Array.isArray(value.status_decisions) || value.status_decisions.length>100000 || !value.status_decisions.every(v=>obj(v)&&json(v)) ||
    !Array.isArray(value.replacement_preview) || value.replacement_preview.length>50000 || !value.replacement_preview.every(r=>obj(r)&&text(r.fact_kind,120)&&text(r.source_key,255)&&date(r.effective_at)&&integer(r.expected_version)&&text(r.state,40)&&text(r.selection_action,40)&&obj(r.before)&&json(r.before))) return fail();
  if(value.preview_status!==undefined && !["complete","incomplete","unavailable"].includes(String(value.preview_status)))return fail();
  if(value.preview_issues!==undefined && (!Array.isArray(value.preview_issues)||value.preview_issues.length>50000||!value.preview_issues.every(v=>text(v,255)&&/^[A-Za-z0-9_.:-]+$/.test(v))))return fail();
  if(value.change_preview!==undefined && (!Array.isArray(value.change_preview)||value.change_preview.length>50000||!value.change_preview.every(v=>obj(v)&&text(v.fact_kind,120)&&text(v.source_key,255)&&text(v.operation,30)&&date(v.effective_at)&&integer(v.expected_version)&&["explicit","replacement_end","linked"].includes(String(v.origin))&&
    (v.before===null||obj(v.before)&&json(v.before))&&(v.after===null||obj(v.after)&&json(v.after))&&
    Array.isArray(v.before_segments)&&v.before_segments.length<=1000&&v.before_segments.every(s=>obj(s)&&json(s))&&
    Array.isArray(v.after_segments)&&v.after_segments.length<=1000&&v.after_segments.every(s=>obj(s)&&json(s))&&
    (v.domain_preview===null||obj(v.domain_preview)&&json(v.domain_preview)))))return fail();
  return structuredClone(value) as unknown as PackagePlan;
}
/** Missing legacy fields can be displayed, but can never authorize confirmation. */
export function hasCompletePackageReview(plan: PackagePlan): boolean {
  if(plan.preview_status!=="complete"||!plan.preview_issues||plan.preview_issues.length||!plan.change_preview||plan.change_preview.length!==plan.changes.length)return false;
  const rows=new Map(plan.change_preview.map(row=>[JSON.stringify([row.fact_kind,row.source_key]),row]));
  if(rows.size!==plan.changes.length||new Set(plan.changes.map(row=>JSON.stringify([row.fact_kind,row.source_key]))).size!==plan.changes.length)return false;
  return plan.changes.every(change=>{
    const row=rows.get(JSON.stringify([change.fact_kind,change.source_key]));
    return row!==undefined&&row.operation===change.operation&&row.expected_version===change.expected_version&&
      change.effective_at!==null&&Date.parse(row.effective_at)===Date.parse(change.effective_at);
  });
}
