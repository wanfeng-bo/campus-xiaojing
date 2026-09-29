import {createApp,h,nextTick,ref} from 'vue';
import {afterEach,expect,it,vi} from 'vitest';
import ProductAnswer from '../src/features/school_facts/product_answer.vue';
import {decodeSchoolFactPlan} from '../src/features/school_facts/school_fact_contracts';
const cleanup:Array<()=>void>=[];
afterEach(()=>{cleanup.splice(0).forEach(f=>f());document.body.replaceChildren();});
async function render(facts:unknown[]){const host=document.createElement('div');document.body.append(host);const app=createApp(ProductAnswer,{payload:{text:'目前按正式安排住宿。',facts}});app.mount(host);cleanup.push(()=>app.unmount());await nextTick();return host;}
const residence={kind:'family_residence',status:'available',as_of:'2026-09-22T00:00:00Z',queried_at:'2026-09-22T00:00:00Z',residence_mode:'boarding',coverage:{status:'unknown'},limitations:[],records:[{reference:'private-opaque-residence',fact_kind:'student_bed_residence',version:1,student_name:'林同学',building_name:'甲校区住宿楼',room_name:'301室',bed_name:'2号床',valid_from:'2026-09-01T00:00:00Z',valid_to:null,source:{kind:'formal_publication',label:'学校住宿安排',published_at:'2026-08-30T00:00:00Z'},quality:[]}]};
it('shows authorized building room bed and time as evidence without private identifiers',async()=>{const host=await render([residence]);const card=host.querySelector('[aria-label="住宿安排依据"]');expect(card).not.toBeNull();for(const text of ['林同学','甲校区住宿楼','301室','2号床'])expect(card?.textContent).toContain(text);expect(card?.textContent).not.toContain('private-opaque');expect(card?.closest('details')?.open).toBe(false);expect(host.querySelector('[role="alert"]')).toBeNull();});
it.each(['conflict','denied','unavailable','partial'])('keeps %s visible even beside another successful result',async(status)=>{const host=await render([{...residence,status,records:[]},residence]);expect(host.querySelector('[role="alert"]')?.textContent).toBeTruthy();});
it.each(['denied','unavailable'])('keeps minimal owner %s without timestamps visible',async(status)=>{const host=await render([{kind:'family_residence',status}]);expect(host.querySelector('[role="alert"]')?.textContent).toBeTruthy();});
it('keeps no current records separate from day student and unknown mode',async()=>{const host=await render([{...residence,status:'no_current_records',residence_mode:null,records:[]}]);const card=host.querySelector('[aria-label="住宿安排依据"]');expect(card?.textContent).toContain('没有当前住宿登记');expect(card?.textContent).not.toContain('走读');});
it('explains missing building purpose but does not announce normal arrangement mechanics',async()=>{const host=await render([{...residence,limitations:['building_use_not_recorded','formal_arrangement_not_actual_presence']}]);expect(host.querySelector('[role="alert"]')?.textContent).toContain('住宿用途尚未登记');expect(host.querySelector('[role="alert"]')?.textContent).not.toContain('实际到宿');expect(host.querySelector('[aria-label="住宿安排依据"]')?.textContent).toContain('正式安排不代表实际到宿');});
it('accepts an explicit unknown value in a complete maintenance difference',()=>{expect(()=>decodeSchoolFactPlan({plan_ref:'sf3plan_1234567890',plan_sha256:'a'.repeat(64),is_clean:true,issues:[],status_decisions:[],changes:[{operation:'upsert',fact_kind:'student_school_relationship',source_key:'student:one',expected_version:1,sheet_code:'relationships',row_number:2,fields:[{field_code:'residence_mode',new_value:null}]}]})).not.toThrow();});

it('routes residence maintenance HTTP to its owner',async()=>{
 const {createDirectoryApi}=await import('../src/features/school_facts/directory_api');
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(JSON.stringify({families:[]})));
 try {await createDirectoryApi('residence').catalog();expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v1/school-fact-product/residence-facts/catalog');}finally{fetcher.mockRestore();}
});
it('routes residence proposal review without sending it to education or directory',async()=>{
 const host=document.createElement('div');document.body.append(host);const review=vi.fn();
 const app=createApp(ProductAnswer,{payload:{facts:[{kind:'fact_proposal',family_owner:'school_facts.residence',proposal_ref:'proposal.residence',expires_at:'2099-01-01T00:00:00Z',summary:{label:'住宿安排',reference:'opaque',fact_kind:'student_bed_residence',operation:'upsert',before:{bed_source_key:'before'},after:{bed_source_key:'after'},effective_at:'2026-09-22T00:00:00Z',reason:'调整',source_label:'正式维护'}}]},onReviewResidence:review});app.mount(host);cleanup.push(()=>app.unmount());await nextTick();
 const button=host.querySelector('button');expect(button).not.toBeNull();button!.click();await nextTick();expect(review).toHaveBeenCalledWith('proposal.residence');
});

it('shows facility name and current basis for a future query',async()=>{const host=await render([{...residence,kind:'residence_facts',future_basis:'按目前已登记的安排',records:[{fact_kind:'dormitory_building_profile',display_name:'东住宿楼',valid_from:'2026-09-01T00:00:00Z',valid_to:null}]}]);expect(host.textContent).toContain('东住宿楼');expect(host.textContent).toContain('按目前已登记的安排');});
it('keeps formal quality risks visible outside collapsed evidence',async()=>{const host=await render([{...residence,quality:[{source_status:'unavailable',review_due_at:'2026-09-01T00:00:00Z'}]}]);const alerts=[...host.querySelectorAll('[role="alert"]')].map(el=>el.textContent).join('');expect(alerts).toContain('来源暂时无法核实');expect(alerts).toContain('需要再次核对');});

it('binds record quality warnings to the original query and granted object without attributing them to a later target',async()=>{
 const host=await render([{...residence,records:[{...residence.records[0],building_name:'乙住宿楼',room_name:'301',bed_name:'2号床',quality:[{review_due_at:'2026-09-01T00:00:00Z'}]}]},residence]);
 const alerts=[...host.querySelectorAll('[role="alert"]')].map(el=>el.textContent).join('');
 expect(alerts).toContain('住宿查询1');expect(alerts).toContain('乙住宿楼');expect(alerts).toContain('2号床');expect(alerts).not.toContain('住宿查询2');
});
it('labels collection-only quality by its query instead of guessing a record target',async()=>{
 const host=await render([{...residence,quality:[{dispute_status:'open'}]}]);
 const alerts=[...host.querySelectorAll('[role="alert"]')].map(el=>el.textContent).join('');
 expect(alerts).toContain('住宿查询1：');expect(alerts).toContain('争议');expect(alerts).not.toContain('结果范围');
});
it.each([false,true])('keeps a denied residence attempt beside a successful one without presenting a global refusal (reversed=%s)',async(reversed)=>{
 const denied={kind:'family_residence',status:'denied',subject:{child_ref:'private-child-a',label:'合成孩子甲'}};
 const known={...residence,subject:{child_ref:'private-child-a',label:'合成孩子甲'}};
 const host=await render(reversed?[known,denied]:[denied,known]);
 const top=host.querySelector('.system-notices')?.textContent??'';
 expect(top).toContain('有住宿查询未获准，可展开依据查看对应资料。');
 expect(top).not.toContain('当前身份未获准读取所问住宿资料');
 expect(top).not.toContain('已解决');expect(top).not.toContain('权限恢复');
 const cards=[...host.querySelectorAll('[aria-label="住宿安排依据"]')];expect(cards).toHaveLength(2);
 const refused=cards.find(card=>card.textContent?.includes('当前身份未获准读取所问住宿资料'))!;
 expect(refused.textContent).toContain('合成孩子甲');expect(refused.textContent).not.toContain('301室');
 expect(refused.textContent).not.toContain('2026/9/22');
 expect(refused.closest('details')?.open).toBe(false);
 const supplied=cards.find(card=>card.textContent?.includes('301室'))!;
 expect(supplied.textContent).not.toContain('当前身份未获准读取所问住宿资料');
});
it('retains different-child and different-time denials beside an authorized empty query',async()=>{
 const host=await render([
  {...residence,subject:{child_ref:'private-child-a',label:'合成孩子甲'},status:'denied',as_of:'2026-09-29T01:00:00Z',records:[]},
  {...residence,subject:{child_ref:'private-child-b',label:'合成孩子乙'},status:'no_current_records',as_of:'2026-10-01T01:00:00Z',records:[]},
 ]);
 expect(host.querySelector('.system-notices')?.textContent).toContain('有住宿查询未获准');
 const cards=host.querySelectorAll('[aria-label="住宿安排依据"]');
 expect(cards[0]?.textContent).toContain('合成孩子甲');expect(cards[0]?.textContent).toContain('2026/9/29');
 expect(cards[0]?.textContent).toContain('未获准');expect(cards[0]?.textContent).not.toContain('2026/10/1');
 expect(cards[1]?.textContent).toContain('合成孩子乙');expect(cards[1]?.textContent).toContain('没有当前住宿登记');
 expect(cards[1]?.textContent).not.toContain('未获准');
});
it('keeps a sole denied residence query explicit without borrowing a success from another fact family',async()=>{
 const host=await render([{kind:'family_residence',status:'denied',subject:{child_ref:'private-child-a',label:'合成孩子甲'}},
  {kind:'family_teaching',status:'available',items:[],coverage:'unknown'}]);
 const top=host.querySelector('.system-notices')?.textContent??'';
 expect(top).toContain('合成孩子甲');expect(top).toContain('当前身份未获准读取所问住宿资料');
 expect(top).not.toContain('有住宿查询未获准，可展开依据查看对应资料。');
});
it('does not hide quality risks from either residence query when summarizing mixed access outcomes',async()=>{
 const host=await render([
  {...residence,subject:{child_ref:'private-child-a',label:'合成孩子甲'},status:'denied',records:[],quality:[{dispute_status:'open'}]},
  {...residence,subject:{child_ref:'private-child-b',label:'合成孩子乙'},quality:[{source_status:'unavailable',review_due_at:'2026-09-01T00:00:00Z'}]},
 ]);
 const top=host.querySelector('.system-notices')?.textContent??'';
 for(const text of ['有住宿查询未获准','合成孩子甲','合成孩子乙','争议','来源暂时无法核实','需要再次核对'])expect(top).toContain(text);
 const cards=host.querySelectorAll('[aria-label="住宿安排依据"]');
 expect(cards[0]?.textContent).toContain('争议');expect(cards[0]?.textContent).not.toContain('核实期限');
 expect(cards[1]?.textContent).toContain('核实期限');expect(cards[1]?.textContent).not.toContain('争议');
});
it('keeps each child label on its own evidence and quality warning without rendering the child reference',async()=>{
 const host=await render([
  {...residence,subject:{child_ref:'private-child-a',label:'合成孩子甲'},quality:[{dispute_status:'open'}]},
  {...residence,subject:{child_ref:'private-child-b',label:'合成孩子乙'},status:'no_current_records',records:[]},
 ]);
 const cards=host.querySelectorAll('[aria-label="住宿安排依据"]');
 expect(cards[0]?.textContent).toContain('合成孩子甲');expect(cards[1]?.textContent).toContain('合成孩子乙');
 const alerts=[...host.querySelectorAll('[role="alert"]')].map(el=>el.textContent).join('');
 expect(alerts).toContain('合成孩子甲');expect(alerts).not.toContain('合成孩子乙');
 expect(host.textContent).not.toContain('private-child-');
});
it.each([{label:{secret:'BAD_SUBJECT'}},{child_ref:'private',label:'ok',scope:'BAD_SUBJECT'},{child_ref:'private',label:'',other:'BAD_SUBJECT'}])('rejects invalid child subject metadata instead of guessing attribution %#',async(subject)=>{
 const host=await render([{...residence,subject}]);
 expect(host.querySelector('[aria-label="住宿安排依据"]')).toBeNull();
 expect(host.querySelector('.unavailable-card')).not.toBeNull();expect(host.textContent).not.toContain('BAD_SUBJECT');
});

it.each([true,false])('shows residence workbench only when feature is explicitly %s',async(enabled)=>{
 const {default:App}=await import('../src/app/SchoolFactsApp.vue');
 const fetcher=vi.spyOn(globalThis,'fetch').mockImplementation(async input=>{
  const url=String(input);
  if(url.endsWith('/session'))return new Response(JSON.stringify({status:'signed_in',accounts:[],contexts:[{ref:'staff:one',label:'当前任职'}],selected_context_ref:'staff:one',selected_context_kind:'staff_assignment',can_maintain:false}));
  if(url.endsWith('/features'))return new Response(JSON.stringify({f6_features_enabled:enabled}));
  if(url.endsWith('/catalog'))return new Response(JSON.stringify({families:[]}));
  return new Response('{}',{status:404});
 });
 const host=document.createElement('div');document.body.append(host);const app=createApp(App);app.mount(host);
 try{for(let n=0;n<30;n++)await new Promise(resolve=>setTimeout(resolve,0));expect(host.textContent?.includes('住宿资料')).toBe(enabled);expect(fetcher.mock.calls.some(call=>String(call[0]).endsWith('/residence-facts/catalog'))).toBe(enabled);}finally{app.unmount();fetcher.mockRestore();}
});


it('retains the selected residence owner for an SSR query and prepare',async()=>{
 const {createDirectoryApi}=await import('../src/features/school_facts/directory_api');
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('{}',{status:403}));
 try {
  const api=createDirectoryApi('residence');
  await expect(api.describe('student_school_relationship')).rejects.toThrow();
  await expect(api.query({fact_kind:'student_school_relationship',scope:'selected',reference:'opaque-ssr'})).rejects.toThrow();
  await expect(api.prepare({fact_kind:'student_school_relationship',target_ref:'opaque-ssr',operation:'upsert',fields:{residence_mode:'boarding'},reason:'核对',effective_at:'2026-09-22T00:00:00Z'})).rejects.toThrow();
  expect(fetcher.mock.calls.every(call=>String(call[0]).startsWith('/api/v1/school-fact-product/residence-facts/'))).toBe(true);
 }finally{fetcher.mockRestore();}
});
const safePreflight='学生在同一期间有重复住宿安排，请在同一修改方案中处理。未生成可确认方案。';
it('preserves only the reviewed preflight reason and code for maintenance errors',async()=>{
 const {createDirectoryApi}=await import('../src/features/school_facts/directory_api');
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(JSON.stringify({code:'school_facts.preflight_rejected',message:safePreflight,request_id:'safe-request'}),{status:409}));
 try {await expect(createDirectoryApi('residence').catalog()).rejects.toMatchObject({status:409,code:'school_facts.preflight_rejected',message:safePreflight});}finally{fetcher.mockRestore();}
});
it.each([
 {code:'unknown',message:safePreflight},
 {code:'school_facts.preflight_rejected',message:'SQL private credentials'},
 {code:'school_facts.preflight_rejected',message:safePreflight+' SQL private credentials'},
 {detail:{code:'school_facts.preflight_rejected',message:'private raw exception'}},
])('does not expose arbitrary backend diagnostic bodies %#',async body=>{
 const {createDirectoryApi}=await import('../src/features/school_facts/directory_api');
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(JSON.stringify(body),{status:409}));
 try {await expect(createDirectoryApi('residence').catalog()).rejects.toMatchObject({message:'学校事实服务暂时不可用，请稍后重试。'});}finally{fetcher.mockRestore();}
});

it('keeps a residence report receipt inside the current child session without echoing extra details',async()=>{
 const {default:Form}=await import('../src/features/school_facts/FamilyCorrectionForm.vue');
 const invalid=ref(false);const host=document.createElement('div');document.body.append(host);
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(JSON.stringify({kind:'family_fact_correction_receipt',report_id:'owned-residence-report',status:'submitted',field_name:'bed_source_key',fact_kind:'student_bed_residence',submitted_at:'2026-09-22T00:00:00Z',student_label:'OTHER_CHILD_SECRET',roommates:['PRIVATE_OTHER_STUDENT']})));
 const app=createApp({render:()=>h(Form,{identityLabel:'当前孩子',identityInvalid:invalid.value,options:[{field_name:'bed_source_key',reference:'sf6_fam_current_child',label:'当前孩子床位'}]})});app.mount(host);
 try{
  const select=host.querySelector('select')!;select.value='sf6_fam_current_child';select.dispatchEvent(new Event('change'));
  const text=host.querySelector('textarea')!;text.value='请核对床位';text.dispatchEvent(new Event('input'));await nextTick();
  host.querySelector<HTMLButtonElement>('[data-action="review-correction"]')!.click();await nextTick();
  const ack=host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();
  host.querySelector<HTMLButtonElement>('[data-action="confirm-correction"]')!.click();
  for(let n=0;n<5;n++)await new Promise(resolve=>setTimeout(resolve,0));
  expect(host.textContent).toContain('学校已收到');expect(host.textContent).not.toContain('SECRET');expect(host.textContent).not.toContain('PRIVATE_OTHER_STUDENT');
  expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body)).reference).toBe('sf6_fam_current_child');
  invalid.value=true;await nextTick();expect(host.querySelector('[aria-label="报错回执"]')).toBeNull();
 }finally{app.unmount();fetcher.mockRestore();}
});

it('offers a residence record batch without exposing education purpose menus',async()=>{
 const {default:Workbench}=await import('../src/features/school_facts/DirectoryWorkbench.vue');
 const host=document.createElement('div');document.body.append(host);
 const api={catalog:vi.fn().mockResolvedValue({families:[]}),describe:vi.fn(),query:vi.fn(),original:vi.fn(),prepare:vi.fn(),proposal:vi.fn(),confirm:vi.fn(),receipt:vi.fn()};
 const app=createApp(Workbench,{identityLabel:'住宿维护者',batch:true,api});app.mount(host);
 try{await nextTick();expect(host.querySelector('[aria-label="同次学校变更清单"]')).not.toBeNull();expect(host.textContent).toContain('将当前修改加入清单');expect(host.textContent).not.toContain('本次变更目的');expect(host.textContent).not.toContain('正式毕业');expect(host.querySelector('[aria-label="住宿名单导入方式"]')).toBeNull();}finally{app.unmount();}
});
it('requires opt-in, explicit scope and time before preparing an empty residence replacement',async()=>{
 const {createDirectoryController}=await import('../src/features/school_facts/directory_session');
 const kind='student_bed_residence';const row={reference:'sf6-record-one',fact_kind:kind,label:'当前住宿',fields:{},version:1,valid_from:'2026-09-01T00:00:00Z',valid_to:null,source:{label:'正式',reference:null,published_at:null}};
 const proposal={proposal_ref:'proposal-f6-replace',summary:{reference:row.reference,fact_kind:kind,label:'当前住宿',operation:'end',before:{},after:null,effective_at:'2026-09-29T00:00:00.000Z',reason:'核对完整集合',source_label:'正式'},expires_at:'2099-01-01T00:00:00Z'};
 const api={catalog:vi.fn().mockResolvedValue({families:[{value:kind,label:'住宿'}]}),describe:vi.fn().mockResolvedValue({fact_kind:kind,label:'住宿',fields:[],creation_choices:[]}),query:vi.fn().mockResolvedValue({kind:'directory_facts',fact_kind:kind,records:[row],truncated:false}),prepare:vi.fn().mockResolvedValue(proposal),proposal:vi.fn(),confirm:vi.fn(),receipt:vi.fn(),original:vi.fn()};
 const c=createDirectoryController(api,{residenceImports:true});await c.open();await c.chooseFamily(kind);await c.query({fact_kind:kind,scope:'all_authorized'});c.edit(row.reference);c.setReason('核对完整集合');
 expect(await c.prepareResidenceImport('replace',[row.reference],false)).toBe(false);expect(api.prepare).not.toHaveBeenCalled();
 c.setEffectiveAt('2026-09-29T00:00:00Z');expect(await c.prepareResidenceImport('replace',[],false)).toBe(false);expect(await c.prepareResidenceImport('replace',['unread-reference'],false)).toBe(false);
 expect(await c.prepareResidenceImport('replace',[row.reference],false)).toBe(true);
 expect(api.prepare.mock.calls[0][0]).toMatchObject({import_mode:'replace',import_rows:[],import_replacement_refs:[row.reference],effective_at:'2026-09-29T00:00:00.000Z'});expect(api.prepare.mock.calls[0][0]).not.toHaveProperty('additional_changes');expect(api.confirm).not.toHaveBeenCalled();
 api.prepare.mockResolvedValue({...proposal,summary:{...proposal.summary,operation:'upsert',after:{}}});
 expect(await c.prepareResidenceImport()).toBe(true);const incremental=api.prepare.mock.calls[1][0];expect(incremental.import_mode).toBe('incremental');expect(incremental.import_rows).toHaveLength(1);expect(incremental).not.toHaveProperty('import_replacement_refs');
 c.invalidateIdentity();expect(await c.prepareResidenceImport('replace',[row.reference],false)).toBe(false);expect(api.prepare).toHaveBeenCalledTimes(2);
 const legacy=createDirectoryController(api);expect(await legacy.prepareResidenceImport('replace',[row.reference],false)).toBe(false);c.dispose();legacy.dispose();
});
it('shows residence replacement only on the explicit owner',async()=>{
 const {default:Workbench}=await import('../src/features/school_facts/DirectoryWorkbench.vue');
 const api={catalog:vi.fn().mockResolvedValue({families:[]}),describe:vi.fn(),query:vi.fn(),original:vi.fn(),prepare:vi.fn(),proposal:vi.fn(),confirm:vi.fn(),receipt:vi.fn()};
 const host=document.createElement('div');document.body.append(host);const app=createApp(Workbench,{identityLabel:'住宿维护者',batch:true,residenceImports:true,api});app.mount(host);
 try{await nextTick();expect(host.querySelector('[aria-label="住宿名单导入方式"]')).not.toBeNull();expect(host.textContent).toContain('不代表整房或全校');}finally{app.unmount();}
});
it.each([
 ['dorm_bed',['upsert','end','correct','cancel'],['revoke']],
 ['student_school_relationship',['upsert','end','correct','revoke'],['cancel']],
 ['student_bed_residence',['end','revoke','cancel'],['correct']],
])('exposes only supported residence maintenance operations for %s',async(kind,present,absent)=>{
 const {default:Workbench}=await import('../src/features/school_facts/DirectoryWorkbench.vue');const stamp='2026-09-01T00:00:00Z';
 const row={reference:'sf6-object-reference',fact_kind:kind,label:'合成对象',fields:{},version:1,valid_from:stamp,valid_to:null,source:{label:'正式',reference:null,published_at:stamp}};
 const api={catalog:vi.fn().mockResolvedValue({families:[{value:kind,label:'对象'}]}),describe:vi.fn().mockResolvedValue({fact_kind:kind,label:'对象',fields:[],creation_choices:[]}),query:vi.fn().mockResolvedValue({kind:'directory_facts',status:'known',fact_kind:kind,records:[row],matched_count:1,returned_count:1,truncated:false,next_cursor:null,queried_at:stamp,factual_at:stamp,coverage:{status:'unknown',description:'获准范围'}}),original:vi.fn(),prepare:vi.fn(),proposal:vi.fn(),confirm:vi.fn(),receipt:vi.fn()};
 const host=document.createElement('div');document.body.append(host);const app=createApp(Workbench,{identityLabel:'维护者',residenceImports:true,batch:true,api});app.mount(host);
 try{for(let i=0;i<12;i++)await nextTick();host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));for(let i=0;i<12;i++)await nextTick();host.querySelector<HTMLButtonElement>('[data-action="edit-directory"]')!.click();await nextTick();
 const select=host.querySelector<HTMLSelectElement>('form.maintenance select')!;const values=[...select.options].filter(o=>!o.disabled).map(o=>o.value);for(const v of present)expect(values).toContain(v);for(const v of absent)expect(values).not.toContain(v);
 if(present.includes('correct')){select.value='correct';select.dispatchEvent(new Event('change'));await nextTick();expect(host.textContent).toContain('补正生效时点');}
 else expect(host.textContent).toContain('不能直接改写学生和床位');
 }finally{app.unmount();}
});
it.each(['Asia/Tokyo','America/New_York'])('uses the authoritative school timezone %s for residence and publication timestamps',async zone=>{
 const host=await render([{...residence,fact_timezone_name:zone}]);const text=host.querySelector('[aria-label="住宿安排依据"]')!.textContent!;
 for(const value of [residence.records[0].valid_from,residence.records[0].source.published_at])expect(text).toContain(new Date(value).toLocaleString('zh-CN',{timeZone:zone,hour12:false}));
 expect(text).toContain(zone);expect(text).not.toContain('北京时间');
});
it.each([undefined,null,'not/a-timezone'])('shows explicit UTC when the school timezone is unavailable: %s',async zone=>{
 const host=await render([{...residence,fact_timezone_name:zone}]);const text=host.querySelector('[aria-label="住宿安排依据"]')!.textContent!;
 expect(text).toContain(new Date(residence.records[0].valid_from).toLocaleString('zh-CN',{timeZone:'UTC',hour12:false}));expect(text).toContain('UTC');expect(text).toContain('学校时区待确认');expect(text).not.toContain('北京时间');
});

// A finite candidate result is an ordinary selection outcome, not an unknown failure.
it('keeps ambiguous residence candidates in evidence without an unknown system warning',async()=>{
 const host=await render([{...residence,kind:'residence_facts',status:'ambiguous',records:[{...residence.records[0],fact_kind:'dorm_room',display_name:'301室'}]}]);
 expect(host.querySelector('[role="alert"]')).toBeNull();
 const evidence=host.querySelector('[aria-label="住宿安排依据"]');
 expect(evidence?.textContent).toContain('请先选定');
 expect(evidence?.textContent).toContain('301室');
 expect(evidence?.textContent).not.toContain('状态尚不能确定');
});
it('preserves real quality warnings on ambiguous residence results and unknown state fallback',async()=>{
 const host=await render([{...residence,status:'ambiguous',limitations:['source_quality_unavailable']},{...residence,status:'future_unknown_state',records:[]}]);
 // The shared limitation can also mean an overdue review. Do not assert a
 // source outage without the separate source_status evidence.
 expect(Array.from(host.querySelectorAll('[role="alert"]')).map(n=>n.textContent).join(' ')).toContain('住宿资料还需要核对');
 expect(host.querySelector('[aria-label="住宿安排依据"]')?.textContent).toContain('来源目前不可用');
 expect(Array.from(host.querySelectorAll('[role="alert"]')).map(n=>n.textContent).join(' ')).toContain('状态尚不能确定');
});

it('labels family report fields by their fact owner without changing education or teaching',async()=>{
 const {correctionOptionLabel}=await import('../src/features/school_facts/family_corrections');
 const option={label:'所选资料',reference:'opaque-target'};
 expect(correctionOptionLabel({...option,fact_kind:'dormitory_building_profile',field_name:'location_source_key'})).toContain('所属位置');
 expect(correctionOptionLabel({...option,fact_kind:'student_bed_residence',field_name:'valid_to'})).toContain('安排结束时间');
 expect(correctionOptionLabel({...option,fact_kind:'class_location',field_name:'location_source_key'})).toContain('班级教室');
 expect(correctionOptionLabel({...option,fact_kind:'teaching_group_member',field_name:'valid_to'})).toContain('成员有效截止时间');
});

it('renders a serialized batch summary with explicit empty leaf impacts before residence review',async()=>{
 const {isDirectoryProposal}=await import('../src/features/school_facts/directory_contracts');
 const summary={label:'原住宿安排',reference:'opaque-old',fact_kind:'student_bed_residence',operation:'end',before:{bed_ref:'old-bed'},after:{valid_to:'2026-09-29T00:00:00Z'},effective_at:'2026-09-29T00:00:00Z',reason:'明确换床',source_label:'正式维护',before_segments:[],after_segments:[],impacts:[]};
 const impact={...summary,label:'新住宿安排',reference:'opaque-new',operation:'upsert',before:null,after:{bed_ref:'new-bed'},impacts:[]};
 const proposal={kind:'fact_proposal',family_owner:'school_facts.residence',proposal_ref:'proposal.residence',expires_at:'2099-01-01T00:00:00Z',summary:{...summary,impacts:[impact]}};
 expect(isDirectoryProposal(proposal)).toBe(true);
 expect(isDirectoryProposal({...proposal,summary})).toBe(true);
 expect(isDirectoryProposal({...proposal,summary:{...summary,impacts:[{...impact,impacts:[impact]}]}})).toBe(false);
 expect(isDirectoryProposal({...proposal,summary:{...summary,impacts:[{...impact,after_segments:[{effective_at:'bad',valid_to:null,fields:{}}]}]}})).toBe(false);
 const {default:FullPreview}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const full=document.createElement('div');document.body.append(full);const fullApp=createApp(FullPreview,{proposal,complete:true});fullApp.mount(full);cleanup.push(()=>fullApp.unmount());await nextTick();
 expect(full.textContent).toContain('新住宿安排');expect(full.textContent).toContain('old-bed');expect(full.textContent).toContain('new-bed');
 expect(full.querySelector('[aria-label="本次修改总数"]')?.textContent).toBe('本次全部修改（2项）');
 expect(full.querySelector('[aria-label="全部联动修改"] h4')?.textContent).toBe('关联修改明细（1项）');
 const review=vi.fn();const host=document.createElement('div');document.body.append(host);const app=createApp(ProductAnswer,{payload:{facts:[proposal]},onReviewResidence:review});app.mount(host);cleanup.push(()=>app.unmount());await nextTick();
 expect(host.textContent).not.toContain('这条资料暂时无法展示');expect(proposal.summary.impacts[0].label).toBe('新住宿安排');
 const button=Array.from(host.querySelectorAll('button')).find(item=>item.textContent?.includes('核对完整修改'));
 expect(button).toBeDefined();button!.click();await nextTick();expect(review).toHaveBeenCalledWith('proposal.residence');
});

it('preserves writer option owners through the actual family panel and correction form',async()=>{
 const {default:Panel}=await import('../src/features/school_facts/FamilyProfilePanel.vue');
 const items=[
  {fact_kind:'student_bed_residence',field_name:'valid_to',label:'孩子住宿安排',reference:'opaque-residence'},
  {fact_kind:'dormitory_building_profile',field_name:'location_source_key',label:'孩子宿舍楼或住宿区域',reference:'opaque-building'},
 ];
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(JSON.stringify({items})));
 const host=document.createElement('div');document.body.append(host);const app=createApp(Panel,{identityLabel:'当前孩子'});app.mount(host);
 try{
  host.querySelector<HTMLButtonElement>('[data-action="prepare-family-correction"]')!.click();
  for(let i=0;i<5;i++)await new Promise(resolve=>setTimeout(resolve,0));
  const options=Array.from(host.querySelectorAll('[data-role="correction-field"] option')).map(option=>option.textContent);
  expect(options).toContain('孩子住宿安排 · 安排结束时间');
  expect(options).toContain('孩子宿舍楼或住宿区域 · 所属位置');
  expect(options.join(' ')).not.toContain('成员有效截止时间');expect(options.join(' ')).not.toContain('班级教室');
  expect(fetcher).toHaveBeenCalledTimes(1);expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v1/school-fact-product/family/corrections/options');
 }finally{app.unmount();fetcher.mockRestore();}
});
