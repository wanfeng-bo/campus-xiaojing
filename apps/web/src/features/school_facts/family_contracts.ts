export type FamilyFactSubject = {child_ref:string;label:string} | {scope:string};
const subjectText=(value:unknown):value is string=>typeof value==='string'&&value.trim().length>0&&value.length<=256&&!/[\u0000-\u001f\u007f]/.test(value);
/** Display provenance only: opaque references never confer authority. */
export function validFamilyFactSubject(value:unknown):boolean {
 if(!object(value)||!('subject' in value))return true;
 const subject=value.subject;
 if(!object(subject))return false;
 if(value.kind==='family_school_leadership')return Object.keys(subject).length===1&&subjectText(subject.scope);
 return Object.keys(subject).length===2&&subjectText(subject.child_ref)&&subjectText(subject.label);
}
export function familySubjectLabel(value:unknown):string|null {
 if(!object(value)||!object(value.subject)||!validFamilyFactSubject(value))return null;
 return typeof value.subject.label==='string'?value.subject.label:typeof value.subject.scope==='string'?value.subject.scope:null;
}
export interface ResidenceRecord {
 reference?:string;fact_kind:string;version?:number;display_name?:string|null;student_name?:string|null;building_name?:string|null;room_name?:string|null;bed_name?:string|null;
 valid_from:string;valid_to:string|null;source?:{label:string;published_at?:string|null};quality?:Array<Record<string,unknown>>;
}
export interface ResidenceResult {fact_timezone_name?:string|null;kind:'family_residence'|'residence_facts';status:string;as_of?:string;queried_at?:string;records?:ResidenceRecord[];limitations?:string[];has_more?:boolean;coverage?:{status:string};future_basis?:string;quality?:Array<Record<string,unknown>>}
export function isResidenceResult(value:unknown):value is ResidenceResult {
 if(!validFamilyFactSubject(value))return false;
 return object(value)&&['family_residence','residence_facts'].includes(String(value.kind))&&typeof value.status==='string'&&(typeof value.as_of==='string'||['denied','unavailable'].includes(value.status))&&(value.records===undefined&&['denied','unavailable'].includes(value.status)||Array.isArray(value.records)&&value.records.every(r=>object(r)&&typeof r.fact_kind==='string'&&typeof r.valid_from==='string'&&(r.valid_to===null||typeof r.valid_to==='string')&&['display_name','student_name','building_name','room_name','bed_name'].every(k=>r[k]===undefined||r[k]===null||typeof r[k]==='string')));
}
export function residenceStatusLabel(status:string):string {return ({available:'正式住宿安排',ambiguous:'有多个符合条件的住宿资料，请先选定要查询的对象。',no_current_records:'所问时点没有当前住宿登记',conflict:'住宿资料存在冲突，尚不能确定安排。',denied:'当前身份未获准读取所问住宿资料。',unavailable:'住宿资料暂时无法核实。',partial:'所问住宿资料尚未核对完整。'} as Record<string,string>)[status]??'住宿资料状态尚不能确定。';}
export function residenceNotices(value:ResidenceResult, queryLabel='本次住宿查询', options:{includeDenied?:boolean;concise?:boolean}={}):string[]{
 const notices=['available','no_current_records','ambiguous'].includes(value.status)||(value.status==='denied'&&options.includeDenied===false)?[]:[residenceStatusLabel(value.status)];
 const conciseLimitations:Record<string,string>={source_quality_unavailable:'这项住宿资料还需要核对。',quality_review_overdue:'这项住宿资料已需要再次核对，请先确认。'};
 for(const code of value.limitations??[])if(!['formal_arrangement_not_actual_presence','absence_is_not_proof_of_never_boarding'].includes(code))notices.push(options.concise&&conciseLimitations[code]?conciseLimitations[code]:residenceLimitation(code));
 if(value.has_more||value.coverage?.status==='partial')notices.push('这次住宿资料还没有查全，不能当作全部安排。');
 const checked=Date.parse(value.queried_at??'');
 function qualityNotices(quality:Array<Record<string,unknown>>):string[]{
 const notices:string[]=[];
 if(quality.some(q=>q.dispute_status==='open'))notices.push('住宿资料存在尚未解决的争议。');
 if(quality.some(q=>q.source_status==='unavailable'))notices.push(options.concise?'住宿资料来源暂时无法核实。':'住宿资料来源暂时不可核实。');
 if(quality.some(q=>typeof q.review_due_at==='string'&&Date.parse(q.review_due_at)<=checked))notices.push(options.concise?'这项住宿资料已需要再次核对，请先确认。':'住宿资料已到核实期限，需核对其时效。');
 return notices;
 }
 // Default answers retain the query/object boundary. Exact timing and the
 // maintenance explanation remain in the existing evidence disclosure.
 const time=!options.concise&&value.as_of?`（所问时点 ${value.as_of}）`:'';
 const scope=options.concise?queryLabel:`${queryLabel}的结果范围${time}`;
 const result=notices.map(message=>`${scope}：${message}`);
 result.push(...qualityNotices(value.quality??[]).map(message=>`${scope}：${message}`));
 for(const [index,record] of (value.records??[]).entries()){
  const label=[record.display_name,record.student_name,record.building_name,record.room_name,record.bed_name].filter(Boolean).join(' · ')||`第${index+1}条记录`;
  result.push(...qualityNotices(record.quality??[]).map(message=>`${queryLabel} · ${label}${time}：${message}`));
 }
 return result;
}
export function residenceLimitation(code:string):string {return ({source_quality_unavailable:'住宿资料来源目前不可用，待核实。',quality_review_overdue:'住宿资料核验已过复核期限。',absence_is_not_proof_of_never_boarding:'本次未查到安排，不能据此证明从未住校。',building_use_not_recorded:'楼栋住宿用途尚未登记。',formal_arrangement_not_actual_presence:'正式安排不代表实际到宿。'} as Record<string,string>)[code]??'住宿资料存在尚未核实的限制。';}
export interface FamilyProfile {
  subject?: FamilyFactSubject;
  kind: "family_student_profile";
  status: "available" | "not_recorded" | "no_current_records" | "conflict" | "denied" | "source_unavailable";
  relationship_kind: "verified_guardian" | "recognized_caregiver" | null;
  query_scope: string;
  profile: Record<string, string | null | {district_code:string;district_name:string}>;
  sources: Array<{source_name?:string|null;source_trace_reference?:string|null;fact:string;reference:string;published_at:string;channel:string}>;
  missing: string[];
  conflicting_fields?: string[];
  authorized_fields: string[];
  field_restrictions: Record<string,string>;
  as_of: string;
  queried_at: string;
  coverage: {status:string;meaning:string};
}
const object = (value:unknown): value is Record<string,unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const texts = (value:unknown): value is string[] => Array.isArray(value) && value.every(v=>typeof v === "string");
const fields=["name","enrollment_status","campus","student_number","residence_mode","region"];
export function isFamilyProfile(value:unknown): value is FamilyProfile {
  if (!validFamilyFactSubject(value)) return false;
  if (!object(value) || value.kind !== "family_student_profile" || !["available","not_recorded","no_current_records","conflict","denied","source_unavailable"].includes(String(value.status)) ||
    !object(value.profile) || !Array.isArray(value.sources) || !texts(value.missing) || !texts(value.authorized_fields) || !object(value.field_restrictions) ||
    typeof value.query_scope !== "string" || typeof value.as_of !== "string" || !Number.isFinite(Date.parse(value.as_of)) || typeof value.queried_at !== "string" || !Number.isFinite(Date.parse(value.queried_at)) ||
    !object(value.coverage) || typeof value.coverage.status !== "string" || typeof value.coverage.meaning !== "string") return false;
  if (!["verified_guardian","recognized_caregiver",null].includes(value.relationship_kind as string|null)) return false;
  const allowed=value.relationship_kind === "verified_guardian" ? fields : value.relationship_kind === "recognized_caregiver" ? fields.slice(0,3) : [];
  const conflicts=value.conflicting_fields ?? [];
  const missing=value.missing;
  if (!texts(conflicts) || conflicts.some(k=>!allowed.includes(k)||missing.includes(k)) ||
      (conflicts.length>0 && value.status!=="conflict") || value.authorized_fields.some(k=>!allowed.includes(k))) return false;
  if (Object.keys(value.profile).some(k=>!allowed.includes(k)) || value.missing.some(k=>!allowed.includes(k))) return false;
  if (["denied","source_unavailable"].includes(String(value.status)) && (Object.keys(value.profile).length || value.sources.length)) return false;
  for(const [key,item] of Object.entries(value.profile)) {
    if(key === "region" && item !== null) {
      if(!object(item) || Object.keys(item).some(k=>!["district_code","district_name"].includes(k)) || typeof item.district_code!=="string" || typeof item.district_name!=="string") return false;
    } else if(item !== null && typeof item !== "string") return false;
  }
  if(Object.entries(value.field_restrictions).some(([key,restriction])=>![...fields,"history"].includes(key)||restriction!=="denied"))return false;
  return value.sources.every(s=>object(s)&&["fact","reference","published_at","channel"].every(k=>typeof s[k]==="string") &&
    ["source_name","source_trace_reference"].every(k=>s[k]===undefined||s[k]===null||typeof s[k]==="string"));
}
export const familyFieldLabels:Record<string,string>={name:"姓名",enrollment_status:"在校状态",campus:"当前校区",student_number:"学号",residence_mode:"走读或住宿",region:"居住地区",history:"历史资料"};

export interface StaffStudentProfile {
  kind: "f3_student_profile";
  status: string;
  profile?: Record<string, {status:string;value?:string|null}>;
  candidates?: Array<{name:string|null;student_number:string|null;tool_context?:Record<string,unknown>}>;
  evidence: unknown[];
  sources?: Record<string,unknown>;
  limitations: Array<{status:string;fact_kind?:string;truncated?:boolean}>;
  coverage: {status:string;description:string};
}
const profileStates = ["known","partial","incomplete","ambiguous","object_unresolved","no_current_records","not_recorded","not_returned","unavailable","forbidden","denied","conflict","source_unavailable","selection_unverified"];
export function isStaffStudentProfile(value:unknown): value is StaffStudentProfile {
  if (!object(value) || value.kind!=="f3_student_profile" || !profileStates.includes(String(value.status)) ||
      !Array.isArray(value.evidence) || !Array.isArray(value.limitations) || !value.limitations.every(item=>object(item)&&typeof item.status==="string") ||
      !object(value.coverage) || typeof value.coverage.status!=="string" || typeof value.coverage.description!=="string") return false;
  if (value.profile!==undefined && (!object(value.profile) || !Object.entries(value.profile).every(([key,item])=>fields.includes(key)&&object(item)&&profileStates.includes(String(item.status))&&(item.value===undefined||item.value===null||typeof item.value==="string")&&(item.status!=="known"||typeof item.value==="string")))) return false;
  if (value.candidates!==undefined && (!Array.isArray(value.candidates)||!value.candidates.every(item=>object(item)&&(typeof item.name==="string"||item.name===null)&&(typeof item.student_number==="string"||item.student_number===null)))) return false;
  return value.sources===undefined||object(value.sources);
}
export function staffProfileStateLabel(status:string):string {
  return ({known:"已取得",partial:"只取得部分资料",incomplete:"相关资料尚未核对完整",ambiguous:"存在多项匹配，需要进一步区分",object_unresolved:"尚未明确查询对象",no_current_records:"当前没有有效记录",not_recorded:"这项资料尚未提供",not_returned:"本次没有提供这项资料",unavailable:"这项资料暂时无法核对",forbidden:"当前身份无权查看",denied:"当前身份无权查看",conflict:"正式资料存在不一致",source_unavailable:"正式资料暂时无法读取",selection_unverified:"尚未核实区分条件"} as Record<string,string>)[status] ?? "资料情况尚待核对";
}
export function familyFieldValue(key:string,value:FamilyProfile["profile"][string]):string {
  if(value===null) return "这项资料尚未提供";
  if(typeof value==="object") return value.district_name;
  const labels:Record<string,Record<string,string>>={enrollment_status:{enrolled:"在校",active:"在校",graduated:"已毕业",transferred_out:"已转出",withdrawn:"已退学",suspended:"休学"},residence_mode:{day:"走读",boarding:"住宿",day_student:"走读",boarder:"住宿"}};
  return labels[key]?.[value] ?? value;
}
