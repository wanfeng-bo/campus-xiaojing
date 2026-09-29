import { describe, expect, it, vi, afterEach } from 'vitest';
import { isDirectoryDescriptor, isDirectoryProposal } from '../src/features/school_facts/directory_contracts';
import { createDirectoryApi } from '../src/features/school_facts/directory_api';
const stamp = '2026-09-13T00:00:00Z';
const summary = { label:'初二三班', reference:'f4:class:one', fact_kind:'education_class', operation:'correct', before:{teacher_role_limits:{primary:1}}, after:{teacher_role_limits:{primary:2}}, effective_at:stamp, reason:'核实补正', source_label:'学校登记' };
afterEach(() => vi.unstubAllGlobals());
describe('F4 maintenance contracts', () => {
 it('accepts typed references and finite role limits', () => {
  expect(isDirectoryDescriptor({ fact_kind:'education_class',label:'班级',fields:[{name:'grade_refs',label:'年级顺序',type:'references',required:true,reference_kind:'education_grade',options:[]}],creation_choices:[] })).toBe(true);
 });
 it('accepts all batch impacts and rejects malformed impact', () => {
  expect(isDirectoryProposal({proposal_ref:'p',expires_at:stamp,summary:{...summary,impacts:[summary]}})).toBe(true);
  expect(isDirectoryProposal({proposal_ref:'p',expires_at:stamp,summary:{...summary,impacts:[{...summary,after:{teacher_role_limits:{injected:'text'}}}]}})).toBe(false);
 });
 it('routes education preparation and retains exact batch fields', async () => {
  const fetcher=vi.fn(async(...args: any[])=>new Response(JSON.stringify({proposal_ref:'p',expires_at:stamp,summary}),{status:200}));
  vi.stubGlobal('fetch',fetcher);
  await createDirectoryApi('education').prepare({fact_kind:'education_class',target_ref:'f4:class:one',operation:'correct',fields:{},reason:'核实补正',additional_changes:[],purpose:'historical_correction'});
  expect(fetcher.mock.calls[0][0]).toContain('/education-facts/prepare');
  expect(JSON.parse(fetcher.mock.calls[0][1].body).purpose).toBe('historical_correction');
 });
});

it('renders every impact before confirmation and routes education reviews to its owner', async () => {
 const {createApp,h,nextTick}=await import('vue');
 const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const {default:Answer}=await import('../src/features/school_facts/product_answer.vue');
 const proposal={proposal_ref:'p',expires_at:stamp,summary:{...summary,impacts:[summary,{...summary,label:'结束原班内职务',reference:'position:old',after:null,operation:'end'}]}};
 const host=document.createElement('div'); const app=createApp(Proposal,{proposal,complete:true}); app.mount(host); await nextTick();
 expect(host.querySelector('[aria-label="全部联动修改"]')?.textContent).toContain('结束原班内职务'); expect(host.textContent).toContain('班主任：2'); app.unmount();
 const review=vi.fn(); const chat=createApp({render:()=>h(Answer,{payload:{facts:[{...proposal,kind:'fact_proposal',family_owner:'school_facts.education'}]},onReviewEducation:review})});chat.mount(host);await nextTick();
 (host.querySelector('[data-action="review-directory-proposal"]') as HTMLButtonElement).click();expect(review).toHaveBeenCalledWith('p');chat.unmount();
});

it('keeps an exact staged batch and stops drafts from crossing identity loss', async () => {
 const {createDirectoryController}=await import('../src/features/school_facts/directory_session');
 const rows=['one','two'].map(id=>({reference:id,fact_kind:'education_class',label:id,fields:{display_name:id},version:1,valid_from:stamp,valid_to:null,source:{label:'学校',reference:null,published_at:stamp}}));
 const api:any={catalog:vi.fn(async()=>({families:[{value:'education_class',label:'班级'}]})),describe:vi.fn(async()=>({fact_kind:'education_class',label:'班级',fields:[{name:'display_name',label:'名称',type:'text',required:true,options:[],reference_kind:null}],creation_choices:[]})),query:vi.fn(async()=>({fact_kind:'education_class',records:rows})),prepare:vi.fn(async(input:any)=>({proposal_ref:'p',expires_at:stamp,summary:{...summary,fact_kind:input.fact_kind,reference:input.target_ref,operation:input.operation,reason:input.reason,after:input.fields}}))};
 const c=createDirectoryController(api);await c.open();await c.chooseFamily('education_class');await c.query({fact_kind:'education_class',scope:'all_authorized'});c.edit('one');c.setReason('第一项');c.setField('display_name','甲');expect(c.stageCurrent()).toBe(true);
 c.edit('two');c.setReason('第二项');c.setField('display_name','乙');await c.prepare();expect(api.prepare.mock.calls[0][0].additional_changes[0].target_ref).toBe('one');expect(api.prepare.mock.calls[0][0].target_ref).toBe('two');
 c.invalidateIdentity();expect(c.stageCurrent()).toBe(false);expect(c.setBatchReference('student_ref','student',['forged'])).toBe(false);await c.prepare();expect(api.prepare).toHaveBeenCalledTimes(1);c.dispose();
});

it('validates and shows every before and after time segment', async () => {
 const segments=[{effective_at:stamp,valid_to:'2027-01-01T00:00:00Z',fields:{display_name:'历史段'}},{effective_at:'2027-01-01T00:00:00Z',valid_to:null,fields:{display_name:'独立未来段'}}];
 const proposal={proposal_ref:'p',expires_at:stamp,summary:{...summary,before_segments:segments,after_segments:[...segments,{effective_at:'2028-01-01T00:00:00Z',valid_to:null,fields:{display_name:'第三个正式段'}}]}};
 expect(isDirectoryProposal({...proposal,summary:{...proposal.summary,after_segments:[{...segments[0],valid_to:'bad'}]}})).toBe(false);
 const {createApp,nextTick}=await import('vue'); const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const host=document.createElement('div');const app=createApp(Proposal,{proposal,complete:true});app.mount(host);await nextTick();
 expect(host.textContent).toContain('历史段');expect(host.textContent).toContain('独立未来段');expect(host.textContent).toContain('第三个正式段');app.unmount();
});


it('offers only the selected education report field and sends its exact reference', async () => {
 const {createApp,nextTick}=await import('vue'); const {default:Workbench}=await import('../src/features/school_facts/DirectoryWorkbench.vue');
 const key='f4:student_class_membership:signed';
 const row={reference:key,fact_kind:'student_class_membership',label:'孩子归班',fields:{},version:1,valid_from:stamp,valid_to:null,source:{label:'学校',reference:null,published_at:stamp}};
 const api:any={catalog:vi.fn(async()=>({families:[{value:row.fact_kind,label:'归班'}]})),describe:vi.fn(async()=>({fact_kind:row.fact_kind,label:'归班',fields:[],creation_choices:[]})),query:vi.fn(async()=>({fact_kind:row.fact_kind,status:'available',records:[row],queried_at:stamp,coverage:{status:'partial',description:'当前获权'},returned_count:1,matched_count:1,truncated:false}))};
 const request=vi.fn(async(...args:any[])=>new Response(JSON.stringify({items:[]}),{status:200}));vi.stubGlobal('fetch',request);
 const host=document.createElement('div');const app=createApp(Workbench,{api,education:true,familyCorrections:true,identityLabel:'当前老师'});app.mount(host);
 try {
  await vi.waitFor(()=>expect(Array.from(host.querySelectorAll('button')).find(b=>b.textContent==='查看本类型获权集合')?.disabled).toBe(false));
  Array.from(host.querySelectorAll('button')).find(b=>b.textContent==='查看本类型获权集合')!.click();
  await vi.waitFor(()=>expect(host.querySelector('[data-action="edit-directory"]')).not.toBeNull());
  (host.querySelector('[data-action="edit-directory"]') as HTMLButtonElement).click();await nextTick();
  const section=host.querySelector('[aria-label="选择报错查看范围"]');expect(section).not.toBeNull();
  const select=section!.querySelector('select')!;expect(Array.from(select.options).map(o=>o.value)).toEqual(['','class_source_key']);
  expect(select.textContent).toContain('孩子班级');select.value='class_source_key';select.dispatchEvent(new Event('change'));await nextTick();
  (section!.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(request).toHaveBeenCalledOnce());
  const url=new URL(request.mock.calls[0][0] as string,'http://localhost');expect(url.searchParams.get('target_reference')).toBe(key);expect(url.searchParams.get('field_name')).toBe('class_source_key');
 } finally {app.unmount();}
});

it('shows one main timeline plus every distinct related target and changed timeline',async()=>{
 const {createApp,nextTick}=await import('vue');const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const segment={effective_at:stamp,valid_to:null,fields:{display_name:'原段'}};
 const main={...summary,before_segments:[segment],after_segments:[{...segment,fields:{display_name:'主项新段'}}]};
 const related=[1,2].map(n=>({...main,reference:'f4:class:related'+n,label:'联动'+n}));
 const changed={...main,after_segments:[{...segment,effective_at:'2027-01-01T00:00:00Z',fields:{display_name:'独立未来变化'}}]};
 const host=document.createElement('div');const app=createApp(Proposal,{proposal:{proposal_ref:'p',expires_at:stamp,summary:{...main,impacts:[structuredClone(main),...related,changed]}},complete:true});app.mount(host);await nextTick();
 try {
  const timelines=host.querySelector('[aria-label="完整时间分段"]')!;
  expect(timelines.querySelectorAll(':scope > article')).toHaveLength(4);
  expect(timelines.textContent).toContain('联动1');expect(timelines.textContent).toContain('联动2');expect(timelines.textContent).toContain('独立未来变化');
  expect(host.querySelector('[aria-label="全部联动修改"]')!.querySelectorAll('article')).toHaveLength(4);
  expect(host.querySelector('[aria-label="本次修改总数"]')?.textContent).toBe('本次全部修改（4项）');
  expect(host.querySelector('[aria-label="全部联动修改"] h4')?.textContent).toBe('关联修改明细（4项）');
 }finally{app.unmount();}
});
