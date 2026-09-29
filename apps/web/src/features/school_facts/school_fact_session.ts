import {
  SchoolFactApiError,
  confirmSchoolFactPlan,
  discardSchoolFactSource,
  fetchSchoolFactResult,
  preflightAdaptedSchoolFacts,
  preflightSchoolFactAdmin,
  preflightSchoolFactExcel,
  prepareSchoolFactConfirmation,
  uploadSchoolFactSource,
  type SchoolFactAdaptedSource,
} from "./school_fact_api";
import type {
  SchoolFactAdminChange,
  SchoolFactApply,
  SchoolFactConfirmation,
  SchoolFactPlan,
  SchoolFactResult,
  SchoolFactSource,
} from "./school_fact_contracts";


export interface SchoolFactSessionApi {
  uploadSource: (file: File) => Promise<SchoolFactSource>;
  discardSource: (source_ref: string) => Promise<void>;
  preflightExcel: (source_ref: string, base_version: number) => Promise<SchoolFactPlan>;
  preflightAdmin: (
    base_version: number,
    changes: SchoolFactAdminChange[],
  ) => Promise<SchoolFactPlan>;
  preflightAdapted: (
    sources: SchoolFactAdaptedSource[],
    mapping_version: string,
    base_version: number,
  ) => Promise<SchoolFactPlan>;
  prepareConfirmation: (
    plan_ref: string,
    idempotency_key: string,
  ) => Promise<SchoolFactConfirmation>;
  confirmPlan: (
    plan_ref: string,
    credential: string,
    idempotency_key: string,
  ) => Promise<SchoolFactApply>;
  fetchResult: (plan_ref: string) => Promise<SchoolFactResult>;
}

export interface SchoolFactAdaptedFile {
  source_slot: string;
  file: File;
}

export type SchoolFactWriteKind =
  | "preflight_excel"
  | "preflight_adapted"
  | "preflight_admin"
  | "prepare_confirmation"
  | "confirm";

export interface SchoolFactSessionState {
  plan: SchoolFactPlan | null;
  confirmation_ready: boolean;
  confirmation_locked: boolean;
  confirmation_expires_at: string | null;
  apply_result: SchoolFactApply | null;
  result: SchoolFactResult | null;
  busy_write: SchoolFactWriteKind | null;
  refreshing_result: boolean;
  error_message: string | null;
}

export interface SchoolFactSessionController {
  getState: () => Readonly<SchoolFactSessionState>;
  subscribe: (
    listener: (state: Readonly<SchoolFactSessionState>) => void,
  ) => () => void;
  preflightExcelFromClick: (file: File, base_version: number) => Promise<boolean>;
  preflightAdaptedFromClick: (
    sources: SchoolFactAdaptedFile[],
    mapping_version: string,
    base_version: number,
  ) => Promise<boolean>;
  preflightAdminFromClick: (
    base_version: number,
    changes: SchoolFactAdminChange[],
  ) => Promise<boolean>;
  prepareConfirmationFromClick: () => Promise<boolean>;
  confirmFromClick: () => Promise<boolean>;
  refreshResult: () => Promise<boolean>;
  dispose: () => void;
}


const default_api: SchoolFactSessionApi = {
  confirmPlan: confirmSchoolFactPlan,
  discardSource: discardSchoolFactSource,
  fetchResult: fetchSchoolFactResult,
  preflightAdapted: preflightAdaptedSchoolFacts,
  preflightAdmin: preflightSchoolFactAdmin,
  preflightExcel: preflightSchoolFactExcel,
  prepareConfirmation: prepareSchoolFactConfirmation,
  uploadSource: uploadSchoolFactSource,
};

const MAX_WORKBOOK_BYTES = 10 * 1024 * 1024;
const MAX_ADAPTED_SOURCE_COUNT = 8;
const MAX_ADAPTED_TOTAL_BYTES = 32 * 1024 * 1024;


/**
 * SF3 的浏览器状态只活在当前组件内存；正式写按钮互斥且从不隐藏排队。
 * confirmation credential 留在闭包私有变量中，不进入可渲染 state。
 */
export function createSchoolFactSessionController(
  api: SchoolFactSessionApi = default_api,
  idempotency_key_factory: () => string = default_idempotency_key,
): SchoolFactSessionController {
  let state = initial_state();
  let disposed = false;
  let lifetime = 0;
  let request_order = 0;
  let plan_epoch = 0;
  let confirmation: SchoolFactConfirmation | null = null;
  let prepare_idempotency_key: string | null = null;
  const private_source_refs = new Set<string>();
  const listeners = new Set<(value: Readonly<SchoolFactSessionState>) => void>();

  function publish(patch: Partial<SchoolFactSessionState>): void {
    if (disposed) {
      return;
    }
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
  }

  function begin_write(kind: SchoolFactWriteKind): number | null {
    if (disposed || state.busy_write !== null) {
      return null;
    }
    request_order += 1;
    publish({ busy_write: kind, error_message: null });
    return request_order;
  }

  function finish_write(kind: SchoolFactWriteKind, token: number): void {
    if (!disposed && state.busy_write === kind && request_order === token) {
      publish({ busy_write: null });
    }
  }

  function is_live(token_lifetime: number): boolean {
    return !disposed && lifetime === token_lifetime;
  }

  function track_private_source(source_ref: string): void {
    private_source_refs.add(source_ref);
  }

  function discard_private_source(source_ref: string): void {
    if (!private_source_refs.delete(source_ref)) {
      return;
    }
    try {
      void api.discardSource(source_ref).catch(() => undefined);
    } catch {
      // 私有来源销毁是 best-effort，不能覆盖原预检/中断结果。
    }
  }

  function apply_plan(candidate: SchoolFactPlan, token_lifetime: number): boolean {
    if (!is_live(token_lifetime)) {
      return false;
    }
    plan_epoch += 1;
    confirmation = null;
    prepare_idempotency_key = null;
    publish({
      apply_result: null,
      confirmation_expires_at: null,
      confirmation_locked: false,
      confirmation_ready: false,
      error_message: null,
      plan: candidate,
      result: null,
    });
    return true;
  }

  function publish_action_error(message: string, token_lifetime: number): void {
    if (is_live(token_lifetime)) {
      publish({ error_message: message });
    }
  }

  async function preflightExcelFromClick(
    file: File,
    base_version: number,
  ): Promise<boolean> {
    if (
      unresolved_formal_action() ||
      !valid_base_version(base_version) ||
      file.size < 1 ||
      file.size > MAX_WORKBOOK_BYTES
    ) {
      return false;
    }
    const token = begin_write("preflight_excel");
    if (token === null) {
      return false;
    }
    const token_lifetime = lifetime;
    let source_ref: string | null = null;
    try {
      const source = await api.uploadSource(file);
      source_ref = source.source_ref;
      track_private_source(source_ref);
      if (!is_live(token_lifetime)) {
        return false;
      }
      const candidate = await api.preflightExcel(source.source_ref, base_version);
      return apply_plan(candidate, token_lifetime);
    } catch (error) {
      publish_action_error(
        safe_action_error(error, "文件未能完成预检，请检查后重试。"),
        token_lifetime,
      );
      return false;
    } finally {
      if (source_ref !== null) {
        discard_private_source(source_ref);
      }
      finish_write("preflight_excel", token);
    }
  }

  async function preflightAdaptedFromClick(
    sources: SchoolFactAdaptedFile[],
    mapping_version: string,
    base_version: number,
  ): Promise<boolean> {
    if (
      unresolved_formal_action() ||
      !valid_adapted_inputs(sources, mapping_version, base_version)
    ) {
      return false;
    }
    const token = begin_write("preflight_adapted");
    if (token === null) {
      return false;
    }
    const token_lifetime = lifetime;
    const uploaded_refs: string[] = [];
    try {
      const uploaded: SchoolFactAdaptedSource[] = [];
      for (const item of sources) {
        const source = await api.uploadSource(item.file);
        uploaded_refs.push(source.source_ref);
        track_private_source(source.source_ref);
        if (!is_live(token_lifetime)) {
          return false;
        }
        uploaded.push({ source_slot: item.source_slot, source_ref: source.source_ref });
      }
      const candidate = await api.preflightAdapted(
        uploaded,
        mapping_version,
        base_version,
      );
      return apply_plan(candidate, token_lifetime);
    } catch (error) {
      publish_action_error(
        safe_action_error(error, "来源表未能完成映射预检，请检查后重试。"),
        token_lifetime,
      );
      return false;
    } finally {
      uploaded_refs.forEach(discard_private_source);
      finish_write("preflight_adapted", token);
    }
  }

  async function preflightAdminFromClick(
    base_version: number,
    changes: SchoolFactAdminChange[],
  ): Promise<boolean> {
    if (
      unresolved_formal_action() ||
      !valid_base_version(base_version) ||
      changes.length < 1 ||
      changes.length > 50_000
    ) {
      return false;
    }
    const token = begin_write("preflight_admin");
    if (token === null) {
      return false;
    }
    const token_lifetime = lifetime;
    try {
      const candidate = await api.preflightAdmin(base_version, changes);
      return apply_plan(candidate, token_lifetime);
    } catch (error) {
      publish_action_error(
        safe_action_error(error, "管理变更未能完成预检，请检查后重试。"),
        token_lifetime,
      );
      return false;
    } finally {
      finish_write("preflight_admin", token);
    }
  }

  async function prepareConfirmationFromClick(): Promise<boolean> {
    const current_plan = state.plan;
    if (
      current_plan === null ||
      !current_plan.is_clean ||
      confirmation !== null ||
      state.confirmation_locked ||
      state.apply_result !== null ||
      state.result?.state === "applied" ||
      state.refreshing_result
    ) {
      return false;
    }
    const token = begin_write("prepare_confirmation");
    if (token === null) {
      return false;
    }
    const token_lifetime = lifetime;
    const expected_epoch = plan_epoch;
    const idempotency_key = prepare_idempotency_key ?? idempotency_key_factory();
    prepare_idempotency_key = idempotency_key;
    try {
      const candidate = await api.prepareConfirmation(
        current_plan.plan_ref,
        idempotency_key,
      );
      if (
        !is_live(token_lifetime) ||
        plan_epoch !== expected_epoch ||
        state.plan?.plan_ref !== candidate.plan_ref ||
        state.plan.plan_sha256 !== candidate.plan_sha256
      ) {
        return false;
      }
      confirmation = candidate;
      prepare_idempotency_key = null;
      publish({
        confirmation_expires_at: candidate.expires_at,
        confirmation_ready: true,
        error_message: null,
      });
      return true;
    } catch (error) {
      if (
        is_live(token_lifetime) &&
        error instanceof SchoolFactApiError &&
        error.code === "school_facts.change_rejected"
      ) {
        plan_epoch += 1;
        prepare_idempotency_key = null;
        publish({
          confirmation_expires_at: null,
          confirmation_locked: false,
          confirmation_ready: false,
          error_message: error.message,
          plan: null,
          result: null,
        });
      } else {
        publish_action_error(
          safe_action_error(
            error,
            "确认凭据响应未能取得；如需继续，请再次点击“准备正式确认”。",
          ),
          token_lifetime,
        );
      }
      return false;
    } finally {
      finish_write("prepare_confirmation", token);
    }
  }

  async function confirmFromClick(): Promise<boolean> {
    const current_plan = state.plan;
    const current_confirmation = confirmation;
    if (
      current_plan === null ||
      current_confirmation === null ||
      !current_plan.is_clean ||
      state.refreshing_result
    ) {
      return false;
    }
    const token = begin_write("confirm");
    if (token === null) {
      return false;
    }
    const token_lifetime = lifetime;
    const expected_epoch = plan_epoch;
    confirmation = null;
    publish({
      confirmation_expires_at: null,
      confirmation_locked: true,
      confirmation_ready: false,
    });
    try {
      const candidate = await api.confirmPlan(
        current_plan.plan_ref,
        current_confirmation.credential,
        idempotency_key_factory(),
      );
      if (
        !is_live(token_lifetime) ||
        plan_epoch !== expected_epoch ||
        state.plan?.plan_ref !== candidate.plan_ref
      ) {
        return false;
      }
      publish({
        apply_result: candidate,
        error_message: null,
        result: {
          batch_id: candidate.batch_id,
          changed_count: candidate.changed_count,
          plan_ref: candidate.plan_ref,
          state: "applied",
          target_version: candidate.target_version,
        },
      });
      return true;
    } catch (error) {
      if (
        is_live(token_lifetime) &&
        error instanceof SchoolFactApiError &&
        error.code === "school_facts.change_rejected"
      ) {
        plan_epoch += 1;
        prepare_idempotency_key = null;
        publish({
          apply_result: null,
          confirmation_expires_at: null,
          confirmation_locked: false,
          confirmation_ready: false,
          error_message: error.message,
          plan: null,
          result: null,
        });
      } else {
        publish_action_error(
          "提交结果尚未确认，请只使用“核验结果”查询，不要重复提交。",
          token_lifetime,
        );
      }
      return false;
    } finally {
      finish_write("confirm", token);
    }
  }

  async function refreshResult(): Promise<boolean> {
    const current_plan = state.plan;
    if (
      disposed ||
      current_plan === null ||
      state.refreshing_result ||
      state.busy_write !== null ||
      state.result?.state === "applied"
    ) {
      return false;
    }
    const token_lifetime = lifetime;
    const expected_epoch = plan_epoch;
    publish({ error_message: null, refreshing_result: true });
    try {
      const candidate = await api.fetchResult(current_plan.plan_ref);
      if (
        !is_live(token_lifetime) ||
        plan_epoch !== expected_epoch ||
        state.plan?.plan_ref !== candidate.plan_ref
      ) {
        return false;
      }
      publish({
        confirmation_locked: state.confirmation_locked || candidate.state === "applied",
        error_message: null,
        result: candidate,
      });
      return true;
    } catch {
      if (is_live(token_lifetime) && plan_epoch === expected_epoch) {
        publish({ error_message: "暂时没能核验结果，请稍后重试。" });
      }
      return false;
    } finally {
      if (is_live(token_lifetime)) {
        publish({ refreshing_result: false });
      }
    }
  }

  function dispose(): void {
    if (disposed) {
      return;
    }
    lifetime += 1;
    confirmation = null;
    prepare_idempotency_key = null;
    [...private_source_refs].forEach(discard_private_source);
    state = initial_state();
    disposed = true;
    listeners.clear();
  }

  function unresolved_formal_action(): boolean {
    return state.confirmation_locked && state.result?.state !== "applied";
  }

  return {
    confirmFromClick,
    dispose,
    getState: () => state,
    preflightAdaptedFromClick,
    preflightAdminFromClick,
    preflightExcelFromClick,
    prepareConfirmationFromClick,
    refreshResult,
    subscribe: (listener) => {
      if (disposed) {
        listener(state);
        return () => undefined;
      }
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },
  };
}


function safe_action_error(error: unknown, fallback: string): string {
  if (
    error instanceof SchoolFactApiError &&
    (error.code === "school_facts.change_rejected" ||
      error.code === "school_facts.status_mapping_required" ||
      error.code === "school_facts.status_mapping_ambiguous")
  ) {
    return error.message;
  }
  return fallback;
}


function initial_state(): SchoolFactSessionState {
  return {
    apply_result: null,
    busy_write: null,
    confirmation_expires_at: null,
    confirmation_locked: false,
    confirmation_ready: false,
    error_message: null,
    plan: null,
    refreshing_result: false,
    result: null,
  };
}


function valid_base_version(value: number): boolean {
  return Number.isInteger(value) && value >= 1;
}


function valid_adapted_inputs(
  sources: SchoolFactAdaptedFile[],
  mapping_version: string,
  base_version: number,
): boolean {
  if (
    !valid_base_version(base_version) ||
    sources.length < 1 ||
    sources.length > MAX_ADAPTED_SOURCE_COUNT ||
    sources.reduce((total, item) => total + item.file.size, 0) >
      MAX_ADAPTED_TOTAL_BYTES ||
    mapping_version.length < 1 ||
    mapping_version.length > 120 ||
    !/^[A-Za-z0-9._:-]+$/.test(mapping_version)
  ) {
    return false;
  }
  const slots = sources.map((item) => item.source_slot);
  return new Set(slots).size === slots.length && sources.every((item) =>
    /^[a-z][a-z0-9_]{0,63}$/.test(item.source_slot) &&
      item.file.size >= 1 &&
      item.file.size <= MAX_WORKBOOK_BYTES,
  );
}


function default_idempotency_key(): string {
  return `sf3-${globalThis.crypto.randomUUID()}`;
}
