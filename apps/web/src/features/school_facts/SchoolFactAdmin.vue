<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from "vue";

import {
  schoolFactOperationsForKind,
  type SchoolFactAdminChange,
  type SchoolFactAdminValue,
  type SchoolFactContentField,
  type SchoolFactFieldValue,
  type SchoolFactKind,
  type SchoolFactOperation,
} from "./school_fact_contracts";
import {
  createSchoolFactSessionController,
  type SchoolFactSessionState,
} from "./school_fact_session";


type AdminFieldType = "boolean" | "datetime" | "list" | "number" | "select" | "text";

interface AdminFieldDescriptor {
  code: SchoolFactContentField;
  label: string;
  type: AdminFieldType;
  required: boolean;
  options?: readonly string[];
}

interface AdaptedDraft {
  id: number;
  source_slot: string;
  file: File | null;
}

type InputMode = "excel" | "adapted" | "admin";

const MAX_ADMIN_DRAFTS = 100;
const MAX_ADAPTED_SOURCES = 8;
const PLAN_PAGE_SIZE = 100;


const FACT_KINDS: ReadonlyArray<{ value: SchoolFactKind; label: string }> = [
  { value: "school", label: "学校" },
  { value: "person", label: "人员" },
  { value: "student", label: "学生" },
  { value: "student_school_relationship", label: "学生在校状态" },
  { value: "login_mapping", label: "登录映射" },
  { value: "organization", label: "组织" },
  { value: "organization_campus_relationship", label: "组织校区关系" },
  { value: "assignment", label: "任职" },
  { value: "location", label: "地点" },
  { value: "location_alias", label: "地点别名" },
  { value: "dorm_room", label: "宿舍房间属性" },
  { value: "dormitory_building_profile", label: "宿舍楼或住宿区域用途" },
  { value: "dorm_bed", label: "宿舍床位" },
  { value: "student_bed_residence", label: "学生入住" },
  { value: "person_contact", label: "个人联系方式" },
  { value: "student_contact", label: "学生联系人" },
  { value: "official_contact", label: "官方联系方式" },
  { value: "policy", label: "制度目录" },
  { value: "policy_scope", label: "制度适用范围" },
  { value: "policy_version", label: "制度版本" },
];

const FIELD_SETS: Record<SchoolFactKind, readonly AdminFieldDescriptor[]> = {
  school: [
    field("business_code", "学校编号", "text", true),
    field("display_name", "学校名称", "text", true),
    field("short_name", "学校简称", "text"),
    field("timezone_name", "时区", "text", true),
    select_field("lifecycle_status", "状态", ["active", "inactive"], true),
  ],
  person: [
    field("business_code", "人员业务编号", "text"),
    field("display_name", "姓名", "text", true),
    select_field("person_kind", "人员类别", ["staff", "student_contact", "other"], true),
    select_field("lifecycle_status", "状态", ["active", "inactive"], true),
  ],
  student: [
    field("person_source_key", "对应人员稳定标识", "text", true),
    select_field("status", "学生身份状态", ["active", "inactive", "merged"], true),
  ],
  student_school_relationship: [
    field("student_source_key", "学生稳定标识", "text", true),
    field("student_number", "学号", "text", true),
    select_field(
      "enrollment_status",
      "在校状态",
      ["enrolled", "suspended", "transferred_out", "withdrawn", "graduated"],
      true,
    ),
    select_field("residence_mode", "居住方式（unknown表示明确未知，空白不清除）", ["day", "boarding", "unknown"], true),
    field("primary_campus_source_key", "主要校区稳定标识", "text", true),
    field("administrative_campus_source_key", "行政校区稳定标识", "text", true),
  ],
  login_mapping: [
    field("provider_code", "登录平台代码", "text", true),
    field("provider_tenant_key", "平台学校标识", "text", true),
    field("external_subject", "平台账号标识", "text", true),
    field("person_source_key", "人员稳定标识", "text", true),
    select_field("status", "映射状态", ["draft", "confirmed", "rejected"], true),
  ],
  organization: [
    field("parent_source_key", "上级组织稳定标识", "text"),
    field("kind", "组织类别", "text", true),
    field("business_code", "组织编号", "text"),
    field("display_name", "组织名称", "text", true),
    field("short_name", "组织简称", "text"),
    field("display_path", "组织显示路径", "text", true),
    select_field("status", "状态", ["active", "inactive"], true),
  ],
  organization_campus_relationship: [
    field("organization_source_key", "组织稳定标识", "text", true),
    field("campus_source_key", "校区稳定标识", "text", true),
    field("valid_to", "结束时间", "datetime"),
  ],
  assignment: [
    field("person_source_key", "人员稳定标识", "text", true),
    field("organization_source_key", "组织稳定标识", "text", true),
    field("position_title", "岗位名称", "text", true),
    field("semantic_role_code", "岗位语义代码", "text", true),
    select_field("assignment_kind", "任职类别", ["substantive", "part_time", "acting"], true),
    field("appointment_source", "任职来源", "text", true),
    field("approval_reference", "任职依据", "text", true),
    select_field("status", "状态", ["draft", "confirmed", "rejected"], true),
    field("valid_to", "计划结束时间", "datetime"),
  ],
  location: [
    field("parent_source_key", "上级地点稳定标识", "text"),
    field("kind", "地点类别", "text", true),
    field("business_code", "地点编号", "text"),
    field("display_name", "地点名称", "text", true),
    field("display_path", "地点显示路径", "text", true),
    field("aliases", "常用别名（逗号分隔）", "list"),
    select_field("status", "状态", ["active", "inactive"], true),
  ],
  location_alias: [
    field("location_source_key", "地点稳定标识", "text", true),
    field("alias", "别名", "text", true),
    select_field("alias_kind", "别名类别", ["former", "common", "short"], true),
  ],
  dorm_room: [
    field("room_source_key", "房间地点稳定标识", "text", true),
    field("authorized_bed_capacity", "核定床位数", "number", true),
    field("accommodation_zone", "住宿分区", "text", true),
    field("is_available", "房间可用", "boolean", true),
    field("accessibility_tags", "无障碍标签（逗号分隔）", "list"),
  ],
  dormitory_building_profile: [
    field("location_source_key", "地点稳定标识", "text", true),
    select_field("usage_kind", "用途类别", ["building", "residential_area"], true),
    field("display_name", "名称", "text", true),
    field("description", "说明", "text"),
    field("is_available", "是否可用", "boolean", true),
    field("valid_to", "结束时间", "datetime"),
  ],
  dorm_bed: [
    field("room_source_key", "房间稳定标识", "text", true),
    field("business_code", "床位编号", "text", true),
    field("display_name", "床位名称", "text", true),
    select_field("availability", "床位可用状态", ["available", "unavailable", "inactive"], true),
  ],
  student_bed_residence: [
    field("student_source_key", "学生稳定标识", "text", true),
    field("bed_source_key", "床位稳定标识", "text", true),
    field("valid_to", "结束时间", "datetime"),
  ],
  person_contact: [
    field("person_source_key", "人员稳定标识", "text", true),
    select_field("contact_kind", "联系方式类别", ["mobile", "phone", "email"], true),
    field("contact_value", "联系方式", "text", true),
    field("is_primary", "是否主要联系方式", "boolean", true),
    select_field(
      "visibility_scope",
      "可见范围",
      ["private", "school_authenticated", "declared_audience"],
      true,
    ),
    field("audience_code", "指定受众代码", "text"),
    field("consent_reference", "可选同意记录", "text"),
  ],
  student_contact: [
    field("student_source_key", "学生稳定标识", "text", true),
    field("contact_person_source_key", "联系人稳定标识", "text", true),
    select_field(
      "relationship_kind",
      "自然关系",
      ["father", "mother", "grandfather", "grandmother", "other"],
      true,
    ),
    field("priority", "联系顺序", "number", true),
    field("is_emergency", "是否紧急联系人", "boolean", true),
  ],
  official_contact: [
    select_field("owner_kind", "资料归属", ["school", "organization", "location"], true),
    field("organization_source_key", "组织稳定标识", "text"),
    field("location_source_key", "地点稳定标识", "text"),
    select_field("contact_kind", "联系方式类别", ["phone", "email"], true),
    field("contact_value", "联系方式", "text", true),
    field("service_hours", "服务时间", "text"),
    select_field("lifecycle_status", "状态", ["active", "inactive"], true),
    select_field(
      "visibility_scope",
      "可见范围",
      ["public", "school_authenticated", "restricted"],
      true,
    ),
    field("audience_code", "指定受众代码", "text"),
  ],
  policy: [],
  policy_scope: [
    field("policy_source_key", "制度目录稳定标识", "text", true),
    field("organization_source_key", "组织稳定标识", "text"),
    field("campus_source_key", "校区稳定标识", "text"),
    field("valid_to", "结束时间", "datetime"),
  ],
  policy_version: [
    field("policy_source_key", "制度目录稳定标识", "text", true),
    field("business_code", "制度版本编号", "text", true),
    field("display_name", "制度版本名称", "text", true),
    field("category_code", "制度类别", "text", true),
    select_field("classification", "分级", ["public", "internal", "restricted"], true),
    field("publishing_organization_source_key", "发布组织稳定标识", "text"),
    field("published_at", "发布时间", "datetime", true),
    field("effective_from", "生效时间", "datetime", true),
    field("effective_to", "失效时间", "datetime"),
    field("content_text", "制度正文", "text"),
    field("version_note", "版本说明", "text"),
    field("supersedes_policy_version_source_key", "替代版本稳定标识", "text"),
  ],
};


const controller = createSchoolFactSessionController();
const state = shallowRef<Readonly<SchoolFactSessionState>>(controller.getState());
const unsubscribe = controller.subscribe((next_state) => {
  state.value = next_state;
});

const mode = ref<InputMode>("excel");
const form_error = ref<string | null>(null);
const excel_base_version = ref(1);
const formal_file = ref<File | null>(null);
const formal_file_key = ref(0);
const adapted_base_version = ref(1);
const mapping_version = ref("");
const adapted_files = ref<AdaptedDraft[]>([
  { id: 1, source_slot: "", file: null },
]);
const adapted_file_key = ref(0);
let next_adapted_id = 2;
const admin_base_version = ref(1);
const admin_operation = ref<SchoolFactOperation>("upsert");
const admin_fact_kind = ref<SchoolFactKind>("school");
const admin_source_key = ref("");
const admin_expected_version = ref(0);
const admin_effective_at = ref("");
const admin_field_values = ref<Partial<Record<SchoolFactContentField, string>>>({});
const admin_drafts = ref<SchoolFactAdminChange[]>([]);
const issue_page = ref(0);
const change_page = ref(0);
const status_page = ref(0);

const admin_fields = computed(() => FIELD_SETS[admin_fact_kind.value]);
const admin_operations = computed(() => schoolFactOperationsForKind(admin_fact_kind.value));
const write_busy = computed(() => state.value.busy_write !== null);
const unresolved_formal_action = computed(() =>
  state.value.confirmation_locked && state.value.result?.state !== "applied"
);
const visible_issues = computed(() => state.value.plan?.issues.slice(
  issue_page.value * PLAN_PAGE_SIZE,
  (issue_page.value + 1) * PLAN_PAGE_SIZE,
) ?? []);
const visible_changes = computed(() => state.value.plan?.changes.slice(
  change_page.value * PLAN_PAGE_SIZE,
  (change_page.value + 1) * PLAN_PAGE_SIZE,
) ?? []);
const visible_status_decisions = computed(() => state.value.plan?.status_decisions.slice(
  status_page.value * PLAN_PAGE_SIZE,
  (status_page.value + 1) * PLAN_PAGE_SIZE,
) ?? []);
const issue_page_count = computed(() => page_count(state.value.plan?.issues.length ?? 0));
const change_page_count = computed(() => page_count(state.value.plan?.changes.length ?? 0));
const status_page_count = computed(() => page_count(
  state.value.plan?.status_decisions.length ?? 0,
));

watch(() => state.value.plan?.plan_ref, () => {
  issue_page.value = 0;
  change_page.value = 0;
  status_page.value = 0;
});


function field(
  code: SchoolFactContentField,
  label: string,
  type: AdminFieldType,
  required = false,
): AdminFieldDescriptor {
  return { code, label, required, type };
}


function select_field(
  code: SchoolFactContentField,
  label: string,
  options: readonly string[],
  required = false,
): AdminFieldDescriptor {
  return { code, label, options, required, type: "select" };
}


function select_mode(next_mode: InputMode): void {
  if (mode.value === "excel" && next_mode !== "excel") {
    clear_formal_file();
  }
  if (mode.value === "adapted" && next_mode !== "adapted") {
    clear_adapted_files();
  }
  if (mode.value === "admin" && next_mode !== "admin") {
    clear_admin_drafts();
  }
  form_error.value = null;
  mode.value = next_mode;
}


function choose_formal_file(event: Event): void {
  formal_file.value = (event.target as HTMLInputElement).files?.[0] ?? null;
}


function choose_adapted_file(index: number, event: Event): void {
  const item = adapted_files.value[index];
  if (item !== undefined) {
    item.file = (event.target as HTMLInputElement).files?.[0] ?? null;
  }
}


function add_adapted_source(): void {
  if (adapted_files.value.length < MAX_ADAPTED_SOURCES && !write_busy.value) {
    adapted_files.value.push({ id: next_adapted_id, source_slot: "", file: null });
    next_adapted_id += 1;
  }
}


function remove_adapted_source(index: number): void {
  if (adapted_files.value.length > 1 && !write_busy.value) {
    adapted_files.value.splice(index, 1);
  }
}


async function preflight_excel(): Promise<void> {
  const file = formal_file.value;
  if (file === null) {
    form_error.value = "请选择一个 .xlsx 文件。";
    return;
  }
  form_error.value = null;
  if (await controller.preflightExcelFromClick(file, excel_base_version.value)) {
    clear_formal_file();
  }
}


async function preflight_adapted(): Promise<void> {
  const sources = adapted_files.value.flatMap((item) =>
    item.file === null ? [] : [{ source_slot: item.source_slot.trim(), file: item.file }],
  );
  if (sources.length !== adapted_files.value.length || !mapping_version.value.trim()) {
    form_error.value = "请为每份来源表选择文件、填写来源槽，并选择映射版本。";
    return;
  }
  form_error.value = null;
  if (await controller.preflightAdaptedFromClick(
    sources,
    mapping_version.value.trim(),
    adapted_base_version.value,
  )) {
    clear_adapted_files();
  }
}


function change_fact_kind(event: Event): void {
  admin_fact_kind.value = (event.target as HTMLSelectElement).value as SchoolFactKind;
  if (!schoolFactOperationsForKind(admin_fact_kind.value).includes(admin_operation.value)) {
    admin_operation.value = "upsert";
  }
  admin_field_values.value = {};
}


function change_operation(event: Event): void {
  admin_operation.value = (event.target as HTMLSelectElement).value as SchoolFactOperation;
  if (!["upsert", "reactivate"].includes(admin_operation.value)) {
    admin_field_values.value = {};
  }
}


function update_admin_field(code: SchoolFactContentField, event: Event): void {
  admin_field_values.value = {
    ...admin_field_values.value,
    [code]: (event.target as HTMLInputElement | HTMLSelectElement).value,
  };
}


function add_admin_change(): void {
  if (write_busy.value || admin_drafts.value.length >= MAX_ADMIN_DRAFTS) {
    return;
  }
  const change = build_admin_change();
  if (change === null) {
    return;
  }
  const existing_reactivation = admin_drafts.value.some(
    (draft) => draft.operation === "reactivate",
  );
  if (
    (change.operation === "reactivate" && admin_drafts.value.length > 0) ||
    (change.operation !== "reactivate" && existing_reactivation)
  ) {
    form_error.value = "重新启用必须作为独立变更提交。";
    return;
  }
  form_error.value = null;
  admin_drafts.value.push(change);
  clear_admin_form();
}


function remove_admin_change(index: number): void {
  if (!write_busy.value) {
    admin_drafts.value.splice(index, 1);
  }
}


async function preflight_admin(): Promise<void> {
  if (admin_drafts.value.length < 1) {
    form_error.value = "请先把一项或少量结构化变更加入本批次。";
    return;
  }
  form_error.value = null;
  const changes = admin_drafts.value.slice();
  if (await controller.preflightAdminFromClick(admin_base_version.value, changes)) {
    clear_admin_drafts();
  }
}


function build_admin_change(): SchoolFactAdminChange | null {
  const source_key = admin_source_key.value.trim();
  const effective_at = normalize_datetime(admin_effective_at.value);
  if (!source_key || effective_at === null) {
    form_error.value = "请填写稳定标识和有效的生效时间。";
    return null;
  }
  let content: SchoolFactAdminChange["content"] = null;
  if (admin_operation.value === "upsert" || admin_operation.value === "reactivate") {
    const built: Partial<Record<SchoolFactContentField, SchoolFactAdminValue>> = {};
    for (const descriptor of admin_fields.value) {
      const raw = admin_field_values.value[descriptor.code]?.trim() ?? "";
      if (!raw) {
        if (descriptor.required) {
          form_error.value = `请填写${descriptor.label}。`;
          return null;
        }
        continue;
      }
      const converted = convert_admin_value(descriptor, raw);
      if (converted === undefined) {
        form_error.value = `${descriptor.label}格式不正确。`;
        return null;
      }
      built[descriptor.code] = converted;
    }
    content = built;
  }
  return {
    content,
    ...(['dormitory_building_profile','dorm_room','dorm_bed','student_bed_residence'].includes(admin_fact_kind.value)||content?.residence_mode===null?{schema_version:4 as const}:{}),
    effective_at,
    expected_version: admin_expected_version.value,
    fact_kind: admin_fact_kind.value,
    operation: admin_operation.value,
    source_key,
  };
}


function convert_admin_value(
  descriptor: AdminFieldDescriptor,
  raw: string,
): SchoolFactAdminValue | undefined {
  if(descriptor.code==='residence_mode'&&raw==='unknown')return null;
  if (descriptor.type === "boolean") {
    return raw === "true" ? true : raw === "false" ? false : undefined;
  }
  if (descriptor.type === "number") {
    const value = Number(raw);
    return Number.isInteger(value) ? value : undefined;
  }
  if (descriptor.type === "list") {
    const values = raw.split(",").map((item) => item.trim()).filter(Boolean);
    return values.length > 0 ? values : undefined;
  }
  if (descriptor.type === "datetime") {
    return normalize_datetime(raw) ?? undefined;
  }
  return raw;
}


function normalize_datetime(value: string): string | null {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    return null;
  }
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null;
}


function page_count(length: number): number {
  return Math.max(1, Math.ceil(length / PLAN_PAGE_SIZE));
}


function format_field_value(value: SchoolFactFieldValue): string {
  if(value===null)return '未知（明确清除原值）';
  return Array.isArray(value) ? value.join("、") : String(value);
}


function clear_formal_file(): void {
  formal_file.value = null;
  formal_file_key.value += 1;
}


function clear_adapted_files(): void {
  adapted_files.value = [{ id: next_adapted_id, source_slot: "", file: null }];
  next_adapted_id += 1;
  adapted_file_key.value += 1;
}


function clear_admin_form(): void {
  admin_field_values.value = {};
  admin_source_key.value = "";
  admin_expected_version.value = 0;
  admin_effective_at.value = "";
}


function clear_admin_drafts(): void {
  admin_drafts.value = [];
  clear_admin_form();
}


onUnmounted(() => {
  clear_formal_file();
  clear_adapted_files();
  clear_admin_drafts();
  unsubscribe();
  controller.dispose();
});
</script>

<template>
  <section class="school-fact-admin" aria-label="学校基础数据管理台">
    <header>
      <h2>学校基础数据</h2>
      <p>先预检并核对差异；只有下方正式按钮可以写入。</p>
    </header>

    <nav class="mode-tabs" aria-label="数据准备方式">
      <button type="button" data-mode="excel" :aria-pressed="mode === 'excel'" @click="select_mode('excel')">
        标准 Excel
      </button>
      <button type="button" data-mode="adapted" :aria-pressed="mode === 'adapted'" @click="select_mode('adapted')">
        学校现有表格
      </button>
      <button type="button" data-mode="admin" :aria-pressed="mode === 'admin'" @click="select_mode('admin')">
        结构化管理变更
      </button>
    </nav>

    <section v-if="mode === 'excel'" class="input-panel" aria-label="标准 Excel 预检">
      <label>
        当前学校事实版本
        <input v-model.number="excel_base_version" data-role="base-version" type="number" min="1" step="1">
      </label>
      <label>
        标准工作簿
        <input
          :key="formal_file_key"
          data-role="formal-file"
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          @change="choose_formal_file"
        >
      </label>
      <p v-if="formal_file !== null" class="selected-file">已选择：{{ formal_file.name }}</p>
      <button
        type="button"
        data-action="preflight-excel"
        :disabled="write_busy || formal_file === null"
        @click="preflight_excel"
      >
        {{ state.busy_write === "preflight_excel" ? "正在预检……" : "上传并预检" }}
      </button>
    </section>

    <section v-else-if="mode === 'adapted'" class="input-panel" aria-label="来源映射预检">
      <label>
        当前学校事实版本
        <input v-model.number="adapted_base_version" type="number" min="1" step="1">
      </label>
      <label>
        已登记映射版本
        <input v-model="mapping_version" data-role="mapping-version" type="text" maxlength="120">
      </label>
      <p>每批最多 8 份来源表，单份不超过 10 MiB，总计不超过 32 MiB。</p>
      <fieldset v-for="(item, index) in adapted_files" :key="`${item.id}:${adapted_file_key}`">
        <legend>来源表 {{ index + 1 }}</legend>
        <label>
          来源槽
          <input v-model="item.source_slot" :data-role="`adapted-slot-${index}`" type="text" maxlength="64">
        </label>
        <label>
          工作簿
          <input
            :data-role="`adapted-file-${index}`"
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            @change="choose_adapted_file(index, $event)"
          >
        </label>
        <span v-if="item.file !== null">{{ item.file.name }}</span>
        <button
          v-if="adapted_files.length > 1"
          type="button"
          :disabled="write_busy"
          @click="remove_adapted_source(index)"
        >
          移除这份表
        </button>
      </fieldset>
      <div class="button-row">
        <button
          type="button"
          data-action="add-adapted-source"
          :disabled="write_busy || adapted_files.length >= MAX_ADAPTED_SOURCES"
          @click="add_adapted_source"
        >
          再加一份来源表
        </button>
        <button
          type="button"
          data-action="preflight-adapted"
          :disabled="write_busy"
          @click="preflight_adapted"
        >
          {{ state.busy_write === "preflight_adapted" ? "正在预检……" : "按选定映射预检" }}
        </button>
      </div>
    </section>

    <section v-else class="input-panel" aria-label="结构化管理变更">
      <label>
        当前学校事实版本
        <input v-model.number="admin_base_version" type="number" min="1" step="1">
      </label>
      <label>
        操作
        <select :value="admin_operation" data-role="admin-operation" @change="change_operation">
          <option
            v-for="operation in admin_operations"
            :key="operation"
            :value="operation"
          >
            {{
              operation === "upsert"
                ? "新增或更新"
                : operation === "end"
                  ? "结束有效关系"
                  : operation === "revoke"
                    ? "撤销"
                    : "重新启用"
            }}
          </option>
        </select>
      </label>
      <label>
        事实类别
        <select :value="admin_fact_kind" data-role="admin-fact-kind" @change="change_fact_kind">
          <option v-for="item in FACT_KINDS" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
      </label>
      <label>
        稳定标识（不能用显示名代替）
        <input v-model="admin_source_key" data-role="admin-source-key" type="text" maxlength="255">
      </label>
      <label>
        预期版本
        <input v-model.number="admin_expected_version" data-role="admin-expected-version" type="number" min="0" step="1">
      </label>
      <label>
        生效时间（必须带时区偏移，例如 2026-08-21T09:00:00+08:00）
        <input
          v-model="admin_effective_at"
          data-role="admin-effective-at"
          type="text"
          maxlength="64"
          placeholder="2026-08-21T09:00:00+08:00"
        >
      </label>

      <fieldset v-if="admin_operation === 'upsert' || admin_operation === 'reactivate'">
        <legend>规范字段</legend>
        <p v-if="admin_fields.length === 0">制度目录本身没有额外内容字段。</p>
        <label v-for="descriptor in admin_fields" :key="descriptor.code">
          {{ descriptor.label }}{{ descriptor.required ? "（必填）" : "" }}
          <select
            v-if="descriptor.type === 'select' || descriptor.type === 'boolean'"
            :value="admin_field_values[descriptor.code] ?? ''"
            :data-field-code="descriptor.code"
            @change="update_admin_field(descriptor.code, $event)"
          >
            <option value="">请选择</option>
            <option
              v-for="option in descriptor.type === 'boolean' ? ['true', 'false'] : descriptor.options"
              :key="option"
              :value="option"
            >
              {{ option }}
            </option>
          </select>
          <input
            v-else
            :value="admin_field_values[descriptor.code] ?? ''"
            :data-field-code="descriptor.code"
            :type="descriptor.type === 'number' ? 'number' : 'text'"
            :placeholder="descriptor.type === 'datetime' ? '必须带时区偏移，如 2026-08-21T09:00:00+08:00' : undefined"
            @input="update_admin_field(descriptor.code, $event)"
          >
        </label>
      </fieldset>
      <div class="button-row">
        <button
          type="button"
          data-action="add-admin-change"
          :disabled="write_busy || admin_drafts.length >= MAX_ADMIN_DRAFTS"
          @click="add_admin_change"
        >
          加入同一批次
        </button>
        <button
          type="button"
          data-action="preflight-admin"
          :disabled="write_busy || admin_drafts.length === 0"
          @click="preflight_admin"
        >
          {{ state.busy_write === "preflight_admin" ? "正在预检……" : `预检本批 ${admin_drafts.length} 项变更` }}
        </button>
      </div>
      <section v-if="admin_drafts.length > 0" aria-label="本批结构化变更">
        <p>本批共 {{ admin_drafts.length }} 项；预检后将共用同一个计划、确认和事务。</p>
        <ol>
          <li
            v-for="(draft, index) in admin_drafts"
            :key="`${index}:${draft.fact_kind}:${draft.source_key}`"
            data-role="admin-draft-item"
          >
            {{ draft.operation }} · {{ draft.fact_kind }} · {{ draft.source_key }}
            <button type="button" :disabled="write_busy" @click="remove_admin_change(index)">
              移除
            </button>
          </li>
        </ol>
      </section>
    </section>

    <p v-if="form_error !== null" role="alert">{{ form_error }}</p>
    <p v-if="state.error_message !== null" role="alert">{{ state.error_message }}</p>
    <p v-if="unresolved_formal_action">
      上一次正式动作尚未查清；当前只能核验结果，不能建立或提交新计划。
    </p>

    <section v-if="state.plan !== null" class="plan" data-section="plan" aria-label="预检结果">
      <h3>预检结果</h3>
      <p>{{ state.plan.is_clean ? "预检通过，可以准备确认。" : "预检未通过，数据不会写入。" }}</p>

      <section v-if="state.plan.issues.length > 0">
        <h4>需要处理的问题</h4>
        <p>共 {{ state.plan.issues.length }} 条，第 {{ issue_page + 1 }} / {{ issue_page_count }} 页。</p>
        <ul data-list="issues">
          <li v-for="issue in visible_issues" :key="`${issue.source_slot}:${issue.sheet_code}:${issue.row_number}:${issue.column_code}:${issue.error_code}`">
            {{ issue.source_slot }} / {{ issue.sheet_code }} / 第 {{ issue.row_number }} 行 / {{ issue.column_code }}：{{ issue.error_code }}
          </li>
        </ul>
        <div v-if="issue_page_count > 1" class="button-row">
          <button type="button" :disabled="issue_page === 0" @click="issue_page -= 1">上一页</button>
          <button
            type="button"
            data-action="issues-next-page"
            :disabled="issue_page + 1 >= issue_page_count"
            @click="issue_page += 1"
          >下一页</button>
        </div>
      </section>

      <section v-if="state.plan.changes.length > 0">
        <h4>将发生的差异</h4>
        <p>共 {{ state.plan.changes.length }} 条，第 {{ change_page + 1 }} / {{ change_page_count }} 页。</p>
        <article v-for="change in visible_changes" :key="`${change.fact_kind}:${change.source_key}`" class="change-card">
          <h5>{{ change.operation }} · {{ change.fact_kind }} · {{ change.source_key }}</h5>
          <p>{{ change.sheet_code }} 第 {{ change.row_number }} 行，当前预期版本 {{ change.expected_version }}</p>
          <ul>
            <li v-for="diff in change.fields" :key="diff.field_code">
              {{ diff.field_code }}：{{ format_field_value(diff.new_value) }}
            </li>
          </ul>
        </article>
        <div v-if="change_page_count > 1" class="button-row">
          <button type="button" :disabled="change_page === 0" @click="change_page -= 1">上一页</button>
          <button type="button" :disabled="change_page + 1 >= change_page_count" @click="change_page += 1">下一页</button>
        </div>
      </section>

      <section v-if="state.plan.status_decisions.length > 0">
        <h4>状态映射决定</h4>
        <p>共 {{ state.plan.status_decisions.length }} 条，第 {{ status_page + 1 }} / {{ status_page_count }} 页。</p>
        <ul>
          <li v-for="decision in visible_status_decisions" :key="`${decision.source_slot}:${decision.sheet_code}:${decision.row_number}:${decision.field_code}`">
            {{ decision.source_slot }} / {{ decision.sheet_code }} / 第 {{ decision.row_number }} 行：
            {{ decision.field_code }}：{{ decision.original_status_value }} →
            {{ decision.standard_value }}（{{ decision.mapping_version }}）
          </li>
        </ul>
        <div v-if="status_page_count > 1" class="button-row">
          <button type="button" :disabled="status_page === 0" @click="status_page -= 1">上一页</button>
          <button type="button" :disabled="status_page + 1 >= status_page_count" @click="status_page += 1">下一页</button>
        </div>
      </section>

      <div class="button-row">
        <button
          v-if="state.plan.is_clean && !state.confirmation_ready && !state.confirmation_locked && state.apply_result === null && state.result?.state !== 'applied'"
          type="button"
          data-action="prepare-confirmation"
          :disabled="write_busy || state.refreshing_result"
          @click="controller.prepareConfirmationFromClick"
        >
          {{ state.busy_write === "prepare_confirmation" ? "正在准备……" : "准备正式确认" }}
        </button>
        <button
          v-if="state.confirmation_ready"
          type="button"
          data-action="confirm"
          :disabled="write_busy || state.refreshing_result"
          @click="controller.confirmFromClick"
        >
          {{ state.busy_write === "confirm" ? "正在提交……" : "确认写入学校基础数据" }}
        </button>
        <button
          v-if="state.confirmation_locked && state.result?.state !== 'applied'"
          type="button"
          data-action="refresh-result"
          :disabled="state.refreshing_result || state.busy_write !== null"
          @click="controller.refreshResult"
        >
          {{ state.refreshing_result ? "正在核验……" : "核验结果" }}
        </button>
      </div>
      <p v-if="state.confirmation_ready && state.confirmation_expires_at !== null">
        确认凭据已建立，请在到期前核对并点击正式确认。
      </p>
      <p v-if="state.result?.state === 'pending'">结果仍在核验中，不会自动重复提交。</p>
      <p v-else-if="state.result?.state === 'applied'">
        已应用 {{ state.result.changed_count }} 项变更，学校事实版本 {{ state.result.target_version }}。
      </p>
    </section>
  </section>
</template>

<style scoped>
.school-fact-admin,
.input-panel,
.plan {
  display: grid;
  gap: 1rem;
}

.mode-tabs,
.button-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}

.input-panel,
.plan,
.change-card,
fieldset {
  padding: 1rem;
  border: 1px solid #d7dce2;
  border-radius: 0.75rem;
}

label {
  display: grid;
  gap: 0.35rem;
}

input,
select,
button {
  min-height: 44px;
  font: inherit;
}

ul {
  margin: 0;
}
</style>
