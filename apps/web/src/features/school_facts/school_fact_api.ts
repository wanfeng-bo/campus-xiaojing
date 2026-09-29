import {
  decodeSchoolFactApply,
  decodeSchoolFactConfirmation,
  decodeSchoolFactPlan,
  decodeSchoolFactResult,
  decodeSchoolFactSource,
  type SchoolFactAdminChange,
  type SchoolFactApply,
  type SchoolFactConfirmation,
  type SchoolFactPlan,
  type SchoolFactResult,
  type SchoolFactSource,
} from "./school_fact_contracts";


export interface SchoolFactAdaptedSource {
  source_slot: string;
  source_ref: string;
}


export type SchoolFactApiErrorCode =
  | "school_facts.change_rejected"
  | "school_facts.response_invalid"
  | "school_facts.status_mapping_ambiguous"
  | "school_facts.status_mapping_required"
  | "school_facts.unavailable";


export class SchoolFactApiError extends Error {
  readonly code: SchoolFactApiErrorCode;

  constructor(
    message: string,
    code: SchoolFactApiErrorCode,
  ) {
    super(message);
    this.name = "SchoolFactApiError";
    this.code = code;
  }
}


/** 原始工作簿只在本次调用内转码和上传，不写入浏览器存储或日志。 */
export async function uploadSchoolFactSource(file: File): Promise<SchoolFactSource> {
  let content_base64: string;
  try {
    content_base64 = await read_file_base64(file);
  } catch {
    throw unavailable_error();
  }
  return request_contract(
    "/api/v1/school-facts/sources",
    {
      body: JSON.stringify({ filename_summary: file.name, content_base64 }),
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
    decodeSchoolFactSource,
  );
}


export function preflightSchoolFactExcel(
  source_ref: string,
  base_version: number,
): Promise<SchoolFactPlan> {
  return request_contract(
    "/api/v1/school-facts/plans/excel",
    json_post({ source_ref, base_version }),
    decodeSchoolFactPlan,
  );
}


export function preflightSchoolFactAdmin(
  base_version: number,
  changes: SchoolFactAdminChange[],
): Promise<SchoolFactPlan> {
  return request_contract(
    "/api/v1/school-facts/plans/admin",
    json_post({ base_version, changes }),
    decodeSchoolFactPlan,
  );
}


export function preflightAdaptedSchoolFacts(
  sources: SchoolFactAdaptedSource[],
  mapping_version: string,
  base_version: number,
): Promise<SchoolFactPlan> {
  return request_contract(
    "/api/v1/school-facts/plans/adapted",
    json_post({ sources, mapping_version, base_version }),
    decodeSchoolFactPlan,
  );
}


export function prepareSchoolFactConfirmation(
  plan_ref: string,
  idempotency_key: string,
): Promise<SchoolFactConfirmation> {
  return request_contract(
    `/api/v1/school-facts/plans/${encodeURIComponent(plan_ref)}/confirmation`,
    {
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Idempotency-Key": idempotency_key },
      method: "POST",
    },
    decodeSchoolFactConfirmation,
  );
}


export function confirmSchoolFactPlan(
  plan_ref: string,
  credential: string,
  idempotency_key: string,
): Promise<SchoolFactApply> {
  return request_contract(
    `/api/v1/school-facts/plans/${encodeURIComponent(plan_ref)}/confirm`,
    {
      body: JSON.stringify({ credential }),
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
      },
      method: "POST",
    },
    decodeSchoolFactApply,
  );
}


/** 结果刷新永远只查询原计划权威终态，不重入确认 POST。 */
export function fetchSchoolFactResult(plan_ref: string): Promise<SchoolFactResult> {
  return request_contract(
    `/api/v1/school-facts/plans/${encodeURIComponent(plan_ref)}/result`,
    {
      cache: "no-store",
      credentials: "same-origin",
      method: "GET",
    },
    decodeSchoolFactResult,
  );
}


/** 放弃私有来源时只按不透明引用销毁；不携带原始字节，也不自动重试。 */
export async function discardSchoolFactSource(source_ref: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(
      `/api/v1/school-facts/sources/${encodeURIComponent(source_ref)}`,
      {
        cache: "no-store",
        credentials: "same-origin",
        method: "DELETE",
      },
    );
  } catch {
    throw unavailable_error();
  }
  if (!response.ok) {
    const error_payload: unknown = await response.json().catch(() => undefined);
    throw decode_http_error(response.status, error_payload);
  }
  if (response.status !== 204 || (await response.text()) !== "") {
    throw new SchoolFactApiError(
      "学校基础数据服务返回异常，请刷新后重试。",
      "school_facts.response_invalid",
    );
  }
}


function json_post(payload: unknown): RequestInit {
  return {
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    method: "POST",
  };
}


async function request_contract<T>(
  path: string,
  init: RequestInit,
  decoder: (payload: unknown) => T,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw unavailable_error();
  }
  if (!response.ok) {
    const error_payload: unknown = await response.json().catch(() => undefined);
    throw decode_http_error(response.status, error_payload);
  }
  const payload: unknown = await response.json().catch(() => undefined);
  try {
    return decoder(payload);
  } catch {
    throw new SchoolFactApiError(
      "学校基础数据服务返回异常，请刷新后重试。",
      "school_facts.response_invalid",
    );
  }
}


function decode_http_error(status: number, value: unknown): SchoolFactApiError {
  if (!is_http_error_response(value)) {
    return unavailable_error();
  }
  if (status === 409 && value.code === "school_facts.change_rejected") {
    return new SchoolFactApiError(
      "学校基础数据已变化或确认已失效，请重新预检。",
      "school_facts.change_rejected",
    );
  }
  if (
    status === 422 &&
    (value.code === "school_facts.status_mapping_required" ||
      value.code === "school_facts.status_mapping_ambiguous")
  ) {
    const coordinate = decode_safe_status_coordinate(value.message);
    if (coordinate !== null) {
      return new SchoolFactApiError(coordinate, value.code);
    }
  }
  return unavailable_error();
}


function is_http_error_response(
  value: unknown,
): value is { code: string; message: string; request_id: string } {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  const keys = Reflect.ownKeys(record);
  return keys.length === 3 &&
    keys.every((key) => typeof key === "string") &&
    Object.prototype.hasOwnProperty.call(record, "code") &&
    Object.prototype.hasOwnProperty.call(record, "message") &&
    Object.prototype.hasOwnProperty.call(record, "request_id") &&
    typeof record.code === "string" &&
    typeof record.message === "string" &&
    record.message.length <= 1_000 &&
    typeof record.request_id === "string" &&
    /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
      .test(record.request_id);
}


function decode_safe_status_coordinate(message: string): string | null {
  const match = /^(?:([a-z][a-z0-9_]{0,63})\/)?([\p{L}\p{N}_.-]{1,64}) 第 ([1-9]\d{0,7}) 行的 ([\p{L}\p{N}_.-]{1,120}) 需要按服务端状态字典 ([A-Za-z0-9._:-]{1,120}) 处理。$/u.exec(message);
  if (match === null || /\d{7,}/.test(message)) {
    return null;
  }
  const [, source_slot, sheet_code, row_number, field_code, mapping_version] = match;
  const source = source_slot === undefined ? "" : `${source_slot} / `;
  return `状态映射需人工处理：${source}${sheet_code} / 第 ${row_number} 行 / ${field_code}；字典 ${mapping_version}。`;
}


function unavailable_error(): SchoolFactApiError {
  return new SchoolFactApiError(
    "学校基础数据通道暂时不可用，请稍后重试。",
    "school_facts.unavailable",
  );
}


function read_file_base64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("file-read-failed"));
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error("file-read-failed"));
        return;
      }
      const separator = reader.result.indexOf(",");
      if (separator < 0) {
        reject(new Error("file-read-failed"));
        return;
      }
      resolve(reader.result.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}
