import {createApp,nextTick} from 'vue';
import {afterEach,expect,it,vi} from 'vitest';
import FamilyProfilePanel from '../src/features/school_facts/FamilyProfilePanel.vue';
import FamilyCorrectionForm from '../src/features/school_facts/FamilyCorrectionForm.vue';
import {isCorrectionOption,isCorrectionReceipt} from '../src/features/school_facts/family_corrections';
let app:ReturnType<typeof createApp>|undefined;
afterEach(()=>{app?.unmount();document.body.replaceChildren();vi.unstubAllGlobals();});
const opts=Array.from({length:11},(_,i)=>({field_name:i<7?'campus':'grade_source_key',label:i<7?'校区':'班级年级',reference:'opaque-option-'+i}));
it('accepts finite F4 correction fields and their receipt without admitting arbitrary fields',()=>{
 expect(isCorrectionOption(opts[10])).toBe(true);expect(isCorrectionOption({...opts[10],field_name:'private_address'})).toBe(false);
 expect(isCorrectionReceipt({kind:'family_fact_correction_receipt',report_id:'report1',status:'submitted',field_name:'grade_source_key',submitted_at:'2026-09-14T00:00:00Z'})).toBe(true);
});
it('opens the actual eleven-option shape and keeps same-field different targets',async()=>{
 const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify({items:opts}),{status:200}));vi.stubGlobal('fetch',fetcher);
 const host=document.createElement('div');document.body.append(host);app=createApp(FamilyProfilePanel,{identityLabel:'当前孩子'});app.mount(host);
 (host.querySelector('[data-action="prepare-family-correction"]') as HTMLButtonElement).click();
 await vi.waitFor(()=>expect(host.querySelectorAll('select option')).toHaveLength(12));
 expect(host.textContent).not.toContain('暂时无法核对报错范围');
});
it('selects the exact signed reference when two options share a field',async()=>{
 const host=document.createElement('div');document.body.append(host);app=createApp(FamilyCorrectionForm,{identityLabel:'当前孩子',options:opts.slice(9)});app.mount(host);
 const select=host.querySelector('select')!;select.value=opts[10]!.reference;select.dispatchEvent(new Event('change'));
 const area=host.querySelector('textarea')!;area.value='请核对';area.dispatchEvent(new Event('input'));await nextTick();
 host.querySelector('form')!.dispatchEvent(new Event('submit',{cancelable:true}));await nextTick();
 expect(host.querySelector('[aria-label="核对报错内容"]')).not.toBeNull();
 const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({kind:'family_fact_correction_receipt',report_id:'report1',status:'submitted',field_name:'grade_source_key',submitted_at:'2026-09-14T00:00:00Z'})));vi.stubGlobal('fetch',request);
 const ack=host.querySelector('input')!;ack.checked=true;ack.dispatchEvent(new Event('change'));await nextTick();
 (host.querySelector('[data-action="confirm-correction"]') as HTMLButtonElement).click();await vi.waitFor(()=>expect(request).toHaveBeenCalledOnce());
 expect(JSON.parse(request.mock.calls[0]![1].body).reference).toBe(opts[10]!.reference);
});
