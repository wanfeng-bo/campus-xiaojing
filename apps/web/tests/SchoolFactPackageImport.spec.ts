import { createApp, nextTick, h, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import SchoolFactPackageImport from "../src/features/school_facts/SchoolFactPackageImport.vue";

afterEach(()=>{document.body.replaceChildren();vi.unstubAllGlobals();});
describe("F7 package page",()=>{
  it("shows an approved manifest and multiple-file chooser without inventing input metadata",()=>{
    const host=document.createElement("div");document.body.append(host);
    const app=createApp(SchoolFactPackageImport,{identityLabel:"当前测试任职",identityEpoch:1});app.mount(host);
    expect(host.textContent).toContain("已批准的资料包清单");
    expect(host.querySelector('input[data-role="package-files"]')?.hasAttribute("multiple")).toBe(true);
    expect(host.querySelector('[data-action="package-confirm"]')).toBeNull();app.unmount();
  });
  it("clears identity-bound drafts and blocks actions when the identity expires",async()=>{
    const host=document.createElement("div");document.body.append(host);
    const api={upload:vi.fn(),discard:vi.fn(),preflight:vi.fn(),prepare:vi.fn(),confirm:vi.fn(),result:vi.fn()};
    const app=createApp(SchoolFactPackageImport,{identityLabel:"当前测试任职",identityEpoch:1,identityInvalid:true,api});app.mount(host);
    await nextTick();
    expect(host.textContent).toContain("当前身份已失效");
    expect((host.querySelector('[data-action="package-preflight"]') as HTMLButtonElement).disabled).toBe(true);
    expect(api.prepare).not.toHaveBeenCalled();expect(api.confirm).not.toHaveBeenCalled();app.unmount();
  });
  it("shows full approved before/after and linked impacts before an explicit confirmation",async()=>{
    const sha="a".repeat(64),planRef="sf3plan_1234567890",at="2026-09-24T00:00:00Z";
    const oldText="原始正文".repeat(2500),newText="完整修改正文".repeat(2500);
    const manifest={input_version:"school_facts_v5",school_id:"00000000-0000-0000-0000-000000000001",base_version:1,mode:"incremental",sources:[{logical_source_code:"people",source_sha256:sha,size_bytes:1,mapping_version:"school_facts_source_v5",mapping_config_sha256:sha,responsibility_reference:"approved",fact_kinds:["person"]}]};
    const change={fact_kind:"person",source_key:"person:one",operation:"upsert",expected_version:1,effective_at:at,sheet_code:"f7_s1",row_number:2,fields:[{field_code:"display_name",new_value:newText}],replacement_segments:[],correction_reason:null,cause_source_key:null,cause_version:null};
    const plan={plan_ref:planRef,plan_sha256:sha,is_clean:true,issues:[],changes:[change],status_decisions:[],replacement_preview:[],preview_status:"complete",preview_issues:[],change_preview:[{fact_kind:"person",source_key:"person:one",operation:"upsert",expected_version:1,effective_at:at,origin:"linked",before:{display_name:oldText},after:{display_name:newText},before_segments:[],after_segments:[],domain_preview:{notice:"完整关联影响已核对"}}]};
    const api={upload:vi.fn().mockResolvedValue({source_ref:"sf3source_1234567890",source_sha256:sha,expires_at:"2099-01-01T00:00:00Z"}),discard:vi.fn(),preflight:vi.fn().mockResolvedValue(plan),prepare:vi.fn().mockResolvedValue({plan_ref:planRef,plan_sha256:sha,credential:"sf3credential_private_12345",expires_at:"2099-01-01T00:00:00Z"}),confirm:vi.fn().mockResolvedValue({plan_ref:planRef,batch_id:"batch:one",target_version:2,changed_count:1,replayed:false,cleanup_pending:false}),result:vi.fn()};
    const invalid=ref(false);const host=document.createElement("div");document.body.append(host);
    const app=createApp({setup:()=>()=>h(SchoolFactPackageImport,{identityLabel:"测试任职",identityEpoch:1,identityInvalid:invalid.value,api})});app.mount(host);
    function file(selector:string,values:File[]){const input=host.querySelector(selector)!;Object.defineProperty(input,"files",{configurable:true,value:values});input.dispatchEvent(new Event("change",{bubbles:true}));}
    file('[data-role="package-manifest"]',[new File([JSON.stringify(manifest)],"manifest.json")]);
    await vi.waitFor(()=>expect(host.querySelector('[data-slot="people"]')).not.toBeNull());
    file('[data-role="package-files"]',[new File(["x"],"people.json")]);await nextTick();
    const select=host.querySelector('select[data-slot="people"]') as HTMLSelectElement;select.value="0";select.dispatchEvent(new Event("change",{bubbles:true}));await nextTick();
    (host.querySelector('[data-action="package-preflight"]') as HTMLButtonElement).click();
    await vi.waitFor(()=>expect(host.textContent).toContain("完整关联影响已核对"));
    expect(host.textContent).toContain(oldText);expect(host.textContent).toContain(newText);
    expect(api.prepare).not.toHaveBeenCalled();expect(api.confirm).not.toHaveBeenCalled();
    const acknowledgment=host.querySelector('input[type="checkbox"]') as HTMLInputElement;acknowledgment.checked=true;acknowledgment.dispatchEvent(new Event("change",{bubbles:true}));await nextTick();
    (Array.from(host.querySelectorAll("button")).find(b=>b.textContent==="准备正式确认")!).click();
    await vi.waitFor(()=>expect(host.querySelector('[data-action="package-confirm"]')).not.toBeNull());
    expect(host.textContent).not.toContain("sf3credential");
    (host.querySelector('[data-action="package-confirm"]') as HTMLButtonElement).click();
    await vi.waitFor(()=>expect(api.confirm).toHaveBeenCalledTimes(1));
    invalid.value=true;await nextTick();expect(host.textContent).not.toContain(oldText);expect(host.textContent).not.toContain(newText);app.unmount();
  });
});


it("selects an authorized object and stages its reference without hand-written IDs or JSON",async()=>{
  const at="2026-09-24T00:00:00Z";
  const record={reference:"opaque-person-current",fact_kind:"person",label:"Teacher Example",fields:{display_name:"Teacher Example"},version:2,valid_from:at,valid_to:null,source:{label:"Published",kind:"manual",reference:"source-current",published_at:at}};
  const directory={catalog:vi.fn().mockResolvedValue({families:[{value:"person",label:"People"}]}),
    describe:vi.fn().mockResolvedValue({fact_kind:"person",label:"People",fields:[{name:"display_name",label:"Name",type:"text",required:true,reference_kind:null,options:[]}],creation_choices:[]}),
    query:vi.fn().mockResolvedValue({kind:"directory_facts",status:"known",fact_kind:"person",records:[record],matched_count:1,returned_count:1,truncated:false,next_cursor:null,queried_at:at,factual_at:at,coverage:{status:"unknown",description:"authorized"}}),
    original:vi.fn(),prepare:vi.fn(),proposal:vi.fn(),confirm:vi.fn(),receipt:vi.fn()};
  const api={upload:vi.fn(),discard:vi.fn(),preflight:vi.fn(),preflightSelected:vi.fn().mockRejectedValue(new Error("offline")),prepare:vi.fn(),confirm:vi.fn(),result:vi.fn()};
  const invalid=ref(false),host=document.createElement("div");document.body.append(host);
  const app=createApp({setup:()=>()=>h(SchoolFactPackageImport,{identityLabel:"Maintainer",identityEpoch:1,identityInvalid:invalid.value,api,enabledFamilies:["directory"],directoryApis:{directory}})});app.mount(host);
  await vi.waitFor(()=>expect(host.querySelector('[data-role="directory-family"]')).not.toBeNull());
  await vi.waitFor(()=>expect(directory.describe).toHaveBeenCalled());
  await nextTick();
  const queryAll=Array.from(host.querySelectorAll<HTMLButtonElement>(".directory-workbench button")).find(b=>b.textContent==="\u67e5\u770b\u672c\u7c7b\u578b\u83b7\u6743\u96c6\u5408")!;
  await vi.waitFor(()=>expect(queryAll.disabled).toBe(false));queryAll.click();
  await vi.waitFor(()=>expect(host.querySelector('[data-action="edit-directory"]')).not.toBeNull());
  (host.querySelector('[data-action="edit-directory"]') as HTMLButtonElement).click();await nextTick();
  const field=host.querySelector('[data-field="display_name"]') as HTMLInputElement;field.value="Correct Name";field.dispatchEvent(new Event("input",{bubbles:true}));
  const reason=host.querySelector('.maintenance textarea') as HTMLTextAreaElement;reason.value="verified spelling";reason.dispatchEvent(new Event("input",{bubbles:true}));await nextTick();
  (host.querySelector('[data-action="prepare-directory"]') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(host.querySelector('[data-role="package-selected-draft"]')?.textContent).toContain("Teacher Example"));
  expect(directory.prepare).not.toHaveBeenCalled();expect(directory.confirm).not.toHaveBeenCalled();
  (host.querySelector('[data-action="package-selected-preflight"]') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(api.preflightSelected).toHaveBeenCalledOnce());
  expect(api.preflightSelected.mock.calls[0][0]).toEqual([{family:"directory",request:expect.objectContaining({target_ref:record.reference,fields:{display_name:"Correct Name"}})}]);
  expect(api.upload).not.toHaveBeenCalled();expect(api.confirm).not.toHaveBeenCalled();
  invalid.value=true;await nextTick();expect(host.querySelector('[data-role="package-selected-draft"]')?.textContent).not.toContain("Teacher Example");expect(host.querySelector('.directory-workbench')).toBeNull();app.unmount();
});
