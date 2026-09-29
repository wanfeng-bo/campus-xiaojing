import {createApp,nextTick} from 'vue';
import {afterEach,expect,it,vi} from 'vitest';
import FamilyCorrectionRecords from '../src/features/school_facts/FamilyCorrectionRecords.vue';
import {isCorrectionReceipt} from '../src/features/school_facts/family_corrections';
let app:ReturnType<typeof createApp>|undefined;
afterEach(()=>{app?.unmount();document.body.replaceChildren();vi.unstubAllGlobals();});
it('recognizes reviewed as distinct from formally corrected',()=>{
 expect(isCorrectionReceipt({kind:'family_fact_correction_receipt',report_id:'r',status:'reviewed',field_name:'teaching_role',submitted_at:'2026-09-14T00:00:00Z',resolved_at:'2026-09-14T01:00:00Z',review_explanation:'已核实原资料无误'})).toBe(true);
});
it('prepares, requires explicit click and only queries the original token after unknown result',async()=>{
 const report={kind:'family_fact_correction_receipt',report_id:'r',reference:'r',status:'submitted',field_name:'teaching_role',submitted_at:'2026-09-14T00:00:00Z',student_label:'当前对象',field_label:'任教职责',statement:'核对教师职责',required_action:'maintain',target_reference:'f4:teaching_assignment:ref'};
 const prep={kind:'family_correction_review_preparation',report_id:'r',field_name:'teaching_role',explanation:'已核对正式安排，原资料无误',target_version:2,expires_at:'2099-01-01T00:00:00Z',confirmation_token:'fr1.prepared.original'};
 const request=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({items:[report]}))).mockResolvedValueOnce(new Response(JSON.stringify(prep))).mockResolvedValueOnce(new Response('{}',{status:503})).mockResolvedValueOnce(new Response(JSON.stringify({...report,status:'reviewed',resolved_at:'2026-09-14T01:00:00Z',review_explanation:prep.explanation})));vi.stubGlobal('fetch',request);
 const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionRecords,{staff:true,identityLabel:'维护者',targetReference:report.target_reference,fieldName:'teaching_role',requiredAction:'maintain'});app.mount(host);
 (host.querySelector('[data-action="list-corrections"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(host.querySelector('[data-action="start-review"]')).not.toBeNull());
 (host.querySelector('[data-action="start-review"]') as HTMLButtonElement).click();await nextTick();
 const input=host.querySelector('[data-review-explanation]') as HTMLTextAreaElement;input.value=prep.explanation;input.dispatchEvent(new Event('input'));await nextTick();
 (host.querySelector('[data-action="prepare-review"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(request).toHaveBeenCalledTimes(2));await new Promise(r=>setTimeout(r,0));await nextTick();
 const confirm=host.querySelector('[data-action="confirm-review"]') as HTMLButtonElement;expect(confirm.disabled).toBe(true);
 const ack=host.querySelector('[data-review-ack]') as HTMLInputElement;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();confirm.click();
 await vi.waitFor(()=>expect(request).toHaveBeenCalledTimes(3));await new Promise(r=>setTimeout(r,0));await nextTick();
 expect(JSON.parse(request.mock.calls[2]![1].body)).toEqual({confirmation_token:prep.confirmation_token,confirmed:true});
 expect((host.querySelector('[data-action="confirm-review"]') as HTMLButtonElement).disabled).toBe(true);
 (host.querySelector('[data-action="review-result"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(request).toHaveBeenCalledTimes(4));await new Promise(r=>setTimeout(r,0));await nextTick();
 expect(request.mock.calls[3]![0]).toContain('/review-result');expect(host.textContent).toContain('已核实，原资料无误');expect(host.textContent).not.toContain('已登记正式处理结果，请重新查询当前资料');
});
