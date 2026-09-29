import {createApp,nextTick} from 'vue';
import {afterEach,expect,it,vi} from 'vitest';
import FamilyCorrectionRecords from '../src/features/school_facts/FamilyCorrectionRecords.vue';
import {correctionReceiptMatches} from '../src/features/school_facts/family_corrections';
let app:ReturnType<typeof createApp>|undefined;
afterEach(()=>{app?.unmount();document.body.replaceChildren();vi.unstubAllGlobals();});
const at='2026-09-14T00:00:00Z';
it('matches current school-scoped teaching receipts without accepting another field or target',()=>{
 const reference='schoolv1:f5:teaching_assignment:o:YQ:'+'a'.repeat(64);
 const target={reference,fact_kind:'teaching_assignment',operation:'correct'};
 expect(correctionReceiptMatches(target,reference,'assignment_source_key','maintain')).toBe(true);
 expect(correctionReceiptMatches(target,reference,'class_source_key','maintain')).toBe(false);
 expect(correctionReceiptMatches({...target,reference:reference+'x'},reference,'assignment_source_key','maintain')).toBe(false);
});
async function scenario(target='f4:student_class_membership:current',field='class_source_key',operation='correct'){
 const report={kind:'family_fact_correction_receipt',report_id:'report-owned',reference:'report-owned',status:'submitted',submitted_at:at,student_label:'当前孩子',field_label:'所在班级',field_name:field,statement:'核对班级',required_action:'maintain',target_reference:target};
 const result={kind:'own_fact_receipt',reference:'sf2receipt_'+'a'.repeat(64),status:'succeeded',confirmed_at:at,targets:[{fact_kind:target.split(':')[1],reference:target,operation,label:'正式资料'}]};
 const wrong={...result,reference:'sf2receipt_'+'b'.repeat(64),targets:[{...result.targets[0],reference:'f4:student_class_membership:another'}]};
 const request=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({items:[report]}))).mockResolvedValueOnce(new Response(JSON.stringify({kind:'own_fact_receipts',items:[result,wrong]})));vi.stubGlobal('fetch',request);
 const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionRecords,{staff:true,identityLabel:'维护人员',targetReference:target,fieldName:field,requiredAction:'maintain'});app.mount(host);
 (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(host.querySelector('article button')).not.toBeNull());
 (host.querySelector('article button') as HTMLButtonElement).click();await vi.waitFor(()=>expect(request).toHaveBeenCalledTimes(2));await new Promise(r=>setTimeout(r,0));await nextTick();
 return {host,request,result};
}
it.each(['upsert','correct','cancel','end'])('accepts supported F4 %s receipt only for the exact target',async operation=>{
 const {host,result}=await scenario(undefined,undefined,operation);
 expect(Array.from(host.querySelectorAll('select option')).map(o=>(o as HTMLOptionElement).value)).toEqual(['',result.reference]);
});
it('does not extend F3 maintenance to correction operations',async()=>{
 const {host}=await scenario('f3:person_region:current','region','correct');
 expect(host.querySelectorAll('select option')).toHaveLength(1);
});
it('does not treat same-target receipt as proof that the reported field changed',async()=>{
 const {host,request,result}=await scenario();
 const select=host.querySelector('select')!;select.value=result.reference;select.dispatchEvent(new Event('change'));await nextTick();
 const ack=host.querySelector('input')!;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();
 request.mockResolvedValueOnce(new Response(JSON.stringify({detail:'Formal receipt did not change the reported field.'}),{status:403}));
 (host.querySelector('[data-action="resolve-correction"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(request).toHaveBeenCalledTimes(3));await new Promise(r=>setTimeout(r,0));await nextTick();
 expect(host.textContent).not.toContain('已登记正式处理结果，请重新查询当前资料。');
 expect(host.textContent).toContain('尚未登记处理结果');
 expect(JSON.parse(request.mock.calls[2]![1].body)).toEqual({writer_receipt_ref:result.reference});
});
it('does not admit an F4 receipt for a different field contract or unsupported operation',async()=>{
 const {host}=await scenario('f4:student_class_membership:current','grade_source_key','correct');
 expect(host.querySelectorAll('select option')).toHaveLength(1);
});

it.each(['submitted','resolved'])('shows supported education field names in parent %s records',async status=>{
 const fieldNames=['class_source_key','grade_source_key','assignment_source_key','location_source_key','position_source_key','cohort_source_key'];
 const items=fieldNames.map((field_name,i)=>({kind:'family_fact_correction_receipt',report_id:'own-report-'+i,status,field_name,submitted_at:at,...(status==='resolved'?{resolved_at:at}:{})}));
 const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({items})));vi.stubGlobal('fetch',request);
 const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionRecords,{identityLabel:'当前孩子'});app.mount(host);
 (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(host.querySelectorAll('article')).toHaveLength(6));
 for(const label of ['孩子所在班级','孩子年级安排','班主任任职','班级教室','孩子班内职务','孩子届别归属'])expect(host.textContent).toContain(label);
 for(const code of fieldNames)expect(host.textContent).not.toContain(code);
 expect(host.textContent).toContain(status==='resolved'?'已登记正式处理结果':'尚未登记处理结果');
 expect(request.mock.calls[0]![0]).toBe('/api/v1/school-fact-product/family/corrections?limit=20');
});
