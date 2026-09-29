export const SCHOOL_FACT_OPERATIONS = [
  "upsert",
  "end",
  "revoke",
  "reactivate",
] as const;

export type SchoolFactOperation = typeof SCHOOL_FACT_OPERATIONS[number];

export const SCHOOL_FACT_KINDS = [
  "school",
  "person",
  "student",
  "student_school_relationship",
  "login_mapping",
  "organization",
  "organization_campus_relationship",
  "assignment",
  "location",
  "location_alias",
  "dorm_room",
  "dormitory_building_profile",
  "dorm_bed",
  "student_bed_residence",
  "person_contact",
  "student_contact",
  "official_contact",
  "policy",
  "policy_scope",
  "policy_version",
] as const;

export type SchoolFactKind = typeof SCHOOL_FACT_KINDS[number];

const ROOT_FACT_KINDS = new Set<SchoolFactKind>([
  "school",
  "person",
  "student",
  "organization",
  "location",
]);
const COMMON_OPERATIONS: readonly SchoolFactOperation[] = ["upsert", "end", "revoke"];
const ROOT_OPERATIONS: readonly SchoolFactOperation[] = [
  ...COMMON_OPERATIONS,
  "reactivate",
];


export function schoolFactOperationsForKind(
  fact_kind: SchoolFactKind,
): readonly SchoolFactOperation[] {
  return ROOT_FACT_KINDS.has(fact_kind) ? ROOT_OPERATIONS : COMMON_OPERATIONS;
}

export type SchoolFactContentField =
  | "usage_kind"
  | "description"
  | "business_code"
  | "display_name"
  | "short_name"
  | "timezone_name"
  | "lifecycle_status"
  | "person_kind"
  | "person_source_key"
  | "provider_code"
  | "provider_tenant_key"
  | "external_subject"
  | "parent_source_key"
  | "organization_source_key"
  | "campus_source_key"
  | "primary_campus_source_key"
  | "administrative_campus_source_key"
  | "location_source_key"
  | "room_source_key"
  | "student_source_key"
  | "bed_source_key"
  | "contact_person_source_key"
  | "publishing_organization_source_key"
  | "policy_source_key"
  | "supersedes_policy_version_source_key"
  | "kind"
  | "status"
  | "display_path"
  | "position_title"
  | "semantic_role_code"
  | "assignment_kind"
  | "appointment_source"
  | "approval_reference"
  | "alias"
  | "alias_kind"
  | "aliases"
  | "authorized_bed_capacity"
  | "accommodation_zone"
  | "is_available"
  | "accessibility_tags"
  | "availability"
  | "student_number"
  | "enrollment_status"
  | "residence_mode"
  | "contact_kind"
  | "contact_value"
  | "is_primary"
  | "visibility_scope"
  | "audience_code"
  | "consent_reference"
  | "relationship_kind"
  | "priority"
  | "is_emergency"
  | "owner_kind"
  | "service_hours"
  | "category_code"
  | "classification"
  | "published_at"
  | "effective_from"
  | "effective_to"
  | "valid_to"
  | "content_text"
  | "version_note";

export type SchoolFactAdminValue =
  | string
  | number
  | boolean
  | string[]
  | null;

export interface SchoolFactAdminChange {
  schema_version?:1|4;
  operation: SchoolFactOperation;
  fact_kind: SchoolFactKind;
  source_key: string;
  expected_version: number;
  effective_at: string;
  content: Partial<Record<SchoolFactContentField, SchoolFactAdminValue>> | null;
}

export interface SchoolFactSource {
  source_ref: string;
  source_sha256: string;
  expires_at: string;
}

export interface SchoolFactIssue {
  source_slot: string;
  sheet_code: string;
  row_number: number;
  column_code: string;
  error_code: string;
}

export type SchoolFactFieldValue = string | number | boolean | string[] | null;

export interface SchoolFactFieldDiff {
  field_code: string;
  new_value: SchoolFactFieldValue;
}

export interface SchoolFactChangeSummary {
  operation: SchoolFactOperation;
  fact_kind: SchoolFactKind;
  source_key: string;
  expected_version: number;
  sheet_code: string;
  row_number: number;
  fields: SchoolFactFieldDiff[];
}

export interface SchoolFactStatusDecision {
  source_slot: string;
  sheet_code: string;
  row_number: number;
  field_code: string;
  original_status_value: string;
  standard_value: string;
  mapping_version: string;
}

export interface SchoolFactPlan {
  plan_ref: string;
  plan_sha256: string;
  is_clean: boolean;
  issues: SchoolFactIssue[];
  changes: SchoolFactChangeSummary[];
  status_decisions: SchoolFactStatusDecision[];
}

export interface SchoolFactConfirmation {
  plan_ref: string;
  plan_sha256: string;
  credential: string;
  expires_at: string;
}

export interface SchoolFactApply {
  plan_ref: string;
  batch_id: string;
  target_version: number;
  changed_count: number;
  replayed: boolean;
  cleanup_pending: boolean;
}

export interface SchoolFactResult {
  plan_ref: string;
  state: "pending" | "applied";
  batch_id: string | null;
  target_version: number | null;
  changed_count: number | null;
}


const FACT_KINDS = new Set<SchoolFactKind>(SCHOOL_FACT_KINDS);

const INVALID_RESPONSE_MESSAGE = "学校基础数据服务返回异常，请刷新后重试。";
const MAX_PLAN_CHANGES = 50_000;
const MAX_PLAN_ISSUES = 250_001;
const MAX_PLAN_STATUS_DECISIONS = 100_000;


export function decodeSchoolFactSource(value: unknown): SchoolFactSource {
  return require_contract(value, is_source);
}


export function decodeSchoolFactPlan(value: unknown): SchoolFactPlan {
  return require_contract(value, is_plan);
}


export function decodeSchoolFactConfirmation(value: unknown): SchoolFactConfirmation {
  return require_contract(value, is_confirmation);
}


export function decodeSchoolFactApply(value: unknown): SchoolFactApply {
  return require_contract(value, is_apply);
}


export function decodeSchoolFactResult(value: unknown): SchoolFactResult {
  return require_contract(value, is_result);
}


function require_contract<T>(
  value: unknown,
  guard: (candidate: unknown) => candidate is T,
): T {
  if (!guard(value)) {
    throw new Error(INVALID_RESPONSE_MESSAGE);
  }
  return value;
}


function is_source(value: unknown): value is SchoolFactSource {
  return is_record(value) &&
    has_exact_keys(value, ["expires_at", "source_ref", "source_sha256"]) &&
    is_opaque_ref(value.source_ref) &&
    is_sha256(value.source_sha256) &&
    is_aware_datetime(value.expires_at);
}


function is_plan(value: unknown): value is SchoolFactPlan {
  return is_record(value) &&
    has_exact_keys(value, [
      "changes",
      "is_clean",
      "issues",
      "plan_ref",
      "plan_sha256",
      "status_decisions",
    ]) &&
    is_opaque_ref(value.plan_ref) &&
    is_sha256(value.plan_sha256) &&
    typeof value.is_clean === "boolean" &&
    is_bounded_array(value.issues, MAX_PLAN_ISSUES, is_issue) &&
    value.is_clean === (value.issues.length === 0) &&
    is_bounded_array(value.changes, MAX_PLAN_CHANGES, is_change_summary) &&
    is_bounded_array(
      value.status_decisions,
      MAX_PLAN_STATUS_DECISIONS,
      is_status_decision,
    );
}


function is_issue(value: unknown): value is SchoolFactIssue {
  return is_record(value) &&
    has_exact_keys(value, [
      "column_code",
      "error_code",
      "row_number",
      "sheet_code",
      "source_slot",
    ]) &&
    is_neutral_code(value.source_slot) &&
    is_safe_text(value.sheet_code, 255) &&
    is_positive_integer(value.row_number) &&
    is_safe_text(value.column_code, 255) &&
    is_safe_text(value.error_code, 255);
}


function is_change_summary(value: unknown): value is SchoolFactChangeSummary {
  return is_record(value) &&
    has_exact_keys(value, [
      "expected_version",
      "fact_kind",
      "fields",
      "operation",
      "row_number",
      "sheet_code",
      "source_key",
    ]) &&
    is_operation(value.operation) &&
    is_fact_kind(value.fact_kind) &&
    schoolFactOperationsForKind(value.fact_kind).includes(value.operation) &&
    is_stable_source_key(value.source_key) &&
    is_non_negative_integer(value.expected_version) &&
    is_safe_text(value.sheet_code, 255) &&
    is_positive_integer(value.row_number) &&
    is_bounded_array(value.fields, 100, is_field_diff);
}


function is_field_diff(value: unknown): value is SchoolFactFieldDiff {
  return is_record(value) &&
    has_exact_keys(value, ["field_code", "new_value"]) &&
    is_safe_text(value.field_code, 120) &&
    is_field_value(value.new_value);
}


function is_status_decision(value: unknown): value is SchoolFactStatusDecision {
  return is_record(value) &&
    has_exact_keys(value, [
      "field_code",
      "mapping_version",
      "original_status_value",
      "row_number",
      "sheet_code",
      "source_slot",
      "standard_value",
    ]) &&
    is_neutral_code(value.source_slot) &&
    is_safe_text(value.sheet_code, 255) &&
    is_positive_integer(value.row_number) &&
    is_safe_text(value.field_code, 120) &&
    is_safe_status_original(value.original_status_value) &&
    is_safe_text(value.standard_value, 1_000) &&
    is_safe_text(value.mapping_version, 120);
}


function is_safe_status_original(value: unknown): value is string {
  return typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 64 &&
    value === value.trim() &&
    /^[\p{L}\p{N} _\-().（）·./]+$/u.test(value);
}


function is_confirmation(value: unknown): value is SchoolFactConfirmation {
  return is_record(value) &&
    has_exact_keys(value, ["credential", "expires_at", "plan_ref", "plan_sha256"]) &&
    is_opaque_ref(value.plan_ref) &&
    is_sha256(value.plan_sha256) &&
    is_opaque_ref(value.credential) &&
    is_aware_datetime(value.expires_at);
}


function is_apply(value: unknown): value is SchoolFactApply {
  return is_record(value) &&
    has_exact_keys(value, [
      "batch_id",
      "changed_count",
      "cleanup_pending",
      "plan_ref",
      "replayed",
      "target_version",
    ]) &&
    is_opaque_ref(value.plan_ref) &&
    is_safe_text(value.batch_id, 255) &&
    is_positive_integer(value.target_version) &&
    is_non_negative_integer(value.changed_count) &&
    typeof value.replayed === "boolean" &&
    typeof value.cleanup_pending === "boolean";
}


function is_result(value: unknown): value is SchoolFactResult {
  if (!is_record(value) || !has_exact_keys(value, [
    "batch_id",
    "changed_count",
    "plan_ref",
    "state",
    "target_version",
  ]) || !is_opaque_ref(value.plan_ref)) {
    return false;
  }
  if (value.state === "pending") {
    return value.batch_id === null &&
      value.target_version === null &&
      value.changed_count === null;
  }
  return value.state === "applied" &&
    is_safe_text(value.batch_id, 255) &&
    is_positive_integer(value.target_version) &&
    is_non_negative_integer(value.changed_count);
}


function is_operation(value: unknown): value is SchoolFactOperation {
  return value === "upsert" ||
    value === "end" ||
    value === "revoke" ||
    value === "reactivate";
}


function is_fact_kind(value: unknown): value is SchoolFactKind {
  return typeof value === "string" && FACT_KINDS.has(value as SchoolFactKind);
}


function is_field_value(value: unknown): value is SchoolFactFieldValue {
  if(value===null)return true;
  if (typeof value === "string") {
    return value.length <= 8_000;
  }
  if (typeof value === "boolean") {
    return true;
  }
  if (typeof value === "number") {
    return Number.isInteger(value);
  }
  return is_bounded_array(
    value,
    100,
    (item): item is string => typeof item === "string" && item.length <= 1_000,
  );
}


function is_bounded_array<T>(
  value: unknown,
  maximum: number,
  guard: (item: unknown) => item is T,
): value is T[] {
  return Array.isArray(value) && value.length <= maximum && value.every(guard);
}


function is_opaque_ref(value: unknown): value is string {
  return typeof value === "string" &&
    value.length >= 10 &&
    value.length <= 128 &&
    /^[A-Za-z0-9_-]+$/.test(value);
}


function is_sha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}


function is_aware_datetime(value: unknown): value is string {
  return typeof value === "string" &&
    value.length <= 64 &&
    /(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value));
}


function is_neutral_code(value: unknown): value is string {
  return typeof value === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(value);
}


function is_stable_source_key(value: unknown): value is string {
  return typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 255 &&
    /^[a-z0-9][a-z0-9._:-]*$/.test(value);
}


function is_safe_text(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length >= 1 && value.length <= maximum;
}


function is_positive_integer(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}


function is_non_negative_integer(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}


function is_record(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}


function has_exact_keys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Reflect.ownKeys(value);
  return actual.every((key) => typeof key === "string") &&
    actual.length === expected.length &&
    expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}
