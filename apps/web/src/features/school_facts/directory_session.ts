import { createDirectoryApi, type DirectoryApi } from "./directory_api";
import { ProductApiError } from "./product_api";
import type { DirectoryChoice, DirectoryDescriptor, DirectoryQuery, DirectoryQueryInput, DirectoryRecord, DirectoryProposal, DirectoryChange, DirectoryOriginal, FieldValue, ProductReceipt } from "./directory_contracts";
export interface DirectoryState { cancelFrom: string; rescheduleFrom: string; batchChoices: Record<string,DirectoryRecord[]>; batchContext: Pick<DirectoryChange,"student_ref"|"old_class_ref"|"replace_refs"|"corrected_purpose">; batchChanges: DirectoryChange[]; batchPurpose: NonNullable<DirectoryChange["purpose"]>; authentication_required: boolean; families: DirectoryChoice[]; family: string; descriptor: DirectoryDescriptor | null; result: DirectoryQuery | null; selected: DirectoryRecord | null; creation: string | null; fields: Record<string, FieldValue>; reason: string; effectiveAt: string; operation: DirectoryChange["operation"]; proposal: DirectoryProposal | null; reviewed: boolean; receipt: ProductReceipt | null; original: DirectoryOriginal | null; choices: Record<string, DirectoryRecord[]>; choiceNotices: Record<string, string>; busy: boolean; error: string | null }
const initial = (): DirectoryState => ({ cancelFrom:"", rescheduleFrom:"", batchChoices: {}, batchContext: {}, batchChanges: [], batchPurpose: "record", authentication_required: false, families: [], family: "", descriptor: null, result: null, selected: null, creation: null, fields: {}, reason: "", effectiveAt: "", operation: "upsert", proposal: null, reviewed: false, receipt: null, original: null, choices: {}, choiceNotices: {}, busy: false, error: null });
/** In-memory lifecycle; a sent confirmation can only be resolved by its original receipt. */
export function createDirectoryController(api: DirectoryApi = createDirectoryApi(), options: {residenceImports?: boolean;stage?: (input:DirectoryChange)=>void|Promise<void>} = {}) {
  let state = initial(); let disposed = false; let pending: string | null = null;
  let queryInput: DirectoryQueryInput | null = null;
  const listeners = new Set<(state: Readonly<DirectoryState>) => void>();
  const publish = (patch: Partial<DirectoryState>) => { if (disposed || state.authentication_required) return; state = { ...state, ...patch }; listeners.forEach(fn => fn(state)); };
  const available = () => !disposed && !state.authentication_required && !state.busy;
  const editable = () => available() && pending === null;
  function invalidateIdentity() {
    if (disposed || state.authentication_required) return;
    state = { ...state, authentication_required: true, busy: false,
      receipt: pending ? state.receipt ?? unknown(pending) : state.receipt,
      error: "当前履职身份已失效，请重新核对工程身份；草稿保留，暂不能继续操作。" };
    listeners.forEach(fn => fn(state));
  }
  function identityFailure(error: unknown) {
    if (!(error instanceof ProductApiError) || error.status !== 401) return false;
    invalidateIdentity(); return true;
  }
  const failure = (error: unknown) => error instanceof ProductApiError ? error.message : "本次操作暂时未完成，请稍后重试。";
  async function run(work: () => Promise<void>) { if (!available()) return false; publish({ busy: true, error: null }); try { await work(); return !disposed && !state.authentication_required; } catch (cause) { if (!identityFailure(cause)) publish({ error: failure(cause) }); return false; } finally { publish({ busy: false }); } }
  const clearDraft = (): Partial<DirectoryState> => ({ cancelFrom:"", rescheduleFrom:"", selected: null, creation: null, fields: {}, reason: "", effectiveAt: "", operation: "upsert", proposal: null, reviewed: false, receipt: null, original: null, choices: {}, choiceNotices: {} });
  function setField(name: string, value: FieldValue) { if (!editable() || (!state.selected && !state.creation)) return false; const field = state.descriptor?.fields.find(f => f.name === name); if (!field) return false; if (field.type === "reference" && value !== null && value !== "" && !state.choices[name]?.some(row => row.reference === value)) return false; if (field.type === "references" && (!Array.isArray(value) || value.some(ref => !state.choices[name]?.some(row => row.reference === ref) && !(Array.isArray(state.selected?.fields[name]) && (state.selected?.fields[name] as string[]).includes(ref))))) return false; if (field.type === "object" && (value === null || typeof value !== "object" || Array.isArray(value) || Object.entries(value).some(([role,n]) => !(field.options.length ? field.options.map(option=>option.value) : ["primary","deputy","joint","acting"]).includes(role) || !Number.isInteger(n) || Number(n) < 1 || Number(n) > 100))) return false; if (field.type === "select" && value !== null && value !== "" && !field.options.some(option => option.value === value)) return false; const patch = { ...state.fields, [name]: value }; if (name === "education_target_kind") { patch.education_target_ref = null; publish({ choices: { ...state.choices, education_target_ref: [] } }); } if (name === "target_fact_kind") { patch.target_ref = null; patch.replacement_ref = null; publish({ choices: { ...state.choices, target_ref: [], replacement_ref: [] } }); } publish({ fields: patch, proposal: null, reviewed: false, receipt: null }); return true; }
  function unknown(ref: string): ProductReceipt { return { reference: ref, status: "unknown", message: "结果尚未查明，请查询原操作回执。请勿重复提交。", changed_count: null, target_version: null, request_id: null }; }
  function accept(value: ProductReceipt, ref: string) { if (value.reference !== ref) throw new ProductApiError(200, true); if (value.status !== "unknown") pending = null; publish({ receipt: value, proposal: null, reviewed: false, ...(value.status !== "unknown" ? {batchChanges:[],batchPurpose:"record" as const,batchContext:{},batchChoices:{}} : {}) }); }
  async function reviewProposal(ref = state.proposal?.proposal_ref) { if (!editable() || !ref) return false; return run(async () => { const proposal = await api.proposal(ref); if (disposed || state.authentication_required) return; if (proposal.proposal_ref !== ref) throw new ProductApiError(200, true); const descriptor = await api.describe(proposal.summary.fact_kind); if (disposed || state.authentication_required) return; publish({ result: null, original: null, proposal, descriptor, family: proposal.summary.fact_kind, reviewed: true, receipt: null, selected: null, creation: null, fields: {}, reason: "" }); }); }
  function currentChange(): DirectoryChange { return { ...(state.cancelFrom && state.operation === "cancel" ? {cancel_from:new Date(state.cancelFrom).toISOString()}:{}), ...(state.rescheduleFrom ? {reschedule_from:new Date(state.rescheduleFrom).toISOString()}:{}), fact_kind: state.family, ...(state.selected ? { target_ref: state.selected.reference } : { creation_ref: state.creation! }), operation: state.operation, fields: ["end", "revoke", "cancel"].includes(state.operation) ? {} : { ...state.fields }, ...(state.effectiveAt ? { effective_at: new Date(state.effectiveAt).toISOString() } : {}), reason: state.reason.trim() }; }
  return {
    invalidateIdentity,
    getState: (): Readonly<DirectoryState> => state,
    subscribe(fn: (state: Readonly<DirectoryState>) => void) { if (!disposed) { listeners.add(fn); fn(state); } return () => listeners.delete(fn); },
    open: () => run(async () => { const result = await api.catalog(); publish({ families: result.families }); }),
    chooseFamily: (family: string) => !editable() || !state.families.some(f => f.value === family) ? Promise.resolve(false) : run(async () => { publish({ ...clearDraft(), family, descriptor: null, result: null }); const descriptor = await api.describe(family); if (descriptor.fact_kind !== family) throw new ProductApiError(200, true); publish({ descriptor }); }),
    query: (input: DirectoryQueryInput) => run(async () => { queryInput = structuredClone(input); publish({ result: null, original: null, ...(pending === null ? clearDraft() : {}) }); const result = await api.query(input); if (result.fact_kind !== input.fact_kind) throw new ProductApiError(200, true); publish({ result }); }),
    loadMore() {
      const cursor = state.result?.next_cursor;
      if (!available() || !cursor || !queryInput) return Promise.resolve(false);
      const input = { ...queryInput, cursor };
      return run(async () => {
        publish({ result: null, original: null, ...(pending === null ? clearDraft() : {}) });
        const result = await api.query(input);
        if (result.fact_kind !== input.fact_kind) throw new ProductApiError(200, true);
        // Replace the page: earlier rows are not newly authorized by this read.
        publish({ result });
      });
    },
    edit(ref: string) { if (!editable()) return false; const selected = state.result?.records.find(r => r.reference === ref); if (!selected) return false; publish({ ...clearDraft(), selected }); return true; },
    create(ref: string) { if (!editable() || !state.descriptor?.creation_choices.some(c => c.creation_ref === ref)) return false; publish({ ...clearDraft(), creation: ref }); return true; },
    loadBatchChoices(kind: string) { if (!editable()) return Promise.resolve(false); return run(async()=> { const result=await api.query({fact_kind:kind,scope:"all_authorized"}); if(result.fact_kind!==kind) throw new ProductApiError(200,true); publish({batchChoices:{...state.batchChoices,[kind]:result.records}}); }); },
    setBatchReference(field: "student_ref"|"old_class_ref"|"replace_refs", kind:string, references:string[]) { if(!editable() || references.some(ref=>!state.batchChoices[kind]?.some(row=>row.reference===ref)) || (field==="student_ref" && kind!=="student") || (field==="old_class_ref" && kind!=="education_class") || (field!=="replace_refs" && references.length>1)) return false; publish({batchContext:{...state.batchContext,[field]:field==="replace_refs"?references:references[0]},proposal:null,reviewed:false}); return true; },
    setCorrectedPurpose(value: string) { if(editable() && ["transfer","graduation","close_class","replace_assignment"].includes(value)) publish({batchContext:{...state.batchContext,corrected_purpose:value},proposal:null,reviewed:false}); },
    stageCurrent() { if (TEACHING_KINDS.includes(state.family) && state.operation === "cancel" && !state.cancelFrom) return false; if (!editable() || (!state.selected && !state.creation) || !state.reason.trim() || !!state.rescheduleFrom || state.batchChanges.length >= 299) return false; const change = currentChange(); if (state.batchChanges.some(item => item.fact_kind === change.fact_kind && (item.target_ref ?? item.creation_ref) === (change.target_ref ?? change.creation_ref))) return false; publish({ ...clearDraft(), batchChanges: [...state.batchChanges, change] }); return true; },
    removeStaged(index: number) { if (editable()) publish({batchChanges: state.batchChanges.filter((_,i)=>i!==index), proposal:null, reviewed:false}); },
    setBatchPurpose(purpose: NonNullable<DirectoryChange["purpose"]>) { if (editable()) publish({batchPurpose:purpose, proposal:null, reviewed:false}); },
    setField,
    setReason(reason: string) { if (editable()) publish({ reason, proposal: null, reviewed: false }); },
    setCancelFrom(value:string) { if(editable() && TEACHING_KINDS.includes(state.family)) publish({cancelFrom:value,proposal:null,reviewed:false}); },
    setRescheduleFrom(value:string) { if(editable()) publish({rescheduleFrom:value,proposal:null,reviewed:false}); },
    setEffectiveAt(effectiveAt: string) { if (editable()) publish({ effectiveAt, proposal: null, reviewed: false }); },
    setOperation(operation: DirectoryChange["operation"]) { if (editable() && (!state.creation || operation === "upsert")) publish({ operation, proposal: null, reviewed: false }); },
    loadChoices(name: string) { const field = state.descriptor?.fields.find(f => f.name === name && ["reference", "references"].includes(f.type)); const family = field?.reference_kind ?? (name === "education_target_ref" ? state.fields.education_target_kind ?? state.selected?.fields.education_target_kind : null) ?? (name === "target_ref" || name === "replacement_ref" ? state.fields.target_fact_kind ?? state.selected?.fields.target_fact_kind : null); if (!editable() || !field || typeof family !== "string") return Promise.resolve(false); return run(async () => { const result = api.choices && field.reference_kind ? await api.choices(state.family, name) : await api.query({ fact_kind: family, scope: "all_authorized" }); publish({ choices: { ...state.choices, [name]: result.records }, choiceNotices: { ...state.choiceNotices, [name]: result.status === "denied" ? "当前身份无权查询这些对象。" : result.truncated ? "仅展示部分获权对象，未出现不代表不存在。" : result.records.length ? "请选择正式对象，显示引用用于区分同名项。" : "当前未返回可选对象；不代表正式不存在。" } }); }); },
    prepare() { if (TEACHING_KINDS.includes(state.family) && state.operation === "cancel" && !state.cancelFrom) return Promise.resolve(false); if (!editable() || (!state.selected && !state.creation) || !state.reason.trim()) return Promise.resolve(false); const input: DirectoryChange = { ...currentChange(), ...state.batchContext, ...(state.batchChanges.length || state.batchPurpose !== "record" ? {additional_changes: state.batchChanges.map(item => structuredClone(item)), purpose: state.batchPurpose} : {}) }; return run(async () => { publish({ proposal: null, reviewed: false, receipt: null }); if(options.stage){await options.stage(structuredClone(input));return;} const proposal = await api.prepare(input); if (proposal.summary.reference !== (input.target_ref ?? input.creation_ref) || proposal.summary.fact_kind !== input.fact_kind || (proposal.summary.operation !== input.operation && !(TEACHING_KINDS.includes(input.fact_kind) && (input.operation === "end" || (input.operation === "cancel" && !!input.cancel_from && proposal.summary.regroup?.teaching_cancellations?.some(item=>item.reference===input.target_ref && Date.parse(item.effective_at)===Date.parse(input.cancel_from!) && item.publication_reference.startsWith("publication:")) && Date.parse(proposal.summary.effective_at)===Date.parse(input.cancel_from) && proposal.summary.before_segments?.some(segment=>Date.parse(segment.effective_at)===Date.parse(input.cancel_from!)) && Array.isArray(proposal.summary.after_segments))) && proposal.summary.operation === "correct" && (input.operation === "cancel" || proposal.summary.after_segments?.some(segment=>segment.valid_to===proposal.summary.effective_at)))) || proposal.summary.reason !== input.reason || Object.entries(input.fields).some(([name, value]) => JSON.stringify((proposal.summary.after ?? (input.reschedule_from ? proposal.summary.after_segments?.find(segment=>Date.parse(segment.effective_at)===Date.parse(input.effective_at ?? ""))?.fields : null))?.[name]) !== JSON.stringify(value))) throw new ProductApiError(200, true); publish({ proposal }); }); },
    prepareResidenceImport(mode: "incremental" | "replace" = "incremental", replacementRefs: string[] = [], includeCurrent = true) {
      if (!options.residenceImports || !editable() || state.family !== "student_bed_residence" || !state.reason.trim() || !state.effectiveAt || !Number.isFinite(Date.parse(state.effectiveAt))) return Promise.resolve(false);
      if (mode === "replace" && (!replacementRefs.length || replacementRefs.length > 100 || new Set(replacementRefs).size !== replacementRefs.length || replacementRefs.some(ref => !state.result?.records.some(row => row.reference === ref)))) return Promise.resolve(false);
      if (includeCurrent && !state.selected && !state.creation) return Promise.resolve(false);
      const at = new Date(state.effectiveAt).toISOString();
      const rows = [...state.batchChanges, ...(includeCurrent ? [currentChange()] : [])].map(row => ({...structuredClone(row), effective_at: at}));
      if (rows.length > 100 || rows.some(row => row.fact_kind !== "student_bed_residence") || new Set(rows.map(row => row.target_ref ?? row.creation_ref)).size !== rows.length || (!rows.length && mode !== "replace")) return Promise.resolve(false);
      const root: DirectoryChange = rows[0] ?? {fact_kind: state.family, target_ref: replacementRefs[0], operation: "end", fields: {}, reason: state.reason.trim(), effective_at: at};
      const input: DirectoryChange = {...root, reason: state.reason.trim(), effective_at: at, import_mode: mode, import_rows: rows, ...(mode === "replace" ? {import_replacement_refs: [...replacementRefs]} : {})};
      const references = new Set([...rows.map(row => row.target_ref ?? row.creation_ref), ...replacementRefs]);
      return run(async () => {publish({proposal:null,reviewed:false,receipt:null}); if(options.stage){await options.stage(structuredClone(input));return;} const proposal = await api.prepare(input);
        if (!references.has(proposal.summary.reference) || proposal.summary.fact_kind !== input.fact_kind || proposal.summary.reason !== input.reason || Date.parse(proposal.summary.effective_at) !== Date.parse(at)) throw new ProductApiError(200,true);
        publish({proposal});
      });
    },
    prepareImport(rows: Record<string, FieldValue>[], mode: "incremental" | "replace" = "incremental", replacementRefs: string[] = []) {
      if (!editable() || (!state.selected && !state.creation) || !state.reason.trim() || state.batchChanges.length || !TEACHING_KINDS.includes(state.family) || rows.length > 300 || (mode === "replace" && !replacementRefs.length)) return Promise.resolve(false);
      const references=[...new Set([...(state.result?.records.map(r=>r.reference) ?? []),...(state.descriptor?.creation_choices.map(r=>r.creation_ref) ?? [])])];
      if(replacementRefs.some(ref=>!state.result?.records.some(r=>r.reference===ref)) || rows.some(row=>typeof row.reference!=="string" || !references.includes(row.reference))) return Promise.resolve(false);
      const input:DirectoryChange={...currentChange(),fields:{},import_rows:structuredClone(rows),import_mode:mode,import_target_refs:references,...(mode === "replace" ? {import_replacement_refs:[...replacementRefs]}:{})};
      return run(async()=>{publish({proposal:null,reviewed:false,receipt:null});if(options.stage){await options.stage(structuredClone(input));return;}const proposal=await api.prepare(input);if(!references.includes(proposal.summary.reference)||proposal.summary.fact_kind!==input.fact_kind||proposal.summary.reason!==input.reason) throw new ProductApiError(200,true);publish({proposal});});
    },
    reviewProposal,
    confirm() { const proposal = state.proposal; if (!editable() || !proposal || !state.reviewed) return Promise.resolve(false); if (Date.parse(proposal.expires_at) <= Date.now()) { publish({ proposal: null, reviewed: false, error: "本次修改已过期，请重新准备并核对。" }); return Promise.resolve(false); } pending = proposal.proposal_ref; const ref = pending; return run(async () => { publish({ proposal: null, reviewed: false }); try { const receipt = await api.confirm(ref); if (!disposed && !state.authentication_required) accept(receipt, ref); } catch (cause) { publish({ receipt: unknown(ref) }); identityFailure(cause); } }); },
    checkReceipt() { const ref = pending ?? state.receipt?.reference; if (!ref) return Promise.resolve(false); return run(async () => { try { const receipt = await api.receipt(ref); if (!disposed && !state.authentication_required) accept(receipt, ref); } catch (cause) { if (identityFailure(cause)) return; if (pending) publish({ receipt: unknown(ref) }); else publish({ error: "原回执暂时无法核对。" }); } }); },
    readOriginal: (ref: string, asOf?: string) => run(async () => { publish({ original: null }); const original = await api.original(ref, asOf); publish({ original }); }),
    dispose() { disposed = true; pending = null; listeners.clear(); state = initial(); },
  };
}

export const TEACHING_KINDS = ["teaching_subject","teaching_course","course_subject","teaching_offering","offering_member_source","teaching_group","teaching_group_member","teaching_assignment","teaching_organization_scope"];
/** Clipboard rows from Excel stay a bounded draft until normal server preview and review. */
export function parseTeachingTable(text:string, descriptor:DirectoryDescriptor): Record<string,FieldValue>[] {
  if(text.length>2000000 || !TEACHING_KINDS.includes(descriptor.fact_kind)) throw new Error("请选择教学类型并限制为 300 行以内。");
  const lines=text.trimEnd().split(/\r?\n/); const headers=lines.shift()!.split("\t").map(v=>v.trim());
  const canonical=(name:string)=>name.endsWith("_refs")?name.slice(0,-5)+"_source_keys":name.endsWith("_ref")?name.slice(0,-4)+"_source_key":name;
  const allowed=new Set(["reference","operation",...descriptor.fields.map(f=>canonical(f.name))]);
  if(!headers.includes("reference") || new Set(headers).size!==headers.length || headers.some(h=>!allowed.has(h)) || lines.length>300) throw new Error("列名须含 reference，并使用当前类型的模板字段；最多 300 行。");
  const seen=new Set<string>();return lines.filter(line=>line.trim()).map(line=>{const cells=line.split("\t");if(cells.length>headers.length)throw new Error("存在多余单元格。");const row:Record<string,FieldValue>={};headers.forEach((name,i)=>{const cell=cells[i]?.trim();if(cell)row[name]=cell;});if(typeof row.reference!=="string"||seen.has(row.reference))throw new Error("对象引用为空或重复。");seen.add(row.reference);return row;});
}

export const teachingColumn=(name:string)=>name.endsWith("_refs")?name.slice(0,-5)+"_source_keys":name.endsWith("_ref")?name.slice(0,-4)+"_source_key":name;
export interface TeachingDraftRow { label:string; values:Record<string,FieldValue> }
/** Identity travels with the row; labels are display only and never matched to people. */
export function teachingDraftRows(descriptor:DirectoryDescriptor, records:DirectoryRecord[]):TeachingDraftRow[]{
 if(!TEACHING_KINDS.includes(descriptor.fact_kind)||records.length>300||records.some(r=>r.fact_kind!==descriptor.fact_kind)||new Set(records.map(r=>r.reference)).size!==records.length)throw new Error("请选择同一教学类型的获准条目，最多 300 行。");
 return records.map(record=>({label:record.label,values:{reference:record.reference,operation:"upsert",...Object.fromEntries(descriptor.fields.filter(f=>!["references","object","lines"].includes(f.type)).flatMap(field=>{const value=record.fields[field.name];return value===undefined||value===null?[]:[[teachingColumn(field.name),value]];}))}}));
}

export function teachingImportRows(rows:TeachingDraftRow[]):Record<string,FieldValue>[]{return rows.map(row=>row.values.operation==='end'?{reference:row.values.reference,operation:'end'}:{...row.values});}
