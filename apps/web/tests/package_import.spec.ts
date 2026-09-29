import { describe, expect, it, vi } from "vitest";
import { decodePackagePlan, hasCompletePackageReview, parsePackageManifest } from "../src/features/school_facts/package_contracts";
import { createPackageSession } from "../src/features/school_facts/package_session";
import {PackageApiError} from "../src/features/school_facts/package_api";
import type { PackageApi } from "../src/features/school_facts/package_api";

const sha = "a".repeat(64);
export const manifest = () => ({
  input_version: "school_facts_v5", school_id: "00000000-0000-0000-0000-000000000001",
  base_version: 1, mode: "incremental", coverage_status: "unknown", replacement_scopes: [],
  sources: [{ logical_source_code: "people", source_sha256: sha, size_bytes: 1,
    mapping_version: "school_facts_source_v5", mapping_config_sha256: sha,
    responsibility_reference: "approved synthetic source", purpose: "facts",
    attached_policy_source_key: null, fact_kinds: ["person"] }],
});
export const plan = () => ({
  plan_ref: "sf3plan_1234567890", plan_sha256: sha, is_clean: true, issues: [], status_decisions: [],
  changes: [{ fact_kind: "person", source_key: "person:one", operation: "upsert", expected_version: 0,
    sheet_code: "f7_s1", row_number: 2, fields: [{field_code:"display_name", new_value:"测试人员"}],
    effective_at: "2026-09-24T00:00:00Z", replacement_segments: [], correction_reason: null,
    cause_source_key: null, cause_version: null }], replacement_preview: [],
});
const completePlan=()=>({...plan(),preview_status:"complete",preview_issues:[],change_preview:[{
  fact_kind:"person",source_key:"person:one",operation:"upsert",expected_version:0,
  effective_at:"2026-09-24T00:00:00Z",origin:"explicit",before:null,after:{display_name:"测试人员"},
  before_segments:[],after_segments:[],domain_preview:null,
}]});
function api(): PackageApi {
  return {
    upload: vi.fn().mockResolvedValue({source_ref:"sf3source_1234567890", source_sha256:sha, expires_at:"2099-01-01T00:00:00Z"}),
    discard: vi.fn().mockResolvedValue(undefined), preflight: vi.fn().mockResolvedValue(decodePackagePlan(plan())),
    prepare: vi.fn(), confirm: vi.fn(), result: vi.fn().mockResolvedValue({plan_ref:plan().plan_ref,state:"pending",batch_id:null,target_version:null,changed_count:null}),
  };
}
describe("F7 package input", () => {
  it("validates the approved manifest without inventing school, mapping or replacement scope", () => {
    expect(parsePackageManifest(manifest()).school_id).toBe(manifest().school_id);
    expect(() => parsePackageManifest({...manifest(), mode:"replace"})).toThrow();
    expect(() => parsePackageManifest({...manifest(), coverage_status:"complete"})).toThrow();
    expect(() => parsePackageManifest({...manifest(), base_version:0})).toThrow();
    expect(()=>parsePackageManifest({...manifest(),sources:Array.from({length:33},(_,i)=>({...manifest().sources[0],logical_source_code:`source_${i}`}))})).toThrow();
    expect(()=>parsePackageManifest({...manifest(),sources:[{...manifest().sources[0],size_bytes:10*1024*1024+1}]})).toThrow();
  });
  it("accepts canonical F7 long and structured preview values without truncation", () => {
    const value = plan();
    value.changes[0].fields = [{field_code:"content_text",new_value:"正文".repeat(6000)}];
    expect(decodePackagePlan(value).changes[0].fields[0].new_value).toHaveLength(12000);
  });
  it("does not request confirmation when the current API lacks complete before/impact proof", async () => {
    const service = api(); const session = createPackageSession(service);
    await session.preflight(parsePackageManifest(manifest()), [new File(["x"],"people.json")]);
    expect(session.state.plan?.plan_ref).toBe(plan().plan_ref);
    expect(session.state.canConfirm).toBe(false);
    expect(await session.prepare()).toBe(false);
    expect(service.prepare).not.toHaveBeenCalled();
  });
  it("invalidates in-flight source upload before it can preflight under a different identity", async () => {
    const service=api(); let release!: (value: unknown) => void;
    service.upload=vi.fn().mockReturnValue(new Promise(resolve=>{release=resolve;}));
    const session=createPackageSession(service);
    const pending=session.preflight(parsePackageManifest(manifest()),[new File(["x"],"people.json")]);
    session.invalidate();
    release({source_ref:"sf3source_1234567890",source_sha256:sha,expires_at:"2099-01-01T00:00:00Z"});
    await pending;
    expect(service.preflight).not.toHaveBeenCalled(); expect(session.state.plan).toBeNull();
    expect(session.state.identityInvalid).toBe(true);
  });
  it("uses only GET result recovery and never confirms while checking the original locator", async () => {
    const service=api(); const session=createPackageSession(service);
    await session.recover(plan().plan_ref);
    expect(service.result).toHaveBeenCalledWith(plan().plan_ref);
    expect(service.confirm).not.toHaveBeenCalled(); expect(service.prepare).not.toHaveBeenCalled();
    expect(session.state.result?.state).toBe("pending");
    expect(session.state.submitted).toBe(true);
  });
  it("requires one complete authorized preview for every exact writer change",()=>{
    expect(hasCompletePackageReview(decodePackagePlan(completePlan()))).toBe(true);
    expect(hasCompletePackageReview(decodePackagePlan({...completePlan(),change_preview:[]}))).toBe(false);
    expect(hasCompletePackageReview(decodePackagePlan({...completePlan(),preview_status:"incomplete"}))).toBe(false);
    expect(hasCompletePackageReview(decodePackagePlan({...completePlan(),change_preview:[{...completePlan().change_preview[0],source_key:"person:other"}]}))).toBe(false);
    expect(hasCompletePackageReview(decodePackagePlan({...completePlan(),changes:[plan().changes[0],plan().changes[0]],change_preview:[completePlan().change_preview[0],{...completePlan().change_preview[0],source_key:"person:other"}]}))).toBe(false);
  });
  it("binds explicit confirmation to the exact preview and never resubmits an unknown result",async()=>{
    const service=api();service.preflight=vi.fn().mockResolvedValue(decodePackagePlan(completePlan()));
    service.prepare=vi.fn().mockResolvedValue({plan_ref:plan().plan_ref,plan_sha256:sha,credential:"sf3credential_private_12345",expires_at:"2099-01-01T00:00:00Z"});
    service.confirm=vi.fn().mockRejectedValue(new Error("response lost"));
    const session=createPackageSession(service);
    await session.preflight(parsePackageManifest(manifest()),[new File(["x"],"people.json")]);
    expect(session.state.canConfirm).toBe(true);expect(await session.prepare()).toBe(true);
    expect(JSON.stringify(session.state)).not.toContain("sf3credential");
    expect(await session.confirm(false)).toBe(false);expect(service.confirm).not.toHaveBeenCalled();
    expect(await session.confirm(true)).toBe(false);expect(await session.confirm(true)).toBe(false);
    expect(service.confirm).toHaveBeenCalledTimes(1);
    expect(await session.preflight(parsePackageManifest(manifest()),[new File(["x"],"people.json")])).toBe(false);
    await session.recover();expect(service.result).toHaveBeenCalledWith(plan().plan_ref);
  });
  it("discards a wrong-digest upload without issuing a plan request",async()=>{
    const service=api();service.upload=vi.fn().mockResolvedValue({source_ref:"sf3source_1234567890",source_sha256:"b".repeat(64),expires_at:"2099-01-01T00:00:00Z"});
    const session=createPackageSession(service);
    expect(await session.preflight(parsePackageManifest(manifest()),[new File(["x"],"people.json")])).toBe(false);
    expect(service.preflight).not.toHaveBeenCalled();expect(service.discard).toHaveBeenCalledWith("sf3source_1234567890");
  });
});


describe("F7 selected authorized forms",()=>{
  it("preflights selected form drafts without manual manifest or upload and keeps confirmation separate",async()=>{
    const service=api();service.preflightSelected=vi.fn().mockResolvedValue(decodePackagePlan(completePlan()));
    const session=createPackageSession(service);
    const items=[{family:"directory" as const,request:{fact_kind:"person",operation:"end" as const,fields:{},target_ref:"opaque-person-reference",reason:"approved change"}}];
    expect(await session.preflightSelected(items)).toBe(true);
    expect(service.preflightSelected).toHaveBeenCalledWith(items);
    expect(service.upload).not.toHaveBeenCalled();expect(service.prepare).not.toHaveBeenCalled();expect(service.confirm).not.toHaveBeenCalled();
    expect(session.state.canConfirm).toBe(true);
  });
  it("discards selected-form late preview on identity change and refuses repeated submission",async()=>{
    const service=api();let release!:(v:unknown)=>void;
    service.preflightSelected=vi.fn().mockImplementation(()=>new Promise(resolve=>{release=resolve;}));
    const session=createPackageSession(service);
    const items=[{family:"directory" as const,request:{fact_kind:"person",operation:"end" as const,fields:{},target_ref:"opaque-reference",reason:"change"}}];
    const pending=session.preflightSelected(items);
    expect(await session.preflightSelected(items)).toBe(false);
    session.invalidate();release(decodePackagePlan(completePlan()));expect(await pending).toBe(false);
    expect(session.state.plan).toBeNull();expect(service.prepare).not.toHaveBeenCalled();
  });
});

it("keeps registered teaching source options when an approved workbook manifest is imported",()=>{
  const input=manifest();const options={effective_at:"2026-09-24T00:00:00Z",target_refs:["f4:teaching_course:authorized"],reason:"approved changes",fact_kind:"teaching_course"};
  const source={...input.sources[0],mapping_version:"f7_teaching_xlsx_v1",fact_kinds:["teaching_course"],domain_options:options};
  expect(parsePackageManifest({...input,sources:[source]}).sources[0].domain_options).toEqual(options);
  expect(()=>parsePackageManifest({...input,sources:[{...source,domain_options:{...options,target_refs:[]}}]})).toThrow();
});
it("clears selected preview authorization on revoked permissions without trying formal execution",async()=>{
  const service=api();service.preflightSelected=vi.fn().mockRejectedValue(new PackageApiError(403));
  const session=createPackageSession(service);expect(await session.preflightSelected([{family:"directory",request:{fact_kind:"person",operation:"end",fields:{},target_ref:"ref",reason:"end"}}])).toBe(false);
  expect(session.state.identityInvalid).toBe(true);expect(session.state.plan).toBeNull();expect(service.confirm).not.toHaveBeenCalled();
});
