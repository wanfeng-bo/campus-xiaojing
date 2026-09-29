import { createApp, nextTick, ref, h } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import FamilyProfilePanel from "../src/features/school_facts/FamilyProfilePanel.vue";
import { isFamilyProfile } from "../src/features/school_facts/family_contracts";

const stamp = "2026-09-13T00:00:00Z";
const result = { kind: "family_student_profile", status: "available", relationship_kind: "verified_guardian",
  query_scope: "当前明确选择的孩子及本次获准字段", profile: { name: "合成孩子甲", enrollment_status: "enrolled", campus: "合成东区" },
  sources: [], missing: ["region"], as_of: stamp, queried_at: stamp,
  authorized_fields: ["name", "enrollment_status", "campus", "student_number", "residence_mode", "region"], field_restrictions: {},
  coverage: { status: "unknown", meaning: "未声明学校资料已完整覆盖" } };
let app: ReturnType<typeof createApp> | undefined;
afterEach(() => { app?.unmount(); app=undefined; document.body.replaceChildren(); vi.unstubAllGlobals(); });
async function flush() { await new Promise(resolve => setTimeout(resolve,0)); await nextTick(); }

it('loads report options only on explicit intent and keeps profile reading independent', async()=>{
  const request=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(result),{status:200})).mockResolvedValueOnce(new Response('{}',{status:503}));vi.stubGlobal('fetch',request);
  const host=document.createElement('div');document.body.append(host);app=createApp(FamilyProfilePanel,{identityLabel:'监护人 · 合成孩子甲'});app.mount(host);
  expect(request).not.toHaveBeenCalled();
  (host.querySelector('[data-action="query-family-profile"]') as HTMLButtonElement).click();await flush();
  expect(request).toHaveBeenCalledTimes(1);expect(host.querySelector('[data-role="family-profile-result"]')).not.toBeNull();
  (host.querySelector('[data-action="prepare-family-correction"]') as HTMLButtonElement).click();await flush();
  expect(request.mock.calls[1][0]).toBe('/api/v1/school-fact-product/family/corrections/options');
  expect(host.querySelector('[data-role="family-profile-result"]')).not.toBeNull();expect(host.textContent).toContain('现有查询结果不受影响');
});

it('shows neutral correction guidance after the current child receives authorized options', async()=>{
  const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({items:[{field_name:'campus',label:'当前校区',reference:'current-campus'}]}),{status:200}));
  vi.stubGlobal('fetch',request);
  const host=document.createElement('div');document.body.append(host);
  app=createApp(FamilyProfilePanel,{identityLabel:'监护人 · 当前孩子'});app.mount(host);
  (host.querySelector('[data-action="prepare-family-correction"]') as HTMLButtonElement).click();await flush();
  expect(request).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain('监护人 · 当前孩子');
  expect(host.querySelector('textarea')?.placeholder).toBe('仅说明这项资料的问题，请勿填写证件号码或无关私人资料。');
  expect(host.textContent).not.toContain('合成');
});

it('discards late report options after changing child', async()=>{
  let accept!:(r:Response)=>void;vi.stubGlobal('fetch',vi.fn(()=>new Promise(r=>accept=r)));
  const label=ref('监护人 · 合成孩子甲');const host=document.createElement('div');document.body.append(host);
  app=createApp({render:()=>h(FamilyProfilePanel,{identityLabel:label.value})});app.mount(host);
  (host.querySelector('[data-action="prepare-family-correction"]') as HTMLButtonElement).click();await nextTick();
  label.value='监护人 · 合成孩子乙';await nextTick();
  accept(new Response(JSON.stringify({items:[{field_name:'campus',label:'当前校区',reference:'previous-child-reference'}]}),{status:200}));await flush();
  expect(host.querySelector('[data-role="correction-field"]')).toBeNull();
});

it("rejects unknown private fields and denies payloads that carry child data", () => {
  expect(isFamilyProfile(result)).toBe(true);
  expect(isFamilyProfile({ ...result, profile: {...result.profile, full_address:"hidden"}})).toBe(false);
  expect(isFamilyProfile({...result, status:"denied"})).toBe(false);
  expect(isFamilyProfile({...result, relationship_kind:"recognized_caregiver", profile:{name:"合成孩子", student_number:"secret"}})).toBe(false);
});

it("reads only selected child and hides stale private content on invalidation", async () => {
  const request = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), {status:200}));
  vi.stubGlobal("fetch", request);
  const invalid = ref(false);
  const host = document.createElement("div"); document.body.append(host);
  app=createApp({render:()=>h(FamilyProfilePanel,{identityLabel:"监护人 · 合成孩子甲",identityInvalid:invalid.value})});
  app.mount(host);
  (host.querySelector('[data-action="query-family-profile"]') as HTMLButtonElement).click(); await flush();
  expect(host.textContent).toContain("合成孩子甲");
  expect(host.textContent).toContain("在校");
  expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({});
  expect(host.querySelector("details")?.textContent).toContain("未声明学校资料已完整覆盖");
  invalid.value=true; await flush();
  expect(host.querySelector('[data-role="family-profile-result"]')).toBeNull();
});

it("discards a late response after identity expiry",async()=>{
  let accept!: (value:Response)=>void;
  vi.stubGlobal("fetch",vi.fn(()=>new Promise(resolve=>{accept=resolve;})));
  const invalid=ref(false); const host=document.createElement("div");document.body.append(host);
  app=createApp({render:()=>h(FamilyProfilePanel,{identityLabel:"当前孩子",identityInvalid:invalid.value})});app.mount(host);
  (host.querySelector('[data-action="query-family-profile"]') as HTMLButtonElement).click();await nextTick();
  invalid.value=true;await nextTick();
  accept(new Response(JSON.stringify(result),{status:200}));await flush();
  expect(host.querySelector('[data-role="family-profile-result"]')).toBeNull();
});


it("sends family queries only to the fixed family endpoint without directory scope hints",async()=>{
  const {createDirectoryApi}=await import("../src/features/school_facts/directory_api");
  const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({kind:"directory_facts",status:"no_current_records",fact_kind:"student",records:[],matched_count:0,returned_count:0,truncated:false,next_cursor:null,queried_at:stamp,factual_at:stamp,coverage:{status:"unknown",description:"未声明完整覆盖"}}),{status:200}));
  vi.stubGlobal("fetch",request);
  await createDirectoryApi("family").query({fact_kind:"student",scope:"all_authorized",query_text:"合成学生甲"});
  expect(request.mock.calls[0][0]).toBe("/api/v1/school-fact-product/family-facts/query");
  expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({fact_kind:"student",query_text:"合成学生甲"});
});


it("keeps conflicting fields separate from missing values and technical source details", async () => {
  const conflict = {...result, status:"conflict", profile:{name:"合成孩子甲",campus:null}, missing:[], conflicting_fields:["campus"],
    sources:[{fact:"person",reference:"publication:synthetic",published_at:stamp,channel:"人工正式发布",source_name:null,source_trace_reference:"sf3:synthetic-trace"}]};
  expect(isFamilyProfile(conflict)).toBe(true);
  expect(isFamilyProfile({...conflict,missing:["campus"]})).toBe(false);
  expect(isFamilyProfile({...conflict,sources:[{...conflict.sources[0],source_name:{internal:"invalid"}}]})).toBe(false);
  vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify(conflict),{status:200})));
  const host=document.createElement("div");document.body.append(host);
  app=createApp({render:()=>h(FamilyProfilePanel,{identityLabel:"当前孩子",identityInvalid:false})});app.mount(host);
  (host.querySelector('[data-action="query-family-profile"]') as HTMLButtonElement).click();await flush();
  expect(host.querySelector("dl")?.textContent).toContain("记录不一致，暂无法确定");
  expect(host.querySelector("dl")?.textContent).not.toContain("尚未提供");
  expect(host.querySelector("details")?.textContent).toContain("人工正式发布");
  expect(host.textContent).not.toContain("sf3:synthetic-trace");
});
