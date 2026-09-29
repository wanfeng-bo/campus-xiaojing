export const fieldLabels: Record<string, string> = {
  course_ref:"课程",subject_ref:"学科",offering_ref:"开课",group_ref:"教学群体",term_ref:"学期",grade_group_ref:"届别",stage_refs:"适用学段",subject_refs:"任教学科限定",source_kind:"名单来源",offering_status:"开课状态",teaching_role:"任教角色",teaching_role_limits:"任教人数上限",group_mode:"群体共享方式",scope_basis:"组织范围依据",

  "display_name": "正式名称",
  "business_code": "业务编码",
  "short_name": "简称",
  "timezone_name": "时区",
  "lifecycle_status": "有效状态",
  "person_kind": "人员类别",
  "kind": "类别",
  "status": "关系状态",
  "person_ref": "人员",
  "organization_ref": "组织",
  "parent_ref": "上级对象",
  "campus_ref": "校区",
  "location_ref": "地点",
  "assignment_ref": "任职",
  "policy_ref": "制度",
  "official_contact_ref": "官方渠道",
  "publishing_organization_ref": "发布组织",
  "supersedes_policy_version_ref": "替代的制度版本",
  "position_title": "岗位名称",
  "semantic_role_code": "正式岗位语义",
  "assignment_kind": "任职性质",
  "appointment_source": "任命来源",
  "approval_reference": "批准依据",
  "display_path": "地点路径",
  "alias": "别名",
  "alias_kind": "别名用途",
  "aliases": "其他别名",
  "contact_kind": "联系类型",
  "contact_value": "联系方式",
  "is_primary": "是否正式主要项",
  "visibility_scope": "可见范围",
  "audience_code": "明确对象范围依据",
  "consent_reference": "个人联系依据",
  "owner_kind": "所属对象类型",
  "service_hours": "服务时间",
  "category_code": "制度分类",
  "classification": "正文可见级别",
  "published_at": "发布时间",
  "effective_from": "生效时间",
  "effective_to": "失效时间",
  "valid_to": "结束时间",
  "content_text": "完整制度原文",
  "version_note": "版本说明",
  "organization_type": "正式组织类型",
  "function_summary": "正式职能说明",
  "basis_reference": "正式依据",
  "membership_kind": "成员性质",
  "purpose": "地点用途",
  "audience_description": "服务对象",
  "service_summary": "服务说明",
  "quality_kind": "质量事项",
  "target_fact_kind": "对应事实类型",
  "target_ref": "对应事实对象",
  "replacement_ref": "纠正后的事实对象",
  "responsibility": "发布与核实责任",
  "verified_at": "核实时间",
  "evidence_reference": "证据依据",
  "coverage_status": "覆盖状态",
  "negative_code": "正式否定事项",
  "dispute_status": "争议状态",
  "source_status": "来源可用状态",
  "last_success_at": "最近成功取得来源时间",
  "review_due_at": "下次核实期限"
};

// The F3 Reader and descriptor-free proposal cards use the same public field names.
Object.assign(fieldLabels, {
  provider_code: "登录提供方", provider_tenant_key: "提供方租户", external_subject: "外部账号标识",
  student_ref: "学生", student_number: "学号", enrollment_status: "在校状态", residence_mode: "住校方式",
  primary_campus_ref: "主要校区", administrative_campus_ref: "行政校区", contact_person_ref: "联系人",
  relationship_kind: "关系类型", priority: "联系顺序", is_emergency: "紧急联系人",
  guardian_person_ref: "家长或监护人", access_purpose: "家校访问用途",
  verification_source: "核验来源", verification_reference: "核验依据",
  review_of_relationship_ref: "被纠正家校关系", district_code: "区县代码", district_name: "区县名称",
  region_purpose: "地区用途",
});

const familyFieldLabels: Record<string, Record<string, string>> = {
  student: { status: "学生状态" },
  student_contact: { relationship_kind: "与学生关系" },
  guardian_relationship: { status: "家校关系状态", relationship_kind: "家校关系类型" },
  login_mapping: { status: "登录关联状态" },
  assignment: { status: "任职状态" },
};

/** Match describe metadata where identical field names belong to different objects. */
export function directoryFieldLabel(factKind: string, name: string): string {
  if (factKind === "organization" && name === "display_path") return "组织路径";
  return familyFieldLabels[factKind]?.[name] ?? fieldLabels[name] ?? name;
}

const fieldValueLabels: Record<string, string> = {
  active: "有效", inactive: "已停用", ended: "已结束", revoked: "已撤销",
  staff: "教职工", student: "学生", guardian: "家长", external: "校外人员",
  campus: "校区", building: "楼宇", floor: "楼层", room: "房间", area: "区域",
  primary: "主要", secondary: "兼任", temporary: "临时",
  public: "公开", staff_only: "仅教职工", restricted: "受限",
};
const specificValueLabels: Record<string, Record<string, string>> = {
  source_kind:{administrative_class:"行政班派生名单",independent_group:"独立教学名单"},offering_status:{active:"正常开课",suspended:"暂停开课"},teaching_role:{lead:"主讲",co_teacher:"共同任教"},group_mode:{exclusive:"单开课专用",shared:"学校明确共享"},scope_basis:{stage:"学段",fixed_grade:"固定年级",academic_grade:"届别"},

  negative_code: { no_teaching_assignment:"明确未安排此角色任教", no_class_teacher:"明确未安排班主任", no_fixed_location:"明确没有固定教室",vacant_position:"明确岗位空缺" },
  owner_kind: { education: "教育对象", school: "学校", organization: "组织", person: "人员", location: "地点" },
  contact_kind: { phone: "电话", mobile: "手机", email: "电子邮箱" },
  status: { active: "有效", inactive: "已停用", merged: "已合并", draft: "草稿", confirmed: "已确认", rejected: "已拒绝", ended: "已结束", revoked: "已撤销", pending_verification: "待核验" },
  person_kind: { staff: "教职工", student: "学生", guardian: "家长", external: "校外人员", student_contact: "学生联系人", other: "其他" },
  assignment_kind: { primary: "主要", secondary: "兼任", temporary: "临时", substantive: "正式任职", part_time: "兼职", acting: "临时代理" },
  enrollment_status: { enrolled: "在读", suspended: "休学", transferred_out: "已转出", withdrawn: "已退学", graduated: "已毕业" },
  residence_mode: { day: "走读", boarding: "住校" },
  relationship_kind: { father: "父亲", mother: "母亲", grandfather: "祖父或外祖父", grandmother: "祖母或外祖母", other: "其他", verified_guardian: "已核实监护人", recognized_caregiver: "已认定照护人" },
  access_purpose: { guardian_student_profile: "监护人学生档案", caregiver_student_summary: "照护人学生摘要" },
  region_purpose: { student_profile: "学生档案" },
};
const enumFields = new Set(["source_kind","offering_status","teaching_role","group_mode","scope_basis",
  "lifecycle_status", "person_kind", "kind", "status", "assignment_kind",
  "alias_kind", "contact_kind", "visibility_scope", "owner_kind",
  "negative_code", "classification", "membership_kind", "quality_kind", "coverage_status",
  "dispute_status", "source_status",
  "enrollment_status", "residence_mode", "relationship_kind", "access_purpose", "region_purpose",
]);

/** Directory wire enums remain stable internally but teacher-facing values are localized. */
export function directoryFieldValue(name: string, value: unknown): unknown {
  if (!enumFields.has(name)) return value;
  const labels = specificValueLabels[name] ?? fieldValueLabels;
  if (typeof value === "string" && labels[value]) return labels[value];
  if (Array.isArray(value)) return value.map(item => typeof item === "string" ? (labels[item] ?? item) : item);
  return value;
}
