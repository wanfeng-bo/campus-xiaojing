import { createApp, nextTick } from 'vue';
import { afterEach, expect, it } from 'vitest';
import ProductAnswer from '../src/features/school_facts/product_answer.vue';

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).forEach(f => f()); document.body.replaceChildren(); });
async function render(fact: unknown) {
  const host = document.createElement('div'); document.body.append(host);
  const app = createApp(ProductAnswer, {payload:{text:'数学由陈老师任教。',facts:Array.isArray(fact)?fact:[fact]}});
  app.mount(host); cleanups.push(() => app.unmount()); await nextTick(); return host;
}
const teaching = {kind:'family_teaching',status:'available',coverage:'unknown',as_of:'2026-09-14T00:00:00Z',items:[{course:'基础数学',teachers:[{name:'陈老师',role:'主讲'}],subjects:['数学'],offering_status:'持续开设'}],sources:[{fact:'teaching_assignment',source:{summary:'学校正式教学安排',publication_reference:'publication:internal'}}]};
it('distinguishes child teaching from the independent school leadership scope',async()=>{
 const host=await render([
  {...teaching,subject:{child_ref:'private-child-a',label:'合成孩子甲'}},
  {...teaching,kind:'family_school_leadership',subject:{scope:'本校获准学校目录'},leaders:[{display_name:'林老师',position_title:'校长'}]},
 ]);
 expect(host.querySelector('[aria-label="教学安排依据"]')?.textContent).toContain('合成孩子甲');
 expect(host.querySelector('[aria-label="学校领导依据"]')?.textContent).toContain('本校获准学校目录');
 expect(host.querySelector('[aria-label="学校领导依据"]')?.textContent).not.toContain('合成孩子甲');
 expect(host.textContent).not.toContain('private-child-a');
});
it('keeps later period risks visible regardless of card order and inside their own period evidence',async()=>{
 const period={...teaching,timeline:[
  {from:'2026-09-01T00:00:00Z',until:'2026-10-01T00:00:00Z',status:'available',items:teaching.items},
  {from:'2026-10-01T00:00:00Z',until:'2026-11-01T00:00:00Z',status:'available',
   participation:'休学期间保留关联不能证明实际参加课程。',
   items:[{...teaching.items[0],notices:['课程适用年级不一致。']}],quality:[{dispute_status:'open'}]},
 ]};
 for(const facts of [[period,teaching],[teaching,period]]){
  const host=await render(facts);
  const alerts=[...host.querySelectorAll('[role="alert"]')].filter(n=>!n.closest('details')).map(n=>n.textContent).join(' ');
  for(const text of ['第2时间段','休学期间','适用年级不一致','尚未解决的争议'])expect(alerts).toContain(text);
  const card=[...host.querySelectorAll('[aria-label="教学安排依据"]')].find(n=>n.textContent?.includes('休学期间'));
  expect(card).toBeTruthy();
  expect(card?.closest('details')?.open).toBe(false);
 }
});
it('retains only authorized candidate distinctions in ambiguous teaching evidence',async()=>{
 const host=await render({family_owner:'school_facts.teaching',status:'ambiguous',items:[],candidates:[
  {reference:'private-a',name:'同名班',grade_name:'初二',campus_name:'东校区',class_number:2},
  {reference:'private-b',name:'同名班'},
 ]});
 const card=host.querySelector('[aria-label="教学安排依据"]')!;
 const candidates=card.querySelectorAll('[data-role="query-candidate"]');
 expect(candidates).toHaveLength(2);
 expect(candidates[0]?.textContent).toContain('同名班 · 初二 · 东校区 · 2班');
 expect(candidates[1]?.textContent?.trim()).toBe('同名班');
 expect(card.textContent).not.toContain('private-');
 expect(host.querySelector('[role="alert"]')).toBeNull();
 expect(card.closest('details')?.open).toBe(false);
});
it('uses formal school timezone and excludes the query window end without ending facts',async()=>{
 const host=await render({...teaching,fact_timezone_name:'Asia/Shanghai',query_window:{start:'2027-02-14T16:00:00Z',end_exclusive:'2027-07-14T16:00:00Z'},timeline:[{from:'2027-02-14T16:00:00Z',until:'2027-07-14T16:00:00Z',items:teaching.items}]});
 const content=host.querySelector('[aria-label="教学安排依据"]')!.textContent!;
 expect(content).toContain('2027/2/15 00:00:00');
 expect(content).toContain('2027/7/15 00:00:00');
 expect(content).toContain('Asia/Shanghai');
 expect(content).toContain('不含终点');
 expect(content).toContain('不代表任教或开课结束');
});
it.each([undefined,'Invalid/Zone'])('labels UTC explicitly when school timezone is unavailable: %s',async(zone)=>{
 const host=await render({...teaching,fact_timezone_name:zone});
 expect(host.querySelector('[aria-label="教学安排依据"]')!.textContent).toContain('UTC；学校时区未明确');
});
it('keeps teaching evidence folded and preserves plain role ownership without raw ids',async()=>{
 const host=await render(teaching); const card=host.querySelector('[aria-label="教学安排依据"]')!;
 expect(card).not.toBeNull(); expect(card.closest('details')?.open).toBe(false);
 expect(card.textContent).toContain('基础数学'); expect(card.textContent).toContain('陈老师');
 expect(card.textContent).not.toContain('teaching_assignment');
 expect(host.querySelector('.unavailable-card')).toBeNull();
 expect(card.textContent).toContain('学校正式教学安排');
});
it('keeps period gaps and exact handover visible within evidence',async()=>{
 const host=await render({...teaching,timeline:[{from:'2026-09-01T00:00:00Z',until:'2026-09-10T00:00:00Z',items:teaching.items},{from:'2026-09-10T00:00:00Z',until:'2027-01-01T00:00:00Z',items:[{...teaching.items[0],teachers:[],teacher_status:'no_current_records'}]}]});
 expect(host.querySelector('[aria-label="教学安排依据"]')?.textContent).toContain('尚无有效任课记录');
});
it.each([['denied','无权'],['conflict','不一致'],['unavailable','暂时无法']])('keeps %s visible outside evidence',async(status,label)=>{
 const host=await render({...teaching,status});
 expect([...host.querySelectorAll('.important-notice')].filter(n=>!n.closest('details')).map(n=>n.textContent).join(' ')).toContain(label);
});
it('shows school leaders in their own folded directory with organization channels',async()=>{
 const host=await render({kind:'family_school_leadership',status:'available',leaders:[{display_name:'林老师',position_title:'校长',organization:'学校办公室',official_contacts:[{owner_name:'学校办公室',contact_kind:'email',contact_value:'office@example.invalid'}]}],quality:[],coverage:'unknown'});
 const card=host.querySelector('[aria-label="学校领导依据"]')!;
 expect(card.closest('details')?.open).toBe(false); expect(card.textContent).toContain('校长');
 expect(card.textContent).toContain('学校办公室'); expect(card.textContent).toContain('office@example.invalid');
 expect(card.textContent).not.toContain('email');
});
it('keeps the actual research group and its formal scope together',async()=>{
 const host=await render({family_owner:'school_facts.teaching',status:'available',items:[{name:'跨年级数学备课组',membership:'成员',scopes:[{subject:'数学',grade:'初二',academic_year:2026,academic_grade:'正式年级组'}]}]});
 const card=host.querySelector('[aria-label="教学安排依据"]');
 expect(card?.textContent).toContain('跨年级数学备课组');
 expect(card?.textContent).toContain('数学 · 初二 · 正式年级组 · 2026学年');
});

it.each(['family_teaching','family_school_leadership'])('binds denied %s to its own query without denying successful results',async(kind)=>{
 const host=await render([teaching,{kind,status:'denied'}]);
 const notices=[...host.querySelectorAll('.important-notice')].filter(n=>!n.closest('details')).map(n=>n.textContent).join(' ');
 expect(notices).toContain('部分查询未获准');
 expect(notices).toContain(kind==='family_teaching'?'教学查询 2':'学校领导查询 2');
 expect(notices).not.toContain('当前身份无权查看所问资料');
 expect(notices).not.toContain('基础数学');
 expect(notices).not.toContain('2026');
 expect(host.textContent).toContain('数学由陈老师任教。');
 const cards=host.querySelectorAll('[aria-label="教学安排依据"], [aria-label="学校领导依据"]');
 expect(cards[1]?.textContent).toContain('该项查询未获准');
 expect(cards[1]?.textContent).toContain(kind==='family_teaching'?'教学查询 2':'学校领导查询 2');
});
it('keeps a sole denied query explicit without implying successful partial results',async()=>{
 const host=await render({kind:'family_teaching',status:'denied'});
 const notice=host.querySelector('.important-notice')!.textContent!;
 expect(notice).toContain('教学查询 1');
 expect(notice).toContain('无权');
  expect(notice).not.toContain('部分查询');
});

it.each(['ambiguous','no_current_records','not_recorded','selection_required'])(
 'keeps %s as an individual query outcome instead of a final instruction',async(status)=>{
  const host=await render([{family_owner:'school_facts.teaching',status,items:[],coverage:'unknown'},teaching]);
  const cards=host.querySelectorAll('[aria-label="教学安排依据"]');
  expect(cards).toHaveLength(2);
  expect(cards[0]?.closest('details')?.open).toBe(false);
  const outcome=cards[0]?.querySelector('[data-role="query-outcome"]');
  expect(outcome?.textContent).toContain('该次查询');
  expect(outcome?.textContent).not.toContain('请先区分');
  expect(cards[1]?.textContent).toContain('陈老师');
  expect(host.querySelector('.important-notice')).toBeNull();
  expect(host.querySelector('.answer-text')?.textContent).toBe('数学由陈老师任教。');
 });

it.each(['ambiguous','no_current_records'])(
 'does not turn a sole %s outcome into an automatic task prompt or erase it',async(status)=>{
  const host=await render({family_owner:'school_facts.teaching',status,items:[]});
  expect(host.querySelector('.important-notice')).toBeNull();
  expect(host.querySelector('[data-role="query-outcome"]')?.textContent).toContain('该次查询');
 });

it.each([['denied','无权'],['conflict','不一致'],['stale','已变化'],['source_unavailable','暂时无法核实']])(
 'never treats a later success as resolution of the earlier %s risk',async(status,label)=>{
  const risk={...teaching,status,participation:'休学期间保留的教学关联不能证明实际参加课程。',items:[{notices:['课程适用年级不一致，需要学校核对。']}],quality:[{dispute_status:'open'}]};
  for(const facts of [[risk,teaching],[teaching,risk]]){
   const host=await render(facts);
   const text=[...host.querySelectorAll('.important-notice')].filter(n=>!n.closest('details')).map(n=>n.textContent).join(' ');
   for(const required of [label,'休学期间','年级不一致','尚未解决的争议'])expect(text).toContain(required);
  }
 });

it('renders a bounded own report history as evidence and retains old receipt compatibility',async()=>{
 const host=await render({kind:'family_correction_history',status:'available',coverage:'limited_page',scope:'当前孩子本人有限页',meaning:'不代表全部历史',items:[{kind:'family_fact_correction_receipt',report_id:'report-own',fact_kind:'teaching_assignment',field_name:'assignment_source_key',status:'resolved',status_label:'已处理',status_meaning:'已绑定正式处理证明',target_label:'任课',field_label:'任课教师',submitted_at:'2026-09-14T00:00:00Z',resolved_at:'2026-09-14T01:00:00Z'}]});
 const card=host.querySelector('[aria-label="本人报错处理依据"]');
 expect(card?.textContent).toContain('任课教师');expect(card?.textContent).toContain('已处理');
 expect(card?.closest('details')?.open).toBe(false);expect(host.querySelector('.unavailable-card')).toBeNull();
});
