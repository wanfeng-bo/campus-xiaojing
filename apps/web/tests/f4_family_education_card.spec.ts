import {createApp, nextTick} from 'vue';
import {afterEach, expect, it} from 'vitest';
import ProductAnswer from '../src/features/school_facts/product_answer.vue';

const card = {status:'available',profile:{teachers:[]},authorized_fields:['teachers'],as_of:'2026-09-13T16:00:00Z',coverage:'unknown',fact_timezone_name:'Asia/Shanghai',quality:{teachers:{status:'known_negative',coverage:'unknown',source_availability:'unknown',freshness:'unknown',statements:[{quality_kind:'negative',negative_code:'no_class_teacher',effective_at:'2026-09-01T00:00:00Z',valid_to:null,verified_at:'2026-09-01T00:00:00Z',version:1}]}},sources:[{fact:'fact_quality',field:'teachers',effective_at:'2026-09-01T00:00:00Z',valid_to:null,version:1,publication_reference:'publication:public-proof',source_ref:'正式班主任安排核查'}]};
const apps:Array<ReturnType<typeof createApp>>=[];
afterEach(()=>{apps.forEach(a=>a.unmount());apps.length=0;document.body.replaceChildren();});
async function render(value:unknown){const host=document.createElement('div');document.body.append(host);const app=createApp(ProductAnswer,{payload:{text:'学校已明确未安排班主任。',facts:[value]}});apps.push(app);app.mount(host);await nextTick();return host;}
it('renders actual family education fact-card shape with quality in folded evidence',async()=>{
 const host=await render(card);
 expect(host.querySelector('.unavailable-card')).toBeNull();
 const section=host.querySelector('[aria-label="孩子教育资料依据"]');expect(section).not.toBeNull();
 expect(section!.textContent).toContain('学校明确未安排班主任');
 expect(section!.closest('details')?.open).toBe(false);
 expect(section!.textContent).toContain('2026-09-13T16:00:00Z');
 expect(section!.textContent).toContain('Asia/Shanghai');
 expect(section!.textContent).not.toContain('known_negative');
});
it('shows the authorized child label on education evidence instead of its technical reference',async()=>{
 const host=await render({...card,subject:{child_ref:'private-child-a',label:'合成孩子甲'}});
 expect(host.querySelector('[aria-label="孩子教育资料依据"]')?.parentElement?.textContent).toContain('合成孩子甲');
 expect(host.textContent).not.toContain('private-child-a');
});
it('does not accept fields beyond the authorized child projection',async()=>{
 const host=await render({...card,profile:{teachers:[],members:['另一个孩子']}});
 expect(host.querySelector('.unavailable-card')).not.toBeNull();expect(host.textContent).not.toContain('另一个孩子');
});
it('keeps unknown distinct from formal negative and accepts older no-quality cards',async()=>{
 const {quality,...old}=card;const host=await render(old);
 expect(host.querySelector('.unavailable-card')).toBeNull();
 expect(host.querySelector('[aria-label="孩子教育资料依据"]')!.textContent).toContain('尚无可提供的记录');
});
