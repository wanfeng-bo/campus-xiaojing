import { validFamilyFactSubject } from './family_contracts';
/** Authorized-child education projection; distinct from the F3 personal profile. */
export const educationFields = ['class','grade','teachers','classroom','positions','cohorts','official_contacts'] as const;
export type EducationField = typeof educationFields[number];
type Row = Record<string, unknown>;
export interface FamilyEducation {status:string;profile:Partial<Record<EducationField,unknown[]>>;authorized_fields:EducationField[];sources:Row[];as_of:string;coverage:string;quality?:Partial<Record<EducationField,{status:string;coverage:string;statements:Row[];source_availability?:string;freshness?:string}>>;fact_timezone_name?:string|null}
const obj=(v:unknown):v is Row=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const text=(v:unknown)=>typeof v==='string';
const nullableText=(v:unknown)=>v===null||text(v);
const only=(v:Row,keys:string[])=>Object.keys(v).every(k=>keys.includes(k));
const date=(v:unknown)=>typeof v==='string'&&Number.isFinite(Date.parse(v));
const states=['known','unknown','known_negative','conflict','source_unavailable'];
function item(field:string,v:unknown):boolean {
 if(['class','positions','cohorts'].includes(field))return nullableText(v);
 if(!obj(v))return false;
 if(field==='teachers')return only(v,['display_name','teacher_role'])&&nullableText(v.display_name)&&['primary','deputy','joint','acting',null].includes(v.teacher_role as string|null);
 if(field==='classroom')return only(v,['display_name','purpose'])&&nullableText(v.display_name)&&nullableText(v.purpose);
 if(field==='grade')return only(v,['status','display_name','reason','academic_year','next_boundary'])&&['resolved','unknown','conflict'].includes(String(v.status))&&nullableText(v.display_name);
 return field==='official_contacts'&&only(v,['owner_kind','contact_kind','contact_value','service_hours','contact_at'])&&text(v.contact_value)&&text(v.contact_kind);
}
export function isFamilyEducation(v:unknown):v is FamilyEducation {
 if(!obj(v)||!validFamilyFactSubject(v)||!only(v,['subject','status','profile','authorized_fields','sources','as_of','coverage','quality','fact_timezone_name'])||!['available','no_current_records','conflict'].includes(String(v.status))||!obj(v.profile)||!date(v.as_of)||v.coverage!=='unknown'||!Array.isArray(v.authorized_fields)||!v.authorized_fields.length||v.authorized_fields.length>7||!v.authorized_fields.every(f=>educationFields.includes(f))||new Set(v.authorized_fields).size!==v.authorized_fields.length||!Array.isArray(v.sources)||v.sources.length>200)return false;
 const authorized=v.authorized_fields;
 if(Object.entries(v.profile).some(([f,rows])=>!authorized.includes(f)||!Array.isArray(rows)||rows.length>100||!rows.every(r=>item(f,r))))return false;
 if(v.fact_timezone_name!==undefined&&!nullableText(v.fact_timezone_name))return false;
 if(!v.sources.every(s=>obj(s)&&only(s,['fact','field','source_key','source_kind','source_ref','batch_id','publication_reference','published_at','recorded_at','effective_at','valid_to','version','label','reference','summary'])&&text(s.fact)))return false;
 if(v.quality!==undefined){if(!obj(v.quality))return false;for(const [f,q]of Object.entries(v.quality)){
  if(!authorized.includes(f)||!obj(q)||!only(q,['status','coverage','statements','source_availability','freshness'])||!states.includes(String(q.status))||!['unknown','complete','partial'].includes(String(q.coverage))||!Array.isArray(q.statements)||q.statements.length>100)return false;
  if(!q.statements.every(s=>obj(s)&&only(s,['quality_kind','negative_code','coverage_status','dispute_status','source_status','review_due_at','verified_at','last_success_at','effective_at','valid_to','version','field_name','scope','academic_year'])&&['source','coverage','negative','dispute','review','correction'].includes(String(s.quality_kind))))return false;
 }}return true;
}
export const educationFieldLabels:Record<EducationField,string>={class:'班级',grade:'年级',teachers:'班主任',classroom:'教室',positions:'本人职务',cohorts:'届别',official_contacts:'官方联系'};
export function educationValue(field:EducationField,value:unknown):string {
 if(value===null)return '尚未明确';if(typeof value==='string')return value;if(!obj(value))return '尚未明确';
 if(field==='teachers')return `${value.display_name??'姓名尚未核实'} · ${{primary:'班主任',deputy:'副班主任',joint:'共同班主任',acting:'代任老师'}[String(value.teacher_role)]??'职责待核实'}`;
 if(field==='grade')return value.status==='resolved'?String(value.display_name??'年级名称尚未明确'):'个人年级安排待明确';
 if(field==='official_contacts')return `${value.contact_value}${value.service_hours?' · '+value.service_hours:''}`;
 return String(value.display_name??'尚未明确');
}
export function educationQuality(status:string, field:EducationField):string {
 if(status==='known_negative')return ({teachers:'学校明确未安排班主任',classroom:'学校明确暂无固定教室',positions:'学校明确所问岗位空缺'} as Partial<Record<EducationField,string>>)[field]??'学校已登记所问范围的明确否定';
 return ({conflict:'正式资料存在冲突，需核实',source_unavailable:'正式声明记载来源不可用',unknown:'尚无可提供的记录，不能据此认定未安排',known:'已取得所问资料'} as Record<string,string>)[status]??'尚未核实';
}
