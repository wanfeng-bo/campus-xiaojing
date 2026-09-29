export interface CorrectionOption {fact_kind?:string|null;field_name:string;label:string;reference:string}
export interface CorrectionReceipt {
  kind:'family_fact_correction_receipt';report_id:string;fact_kind?:string|null;status:'submitted'|'resolved'|'reviewed';field_name:string;submitted_at:string;resolved_at?:string|null;review_explanation?:string|null;
}
const object=(v:unknown):v is Record<string,unknown>=>typeof v==='object'&&v!==null&&!Array.isArray(v);
export const correctionFieldsByKind:Record<string,string[]>={
 student_bed_residence:['bed_source_key','effective_at','valid_to'],dorm_bed:['display_name','room_source_key'],dorm_room:['room_source_key'],dormitory_building_profile:['display_name','location_source_key'],
 person:['name','display_name'],person_region:['region'],student_school_relationship:['enrollment_status','campus','student_number','residence_mode'],guardian_relationship:['relationship'],
 student_class_membership:['class_source_key'],student_grade_placement:['grade_source_key'],class_placement:['grade_source_key'],class_teacher_assignment:['assignment_source_key'],class_location:['location_source_key'],student_class_position:['position_source_key'],student_cohort_membership:['cohort_source_key'],
 assignment:['position_title','person_source_key','organization_source_key'],official_contact:['contact_value','contact_kind','service_hours'],
 teaching_subject:['display_name','stage_source_keys'],teaching_course:['display_name','stage_source_keys','grade_source_keys'],course_subject:['course_source_key','subject_source_key'],
 teaching_offering:['course_source_key','offering_status','display_name','campus_source_key','term_source_key'],offering_member_source:['offering_source_key','source_kind','class_source_key','group_source_key'],
 teaching_group:['organization_source_key','group_mode'],teaching_group_member:['group_source_key','student_source_key','school_relationship_source_key','valid_to'],
 teaching_assignment:['offering_source_key','assignment_source_key','teaching_role','subject_source_keys','duty_scope'],
 teaching_organization_scope:['organization_source_key','subject_source_key','scope_basis','stage_source_key','grade_source_key','grade_group_source_key','term_source_key']
};
export const correctionFields=[...new Set(Object.values(correctionFieldsByKind).flat())];
export const correctionFieldLabels:Record<string,string>={
 bed_source_key:'住宿床位',room_source_key:'住宿房间',effective_at:'安排生效时间',
 name:'孩子姓名',enrollment_status:'在校状态',campus:'当前校区',student_number:'学号',residence_mode:'走读或住宿',region:'居住地区',relationship:'孩子关联',
 class_source_key:'孩子班级',grade_source_key:'孩子年级安排',assignment_source_key:'教师任职',location_source_key:'班级教室',position_source_key:'孩子班内职务',cohort_source_key:'孩子届别归属',
 display_name:'正式名称',position_title:'领导职务',person_source_key:'任职人员',organization_source_key:'所属组织',contact_value:'官方联系渠道',contact_kind:'渠道类型',service_hours:'服务时间',
 course_source_key:'课程',subject_source_key:'学科',offering_source_key:'开课',group_source_key:'教学群体',teaching_role:'任教职责',offering_status:'开课状态',source_kind:'名单来源',
 stage_source_keys:'适用学段',grade_source_keys:'适用年级',campus_source_key:'开课校区',term_source_key:'适用学期',group_mode:'群体使用范围',student_source_key:'成员学生',school_relationship_source_key:'在校关系',
 subject_source_keys:'任教学科',duty_scope:'任教分工',scope_basis:'适用口径',stage_source_key:'适用学段',grade_group_source_key:'学年年级',valid_to:'成员有效截止时间'
};
export function correctionReceiptMatches(target:unknown,reference:string|undefined,field:string|undefined,action:string|undefined):boolean {
 if(!object(target)||!reference||target.reference!==reference||typeof target.operation!=='string')return false;
 if(action!=='maintain')return target.operation===action;
 if(['student_bed_residence','dorm_bed','dorm_room','dormitory_building_profile'].includes(String(target.fact_kind)))return (correctionFieldsByKind[String(target.fact_kind)]??[]).includes(field??'')&&['upsert','correct','end','revoke'].includes(target.operation);
 const schoolTeaching=/^schoolv1:(?:f4|f5):([a-z][a-z0-9_]{0,63}):o:[A-Za-z0-9_-]+:[0-9a-f]{64}$/.exec(reference);
 if(reference.startsWith('f4:')||reference.startsWith('f5:')||schoolTeaching){
  const kind=schoolTeaching?.[1]??reference.split(':')[1]??'';
  return target.fact_kind===kind&&(correctionFieldsByKind[kind]??[]).includes(field??'')&&['upsert','correct','cancel','end'].includes(target.operation);
 }
 if((target.fact_kind==='person'&&field==='display_name')||target.fact_kind==='assignment'||target.fact_kind==='official_contact')return target.operation==='upsert'&&(correctionFieldsByKind[String(target.fact_kind)]??[]).includes(field??'');
 return ['upsert','reactivate'].includes(target.operation);
}
export function isCorrectionOption(v:unknown):v is CorrectionOption {
  return object(v)&&(v.fact_kind===undefined||v.fact_kind===null||typeof v.fact_kind==='string')&&correctionFields.includes(String(v.field_name))&&typeof v.reference==='string'&&v.reference.length>=8&&v.reference.length<=1600&&typeof v.label==='string'&&v.label.length>0&&v.label.length<=800;
}
export function isCorrectionReceipt(v:unknown):v is CorrectionReceipt {
  return object(v)&&(v.fact_kind===undefined||v.fact_kind===null||(typeof v.fact_kind==='string'&&v.fact_kind.length>0&&v.fact_kind.length<=64))&&v.kind==='family_fact_correction_receipt'&&typeof v.report_id==='string'&&v.report_id.length>0&&['submitted','resolved','reviewed'].includes(String(v.status))&&correctionFields.includes(String(v.field_name))&&typeof v.submitted_at==='string'&&Number.isFinite(Date.parse(v.submitted_at))&&(v.resolved_at===undefined||v.resolved_at===null||(typeof v.resolved_at==='string'&&Number.isFinite(Date.parse(v.resolved_at))))&&(v.status!=='reviewed'||typeof v.review_explanation==='string'&&v.review_explanation.trim().length>0&&v.review_explanation.length<=1000&&typeof v.resolved_at==='string');
}

export function correctionOptionLabel(option:CorrectionOption):string {
 const owned = option.fact_kind==='dormitory_building_profile'&&option.field_name==='location_source_key'?'所属位置':option.fact_kind==='student_bed_residence'&&option.field_name==='valid_to'?'安排结束时间':undefined;
 return option.label+' · '+(owned??correctionFieldLabels[option.field_name]??option.field_name);
}

export interface CorrectionHistory {kind:'family_correction_history';status:'available';coverage:'limited_page';scope:string;meaning:string;items:Array<CorrectionReceipt & {target_label:string;field_label:string;status_label:string;status_meaning:string}>}
export function isCorrectionHistory(value:unknown):value is CorrectionHistory {
 return object(value)&&value.kind==='family_correction_history'&&value.status==='available'&&value.coverage==='limited_page'&&typeof value.scope==='string'&&typeof value.meaning==='string'&&Array.isArray(value.items)&&value.items.length<=20&&value.items.every(row=>isCorrectionReceipt(row)&&object(row)&&['target_label','field_label','status_label','status_meaning'].every(k=>typeof row[k]==='string'));
}
