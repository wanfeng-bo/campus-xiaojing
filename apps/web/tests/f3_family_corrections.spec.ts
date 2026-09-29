import {createApp,h,nextTick,ref} from 'vue';
import {afterEach,expect,it,vi} from 'vitest';
import FamilyCorrectionForm from '../src/features/school_facts/FamilyCorrectionForm.vue';
import FamilyCorrectionRecords from '../src/features/school_facts/FamilyCorrectionRecords.vue';
import {isFamilyProfile} from '../src/features/school_facts/family_contracts';

let app:ReturnType<typeof createApp>|undefined;
afterEach(()=>{app?.unmount();app=undefined;document.body.replaceChildren();vi.unstubAllGlobals();});
const options=[{field_name:'campus',label:'当前校区',reference:'opaque-current-campus'}, {field_name:'relationship',label:'孩子关联',reference:'opaque-current-relationship'}];
const receipt={kind:'family_fact_correction_receipt',report_id:'report-owned',status:'submitted',field_name:'campus',submitted_at:'2026-09-13T04:00:00Z'};
async function flush(){await new Promise(r=>setTimeout(r,0));await nextTick();}
function mount(invalid=ref(false)){
  const host=document.createElement('div');document.body.append(host);
  app=createApp({render:()=>h(FamilyCorrectionForm,{identityLabel:'监护人 · 合成孩子甲',options,identityInvalid:invalid.value})});app.mount(host);return host;
}
async function prepare(host:HTMLElement){
  const select=host.querySelector('[data-role="correction-field"]') as HTMLSelectElement;select.value='opaque-current-campus';select.dispatchEvent(new Event('change'));
  const text=host.querySelector('textarea')!;text.value='页面里的校区资料与学校告知的不一致。';text.dispatchEvent(new Event('input'));await nextTick();
  (host.querySelector('[data-action="review-correction"]') as HTMLButtonElement).click();await nextTick();
}
it('previews current child and statement without submitting or claiming formal correction',async()=>{
  const request=vi.fn();vi.stubGlobal('fetch',request);const host=mount();await prepare(host);
  expect(request).not.toHaveBeenCalled();expect(host.querySelector('[aria-label="核对报错内容"]')?.textContent).toContain('监护人 · 合成孩子甲');
  expect(host.querySelector('[aria-label="核对报错内容"]')?.textContent).toContain('不会直接修改孩子资料');
  const button=host.querySelector('[data-action="confirm-correction"]') as HTMLButtonElement;button.click();
  expect(request).not.toHaveBeenCalled();
});
it('requires explicit confirmation and recovers an unknown submission with the original key',async()=>{
  const request=vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(new Response(JSON.stringify(receipt),{status:200}));vi.stubGlobal('fetch',request);
  const host=mount();await prepare(host);const ack=host.querySelector('input[type="checkbox"]') as HTMLInputElement;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();
  (host.querySelector('[data-action="confirm-correction"]') as HTMLButtonElement).click();await flush();
  expect(request).toHaveBeenCalledTimes(1);const sent=JSON.parse(request.mock.calls[0][1].body);
  expect(sent.reference).toBe(options[0].reference);expect(sent.field_name).toBe('campus');expect(host.textContent).toContain('是否收到');
  (host.querySelector('[data-action="lookup-correction"]') as HTMLButtonElement).click();await flush();
  expect(request.mock.calls[1][0]).toContain('/by-key/'+sent.idempotency_key);expect(request.mock.calls[1][1]?.method ?? 'GET').toBe('GET');
  expect(host.textContent).toContain('已收到');expect(host.textContent).toContain('尚未登记处理结果');expect(host.textContent).not.toContain('已修改');
});
it('discards late submitted receipts and clears private drafts after identity invalidation',async()=>{
  let resolve!:(v:Response)=>void;vi.stubGlobal('fetch',vi.fn(()=>new Promise(r=>resolve=r)));const invalid=ref(false);const host=mount(invalid);await prepare(host);
  const ack=host.querySelector('input[type="checkbox"]') as HTMLInputElement;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();
  (host.querySelector('[data-action="confirm-correction"]') as HTMLButtonElement).click();await nextTick();invalid.value=true;await nextTick();
  resolve(new Response(JSON.stringify(receipt),{status:200}));await flush();
  expect(host.textContent).not.toContain('尚未登记处理结果');expect((host.querySelector('textarea') as HTMLTextAreaElement)?.value??'').toBe('');
});

it('does not accept child data exposing fields outside the caregiver projection',()=>{
  const data={kind:'family_student_profile',status:'available',relationship_kind:'recognized_caregiver',query_scope:'选中孩子',profile:{name:'合成孩子甲'},sources:[],missing:[],authorized_fields:['name','campus','enrollment_status'],field_restrictions:{student_number:'denied'},as_of:receipt.submitted_at,queried_at:receipt.submitted_at,coverage:{status:'unknown',meaning:'尚未声明完整覆盖'}};
  expect(isFamilyProfile({...data,profile:{...data.profile,student_number:'不可见学号'}})).toBe(false);
  expect(isFamilyProfile(data)).toBe(true);
});
it('drops a late own receipt list after changing child',async()=>{
  let resolve!:(v:Response)=>void;const request=vi.fn((_input:RequestInfo|URL,_init?:RequestInit)=>new Promise(r=>resolve=r));vi.stubGlobal('fetch',request);
  const invalid=ref(false);const host=document.createElement('div');document.body.append(host);app=createApp({render:()=>h(FamilyCorrectionRecords,{identityLabel:'当前孩子',identityInvalid:invalid.value})});app.mount(host);
  (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await nextTick();
  expect(request.mock.calls[0][0]).toContain('/family/corrections?limit=20');invalid.value=true;await nextTick();resolve(new Response(JSON.stringify({items:[receipt]}),{status:200}));await flush();expect(host.textContent).not.toContain('尚未登记处理结果');
});
it('filters Staff intake before display and does not accept a different target',async()=>{
  const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({items:[{...receipt,reference:'report-owned',student_label:'另一个孩子',field_label:'校区',statement:'不应显示',required_action:'maintain',target_reference:'other-child-ref'}]}),{status:200}));vi.stubGlobal('fetch',request);
  const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionRecords,{staff:true,identityLabel:'资料维护者',targetReference:'selected-child-ref',fieldName:'campus',requiredAction:'maintain'});app.mount(host);
  (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await flush();const url=new URL(request.mock.calls[0][0],'http://localhost');
  expect(url.searchParams.get('target_reference')).toBe('selected-child-ref');expect(url.searchParams.get('required_action')).toBe('maintain');expect(host.textContent).not.toContain('另一个孩子');expect(host.textContent).toContain('暂时未能查询');
});

it('selects the existing matching F3 receipt from a full mixed F2/F3 history page',async()=>{
  const targetReference='f3:person_region:current';
  const report={...receipt,reference:'report-owned',student_label:'合成孩子甲',field_label:'地区',field_name:'region',statement:'请核对地区',required_action:'maintain',target_reference:targetReference};
  const items=Array.from({length:20},(_,index)=>({kind:'own_fact_receipt',reference:'sf2receipt_'+index.toString(16).padStart(64,'0'),status:'succeeded',confirmed_at:receipt.submitted_at,targets:[{fact_kind:index===0?'person_region':'official_contact',reference:index===0?targetReference:'f2:official_contact:other',operation:'upsert',label:index===0?'地区':'官方联系方式'}]}));
  const request=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({items:[report]}),{status:200})).mockResolvedValueOnce(new Response(JSON.stringify({kind:'own_fact_receipts',items,has_more:true}),{status:200}));vi.stubGlobal('fetch',request);
  const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionRecords,{staff:true,identityLabel:'资料维护者',targetReference,fieldName:'region',requiredAction:'maintain'});app.mount(host);
  (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await flush();
  (host.querySelector('article button') as HTMLButtonElement).click();await flush();
  expect(request.mock.calls[1][0]).toBe('/api/v1/school-fact-product/family-facts/history-receipts?limit=20');
  const select=host.querySelector('select') as HTMLSelectElement;
  expect(Array.from(select.options).map(option=>option.value)).toEqual(['',items[0].reference]);
  expect(host.textContent).not.toContain('暂时未能取得');
  expect((host.querySelector('[data-action="resolve-correction"]') as HTMLButtonElement).disabled).toBe(true);
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls.every(call=>!call[1]?.method||call[1].method==='GET')).toBe(true);
});
