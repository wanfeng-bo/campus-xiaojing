import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import SchoolFactsApp from "../src/app/SchoolFactsApp.vue";

let app: App | undefined;
afterEach(() => { app?.unmount(); app = undefined; document.body.replaceChildren(); vi.unstubAllGlobals(); });
function reply(value: unknown) { return new Response(JSON.stringify(value), {status: 200}); }
const staff = {status:"signed_in", accounts:[], contexts:[{ref:"staff.1",label:"陈老师 · 当前任职"}], selected_context_ref:"staff.1", can_maintain:true};
async function mount(session = staff, features: Record<string, unknown> = {assistant_enabled:false}) {
  const fetcher = vi.fn(async (url: string) => {
    if(url.endsWith("/features")) return reply(features);
    if(url.endsWith("/session")) return reply(session);
    if(url.endsWith("/catalog")) return reply({families:[]});
    return reply({});
  });
  vi.stubGlobal("fetch", fetcher);
  const host = document.createElement("div"); document.body.append(host);
  app = createApp(SchoolFactsApp, {formal:true}); app.mount(host);
  await vi.waitFor(() => expect(host.querySelector('[data-action="open-work-center"]')).not.toBeNull());
  return {host,fetcher};
}
it("opens existing direct controls without sending queries and keeps drafts mounted after closing", async () => {
  const {host,fetcher} = await mount();
  const open = host.querySelector<HTMLButtonElement>('[data-action="open-work-center"]')!;
  open.focus(); open.click(); await nextTick();
  const drawer = host.querySelector<HTMLDialogElement>('[data-role="work-center"]')!;
  expect(drawer.open).toBe(true);
  expect(drawer.textContent).toContain("工作中心");
  const input = drawer.querySelector<HTMLInputElement>('[data-role="campus-name"]')!;
  input.value = "待查询校区"; input.dispatchEvent(new Event("input", {bubbles:true}));
  drawer.querySelector<HTMLButtonElement>('[data-action="close-work-center"]')!.click(); await nextTick();
  expect(drawer.open).toBe(false); expect(document.activeElement).toBe(open);
  open.click(); await nextTick();
  expect(drawer.querySelector('[data-role="campus-name"]')).toBe(input);
  expect(input.value).toBe("待查询校区");
  drawer.dispatchEvent(new KeyboardEvent("keydown", {key:"Escape",bubbles:true})); await nextTick();
  expect(drawer.open).toBe(false); expect(document.activeElement).toBe(open);
  expect(fetcher.mock.calls.some(([url]) => /\/(query|prepare|confirm)$/.test(url))).toBe(false);
  expect(fetcher.mock.calls.some(([url]) => url.includes("/assistant/"))).toBe(false);
});
it("only advertises available factual areas, not fake notifications or unbuilt tasks", async () => {
  const {host} = await mount({...staff,directory_enabled:true,family_enabled:true,education_enabled:true} as typeof staff, {assistant_enabled:false,f6_features_enabled:true});
  const shortcuts = [...host.querySelectorAll('[data-role="home-shortcut"]')];
  expect(shortcuts.map(item => item.textContent)).toEqual(expect.arrayContaining([
    expect.stringContaining("学校资料"),expect.stringContaining("学生与家校"),expect.stringContaining("班级与教学"),expect.stringContaining("住宿资料"),
  ]));
  expect(host.textContent).not.toMatch(/待审批请假|考勤打卡|成绩查询|待发布通知/);
  expect(host.querySelector('[data-action="shell-new-conversation"]')).toBeNull();
  (shortcuts.find(item => item.getAttribute("data-area") === "education") as HTMLButtonElement).click(); await nextTick();
  expect(host.querySelector<HTMLDialogElement>('[data-role="work-center"]')?.open).toBe(true);
  expect(host.querySelector<HTMLDetailsElement>('[data-role="staff-maintenance"]')?.open).toBe(true);
});
it("keeps an unbound formal identity in the same visual shell without engineering accounts or useful-looking fake controls", async () => {
  vi.stubGlobal("fetch",vi.fn(async(url:string)=>reply(url.endsWith("/session") ? {...staff,contexts:[],selected_context_ref:null} : {assistant_enabled:false})));
  const host=document.createElement("div"); document.body.append(host);
  app=createApp(SchoolFactsApp,{formal:true}); app.mount(host);
  await vi.waitFor(()=>expect(host.textContent).toContain("尚未完成绑定"));
  expect(host.querySelector('[data-role="xiaojing-brand"]')).not.toBeNull();
  expect(host.querySelector('[data-action="open-work-center"]')).toBeNull();
  expect(host.querySelector('#engineering-account')).toBeNull();
  expect(host.querySelector('[data-role="home-shortcut"]')).toBeNull();
});
it("parent cards do not suggest a disabled assistant", async () => {
  const parent = {...staff, can_maintain:false, family_enabled:true, family_conversation_available:true, assistant_context_key:"family.1", selected_context_kind:"guardian_relationship",contexts:[{ref:"child.1",label:"林同学",kind:"guardian_relationship"}],selected_context_ref:"child.1"};
  const {host} = await mount(parent as typeof staff);
  expect(host.textContent).not.toContain("向小静提问");
  expect(host.querySelector('[data-action="query-family-profile"]')).not.toBeNull();
  expect(host.querySelector('#work-center-child')).not.toBeNull();
});

it("shows a failed direct query inside the open work center and never as a hidden background alert", async () => {
  const {host} = await mount();
  host.querySelector<HTMLButtonElement>('[data-action="open-work-center"]')!.click(); await nextTick();
  const input=host.querySelector<HTMLInputElement>('[data-role="campus-name"]')!;
  input.value="待查询校区"; input.dispatchEvent(new Event("input",{bubbles:true})); await nextTick();
  host.querySelector<HTMLFormElement>('[data-role="contact-query"]')!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));
  await vi.waitFor(()=>expect(host.querySelector('[data-role="work-center"] [role="alert"]')).not.toBeNull());
  expect(host.querySelectorAll('.school-facts-app > [role="alert"]')).toHaveLength(0);
});

it("keyboard wrap skips nested summaries inside a collapsed maintenance section", async () => {
  const {host} = await mount({...staff,directory_enabled:true} as typeof staff);
  host.querySelector<HTMLButtonElement>('[data-action="open-work-center"]')!.click(); await nextTick();
  const outer=host.querySelector<HTMLDetailsElement>('[data-role="staff-maintenance"]')!;
  outer.open=false;
  const nested=document.createElement("details"); nested.innerHTML='<summary>维护明细</summary><button>额外操作</button>'; outer.append(nested);
  const close=host.querySelector<HTMLButtonElement>('[data-action="close-work-center"]')!; close.focus();
  close.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",shiftKey:true,bubbles:true,cancelable:true}));
  expect(document.activeElement).toBe(outer.querySelector(':scope > summary'));
});

it.each([false,true])("a campus selected from the conversation opens its real result (directory=%s)",async(directory_enabled)=>{
  const campus={reference:"campus.first",label:"合成中心校区",path:"合成学校／中心校区"};
  const base={display_notice:null,status:"known",candidates:[],coverage:{status:"unknown",description:"获准校区"},queried_at:"2026-09-27T00:00:00Z"};
  const listed={...base,query:{subject:"campuses",scope:"all_authorized",campus_name:null,campus_ref:null},resolved_campuses:[campus],listing:{entity:"campuses",returned_count:1,matched_count:1,truncated:false},items:[]};
  const result={...base,query:{subject:"official_contacts",scope:"selected",campus_name:null,campus_ref:campus.reference},resolved_campuses:[campus],listing:{entity:"contacts",returned_count:1,matched_count:1,truncated:false},items:[{reference:"contact.1",campus_ref:campus.reference,campus_name:campus.label,contact_kind:"email",contact_value:"office@example.invalid",service_hours:null,valid_from:"2026-09-01T00:00:00Z",valid_to:null,version:1,source:{label:"合成发布资料",published_at:"2026-09-01T00:00:00Z",reference:"source.1"}}]};
  const candidate={kind:"directory_facts",status:"ambiguous",fact_kind:"person",records:[1,2].map(index=>({reference:`person.${index}`,fact_kind:"person",label:`合成教师${index}`,fields:{display_name:`合成教师${index}`},version:1,valid_from:"2026-09-01T00:00:00Z",valid_to:null,source:{label:"合成发布资料",reference:null,published_at:"2026-09-01T00:00:00Z"}})),matched_count:2,returned_count:2,truncated:false,next_cursor:null,queried_at:"2026-09-27T00:00:00Z",factual_at:"2026-09-27T00:00:00Z",coverage:{status:"unknown",description:"获准范围"}};
  const fetcher=vi.fn(async(url:string)=>reply(url.endsWith("/session")?{...staff,directory_enabled}:url.endsWith("/features")?{assistant_enabled:true}:url.endsWith("/catalog")?{families:[]}:url.endsWith("/contacts/query")?result:{version:"1",conversation_ref:"conversation.1",generation:1,active_run:null,turns:[{turn_ref:"turn.1",run_ref:"run.1",generation:1,teacher_utterance:"查看可查询校区",visible_state:"completed",display_kind:"card",display_payload:{text:"可选择校区继续查看。",facts:directory_enabled?[listed,candidate]:[listed]},controls:[]}]}));
  vi.stubGlobal("fetch",fetcher);
  const host=document.createElement("div");document.body.append(host);app=createApp(SchoolFactsApp,{formal:true});app.mount(host);
  if(directory_enabled){
    await vi.waitFor(()=>expect(host.querySelectorAll('[aria-label="候选对象入口"] button')).toHaveLength(2));
    expect(host.querySelector('.assistant-panel [data-action="select-campus"]')).toBeNull();
    expect(fetcher.mock.calls.some(([url])=>url.endsWith("/contacts/query"))).toBe(false);
    return;
  }
  await vi.waitFor(()=>expect(host.querySelector('.assistant-panel [data-action="select-campus"]')).not.toBeNull());
  expect(fetcher.mock.calls.some(([url])=>url.endsWith("/contacts/query"))).toBe(false);
  host.querySelector<HTMLButtonElement>('.assistant-panel [data-action="select-campus"]')!.click();
  await vi.waitFor(()=>expect(host.querySelector('[data-role="work-center"]')?.textContent).toContain("office@example.invalid"));
  expect(host.querySelector<HTMLDialogElement>('[data-role="work-center"]')?.open).toBe(true);
  expect(fetcher.mock.calls.filter(([url])=>url.endsWith("/contacts/query"))).toHaveLength(1);
});
