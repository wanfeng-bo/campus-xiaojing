import { createProductApi, ProductApiError, type ProductApi } from "./product_api";
import { hasFamilyConversation, isProductQuery } from "./product_contracts";
import type {
  ProductSession, ProductContact, ProductQuery, ProductQuerySelection, ProductDraft, ProductProposal, ProductReceipt,
} from "./product_contracts";

export interface ProductState {
  authentication_required: boolean;
  identity_rechecked: boolean;
  session: ProductSession | null;
  context_epoch: number;
  busy: "session" | "query" | "prepare" | "confirm" | "receipt" | null;
  result: ProductQuery | null;
  selected_contact: ProductContact | null;
  draft: ProductDraft;
  proposal: ProductProposal | null;
  receipt: ProductReceipt | null;
  error_message: string | null;
}

function initialState(context_epoch = 0): ProductState {
  return {
    authentication_required: false, identity_rechecked: false, session: null, context_epoch, busy: null, result: null, selected_contact: null,
    draft: { contact_value: "", service_hours: "", reason: "" },
    proposal: null, receipt: null, error_message: null,
  };
}

/**
 * 只在当前页面内存保留事实和候选。身份变化立即切断旧生命周期；
 * 正式确认一旦发出，原操作只能查回执，不能因网络失败重发。
 */
export function createProductSessionController(api: ProductApi = createProductApi()) {
  let state = initialState();
  let disposed = false;
  let epoch = 0;
  let draft_revision = 0;
  let pending_receipt_ref: string | null = null;
  const listeners = new Set<(state: Readonly<ProductState>) => void>();
  function publish(patch: Partial<ProductState>) {
    if (disposed) return;
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
  }
  const alive = (version: number) => !disposed && version === epoch;
  const signedIn = () => !state.authentication_required && state.session?.status === "signed_in" && state.session.selected_context_ref !== null;
  const unresolved = () => pending_receipt_ref !== null;
  function invalidateIdentity() {
    if (disposed || state.authentication_required) return;
    epoch += 1;
    publish({ identity_rechecked: false, authentication_required: true, busy: null,
      receipt: pending_receipt_ref ? state.receipt ?? unknownReceipt(pending_receipt_ref) : state.receipt,
      error_message: "当前履职身份已失效，请重新核对身份；原消息与草稿保留，暂不能继续操作。" });
  }
  async function recheckIdentity() {
    if (disposed || !state.authentication_required || state.busy === "session") return false;
    const current = epoch;
    publish({ busy: "session" });
    try {
      const session = await api.getSession();
      if (!alive(current)) return false;
      publish({ session, identity_rechecked: true });
      return true;
    } catch (cause) { if (alive(current)) publish({ error_message: error(cause) }); return false; }
    finally { if (alive(current)) publish({ busy: null }); }
  }
  function error(error: unknown) {
    if (error instanceof ProductApiError && error.status === 401) invalidateIdentity();
    return error instanceof ProductApiError ? error.message : "本次操作暂时未完成，请稍后重试。";
  }

  async function identity(operation: () => Promise<ProductSession>, preserveParentConversation = false) {
    if (disposed || state.busy === "session") return false;
    const current = ++epoch;
    pending_receipt_ref = null;
    draft_revision += 1;
    // Card focus is not a new parent identity. Keep only its known conversation
    // scope while all direct-query/maintenance data is immediately discarded.
    const session = preserveParentConversation ? state.session : null;
    state = { ...initialState(current), session };
    publish({ busy: "session" });
    try {
      const session = await operation();
      if (!alive(current)) return false;
      publish({ session });
      return true;
    } catch (cause) {
      if (alive(current)) publish({ session: null, error_message: error(cause) });
      return false;
    } finally {
      if (alive(current)) publish({ busy: null });
    }
  }

  async function querySelection(selection: string | ProductQuerySelection) {
    if (disposed || !signedIn() || state.session?.directory_enabled || state.busy !== null) return false;
    const current = epoch;
    publish({ busy: "query", result: null, error_message: null });
    if (!unresolved()) {
      draft_revision += 1;
      publish({ selected_contact: null, proposal: null });
    }
    try {
      const result = await api.queryContacts(selection);
      if (!alive(current)) return false;
      publish({ result });
      return true;
    } catch (cause) {
      if (alive(current)) publish({ error_message: error(cause) });
      return false;
    } finally { if (alive(current)) publish({ busy: null }); }
  }

  function queryContacts(campus_name: string) {
    return !campus_name.trim() || campus_name.trim().length > 100 ? Promise.resolve(false)
      : querySelection(campus_name.trim());
  }
  function selectCampus(result: ProductQuery, reference: string) {
    if (!isProductQuery(result) || ![...result.candidates, ...result.resolved_campuses]
      .some((campus) => campus.reference === reference)) return Promise.resolve(false);
    return querySelection({ subject: "official_contacts", scope: "selected", campus_ref: reference });
  }

  function editContact(reference: string) {
    if (disposed || !signedIn() || (!state.session?.can_maintain || state.session.directory_enabled) || state.busy !== null || unresolved()) return false;
    const contact = state.result?.status === "known"
      ? state.result.items.find((item) => item.reference === reference) : undefined;
    if (!contact) return false;
    draft_revision += 1;
    publish({
      selected_contact: contact, proposal: null, receipt: null, error_message: null,
      draft: { contact_value: contact.contact_value, service_hours: contact.service_hours ?? "", reason: "" },
    });
    return true;
  }

  function setDraft(patch: Partial<ProductDraft>) {
    if (disposed || state.authentication_required || state.busy !== null || unresolved() || state.selected_contact === null) return;
    draft_revision += 1;
    publish({ draft: { ...state.draft, ...patch }, proposal: null, receipt: null });
  }

  async function prepare() {
    const contact = state.selected_contact;
    if (disposed || !signedIn() || (!state.session?.can_maintain || state.session.directory_enabled) || state.busy !== null ||
      unresolved() || contact === null || !state.draft.contact_value.trim() || !state.draft.reason.trim()) return false;
    const current = epoch;
    const revision = draft_revision;
    const input = {
      contact_ref: contact.reference, contact_value: state.draft.contact_value.trim(),
      service_hours: state.draft.service_hours.trim() || null, reason: state.draft.reason.trim(),
    };
    publish({ busy: "prepare", proposal: null, receipt: null, error_message: null });
    try {
      const proposal = await api.prepareContact(input);
      if (!alive(current) || revision !== draft_revision) return false;
      const before = proposal.summary.before;
      if (before.reference !== contact.reference || before.campus_ref !== contact.campus_ref ||
        before.campus_name !== contact.campus_name || before.contact_kind !== contact.contact_kind ||
        before.contact_value !== contact.contact_value || before.service_hours !== contact.service_hours ||
        before.version !== contact.version ||
        proposal.summary.after.contact_value !== input.contact_value ||
        proposal.summary.after.service_hours !== input.service_hours || proposal.summary.reason !== input.reason) {
        throw new ProductApiError(200, true);
      }
      publish({ proposal });
      return true;
    } catch (cause) {
      if (alive(current)) publish({ error_message: error(cause) });
      return false;
    } finally { if (alive(current)) publish({ busy: null }); }
  }

  function unknownReceipt(reference: string): ProductReceipt {
    return {
      reference, status: "unknown", message: "结果尚未查明，请核对原操作。请勿重复确认。",
      changed_count: null, target_version: null, request_id: null,
    };
  }
  function acceptReceipt(receipt: ProductReceipt, ref: string) {
    if (receipt.reference !== ref) throw new ProductApiError(200, true);
    if (receipt.status !== "unknown") pending_receipt_ref = null;
    publish({ receipt, proposal: null });
  }
  async function confirm() {
    const proposal = state.proposal;
    if (disposed || !signedIn() || (!state.session?.can_maintain || state.session.directory_enabled) || state.busy !== null ||
      unresolved() || proposal === null) return false;
    if (Date.parse(proposal.expires_at) <= Date.now()) {
      publish({ proposal: null, error_message: "本次修改已过期，请重新准备并核对。" });
      return false;
    }
    const current = epoch;
    const ref = proposal.proposal_ref;
    pending_receipt_ref = ref;
    publish({ busy: "confirm", error_message: null, proposal: null });
    try {
      const receipt = await api.confirmContact(ref);
      if (!alive(current)) return false;
      acceptReceipt(receipt, ref);
      return receipt.status !== "unknown";
    } catch (cause) {
      if (alive(current) && cause instanceof ProductApiError && cause.status === 401) { if (pending_receipt_ref && !state.receipt) publish({ receipt: unknownReceipt(pending_receipt_ref) }); invalidateIdentity(); return false; }
      if (alive(current)) publish({ receipt: unknownReceipt(ref) });
      return false;
    } finally { if (alive(current)) publish({ busy: null }); }
  }

  async function checkReceipt() {
    const completed = state.receipt?.status === "succeeded" ? state.receipt : null;
    const ref = pending_receipt_ref ?? completed?.reference ?? null;
    if (disposed || !signedIn() || state.busy !== null || ref === null) return false;
    const current = epoch;
    publish({ busy: "receipt", error_message: null });
    try {
      const receipt = await api.getReceipt(ref);
      if (!alive(current)) return false;
      acceptReceipt(receipt, ref);
      return receipt.status !== "unknown";
    } catch (cause) {
      if (alive(current) && cause instanceof ProductApiError && cause.status === 401) { if (pending_receipt_ref && !state.receipt) publish({ receipt: unknownReceipt(pending_receipt_ref) }); invalidateIdentity(); return false; }
      if (alive(current)) publish(completed === null
        ? { receipt: unknownReceipt(ref) }
        : { receipt: completed, error_message: "原回执暂时无法重新核对，请稍后再查询。" });
      return false;
    } finally { if (alive(current)) publish({ busy: null }); }
  }

  return {
    getState: (): Readonly<ProductState> => state,
    subscribe(listener: (value: Readonly<ProductState>) => void) {
      if (!disposed) { listeners.add(listener); listener(state); }
      return () => listeners.delete(listener);
    },
    invalidateIdentity, recheckIdentity,
    open: () => identity(() => api.getSession()),
    login: (ref: string) => state.session?.accounts.some((choice) => choice.ref === ref)
      ? identity(() => api.login(ref)) : Promise.resolve(false),
    changeContext: (ref: string) => state.session?.contexts.some((choice) => choice.ref === ref)
      ? identity(() => api.changeContext(ref), !state.authentication_required && hasFamilyConversation(state.session) &&
        state.session.contexts.some(choice => choice.ref === ref && choice.kind === "guardian_relationship")) : Promise.resolve(false),
    logout: () => identity(() => api.logout()),
    queryContacts, selectCampus, listCampuses: () => querySelection({ subject: "campuses", scope: "all_authorized" }),
    editContact, setDraft, prepare, confirm, checkReceipt,
    dispose() { disposed = true; epoch += 1; pending_receipt_ref = null; listeners.clear(); state = initialState(epoch); },
  };
}
