import {expect,it,vi} from 'vitest';
import {isDirectoryProposal} from '../src/features/school_facts/directory_contracts';
import {createDirectoryController} from '../src/features/school_facts/directory_session';
const stamp='2026-09-14T00:00:00Z';
const summary={label:'教学开课',reference:'offer:one',fact_kind:'teaching_offering',operation:'upsert',before:null,after:{teaching_role_limits:{lead:1,co_teacher:3}},effective_at:stamp,reason:'学校核实',source_label:'正式登记'};
it('accepts finite teaching roles but never widens F4 roles',()=>{
 expect(isDirectoryProposal({proposal_ref:'p',expires_at:stamp,summary})).toBe(true);
 expect(isDirectoryProposal({proposal_ref:'p',expires_at:stamp,summary:{...summary,after:{teacher_role_limits:{lead:1}}}})).toBe(false);
 expect(isDirectoryProposal({proposal_ref:'p',expires_at:stamp,summary:{...summary,after:{teaching_role_limits:{primary:1}}}})).toBe(false);
});
it('renders derived unchanged impacts with distinct notices',async()=>{
 const {createApp,nextTick}=await import('vue');const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const proposal={proposal_ref:'p',expires_at:stamp,summary:{...summary,regroup:{scope:'batch_only' as const,notice:'本次教学完整影响',periods:[],teaching_notices:['独立维护名单'],teaching_impacts:[{reference:'other',label:'共享开课乙',changed:false}]}}};
 const host=document.createElement('div');const app=createApp(Proposal,{proposal,complete:true});app.mount(host);await nextTick();
 expect(host.textContent).toContain('共享开课乙');expect(host.textContent).toContain('派生影响');expect(host.textContent).toContain('独立维护名单');app.unmount();
 expect(isDirectoryProposal({...proposal,summary:{...proposal.summary,regroup:{...proposal.summary.regroup,teaching_impacts:[{reference:'x',label:'x',changed:'false'}]}}})).toBe(false);
});
it('uses descriptor roles and stages import through prepare, never confirms it',async()=>{
 const row={reference:'offer:one',fact_kind:'teaching_offering',label:'开课',fields:{},version:1,valid_from:stamp,valid_to:null,source:{label:'学校',reference:null,published_at:stamp}};
 const api:any={catalog:vi.fn(async()=>({families:[{value:row.fact_kind,label:'开课'}]})),describe:vi.fn(async()=>({fact_kind:row.fact_kind,label:'开课',fields:[{name:'teaching_role_limits',label:'任教人数',type:'object',required:false,reference_kind:null,options:[{value:'lead',label:'主讲'},{value:'co_teacher',label:'共同任教'}]}],creation_choices:[]})),query:vi.fn(async()=>({fact_kind:row.fact_kind,records:[row]})),prepare:vi.fn(async()=>({proposal_ref:'p',expires_at:stamp,summary})),confirm:vi.fn()};
 const c=createDirectoryController(api);await c.open();await c.chooseFamily(row.fact_kind);await c.query({fact_kind:row.fact_kind,scope:'all_authorized'});c.edit(row.reference);c.setReason('学校核实');
 expect(c.setField('teaching_role_limits',{lead:1})).toBe(true);expect(c.setField('teaching_role_limits',{primary:1})).toBe(false);
 expect(await c.prepareImport([{reference:row.reference,offering_status:'suspended'}],'incremental',[])).toBe(true);
 expect(api.prepare.mock.calls[0][0].import_mode).toBe('incremental');expect(c.getState().reviewed).toBe(false);expect(api.confirm).not.toHaveBeenCalled();c.dispose();
});

it('generates editable teaching rows with stable identity and preserves complex fields',async()=>{
 const {teachingDraftRows,teachingColumn}=await import('../src/features/school_facts/directory_session');
 const descriptor:any={fact_kind:'teaching_assignment',fields:[{name:'assignment_ref',type:'reference'},{name:'teaching_role',type:'select'},{name:'subject_refs',type:'references'}]};
 const originals:any=[{reference:'signed:first',label:'同名老师',fact_kind:'teaching_assignment',fields:{assignment_ref:'signed:staff-a',teaching_role:'lead',subject_refs:['signed:math']}},{reference:'signed:second',label:'同名老师',fact_kind:'teaching_assignment',fields:{assignment_ref:'signed:staff-b',teaching_role:'co_teacher'}}];
 const draft=teachingDraftRows(descriptor,originals);
 expect(draft.map(r=>r.values.reference)).toEqual(['signed:first','signed:second']);
 expect(draft[0].values.assignment_source_key).toBe('signed:staff-a');
 expect(draft[0].values.subject_source_keys).toBeUndefined();
 draft[0].values.teaching_role='co_teacher';expect(originals[0].fields.teaching_role).toBe('lead');
 expect(teachingColumn('assignment_ref')).toBe('assignment_source_key');
 expect(()=>teachingDraftRows(descriptor,[{...originals[0],fact_kind:'person'}])).toThrow();
});

it('explicit end strips replacement fields while update retains stable references',async()=>{
 const {teachingImportRows}=await import('../src/features/school_facts/directory_session');
 expect(teachingImportRows([{label:'旧任课',values:{reference:'signed:one',operation:'end',assignment_source_key:'signed:staff'}}])).toEqual([{reference:'signed:one',operation:'end'}]);
});

it('requests choices through the declared education/family field, not a foreign fact query',async()=>{
 const {createDirectoryApi}=await import('../src/features/school_facts/directory_api');
 const fetcher=vi.fn(async()=>({ok:false,status:403}));vi.stubGlobal('fetch',fetcher);
 try {
  for(const family of ['education','family'] as const){
   const api=createDirectoryApi(family);
   await expect(api.choices!('teaching_assignment','assignment_ref')).rejects.toThrow();
   const [url,options]=fetcher.mock.calls.at(-1)! as unknown as [string,RequestInit];
   expect(url).toBe(`/api/v1/school-fact-product/${family}-facts/choices`);
   expect(JSON.parse(options.body as string)).toEqual({fact_kind:'teaching_assignment',field_name:'assignment_ref'});
  }
 } finally {vi.unstubAllGlobals();}
});

it('the reference button uses owner and field rather than foreign query kind',async()=>{
 const row={reference:'teaching:one',fact_kind:'teaching_assignment',label:'任课',fields:{},version:1,valid_from:stamp,valid_to:null,source:{label:'学校',reference:null,published_at:stamp}};
 const choices=vi.fn(async()=>({records:[{...row,reference:'teacher:chen',label:'陈老师',fact_kind:'assignment'}]}));
 const api:any={catalog:vi.fn(async()=>({families:[{value:row.fact_kind,label:'任课'}]})),describe:vi.fn(async()=>({fact_kind:row.fact_kind,label:'任课',fields:[{name:'assignment_ref',label:'教职工任职',type:'reference',required:true,reference_kind:'assignment',options:[]}],creation_choices:[]})),query:vi.fn(async()=>({fact_kind:row.fact_kind,records:[row]})),choices};
 const c=createDirectoryController(api);await c.open();await c.chooseFamily(row.fact_kind);await c.loadChoices('assignment_ref');
 expect(choices).toHaveBeenCalledWith('teaching_assignment','assignment_ref');expect(api.query).not.toHaveBeenCalled();expect(c.getState().choices.assignment_ref[0].label).toBe('陈老师');c.dispose();
});

it('preserves canonical selected teacher identity through form rows and clipboard columns',async()=>{
 const {teachingDraftRows,teachingImportRows,parseTeachingTable}=await import('../src/features/school_facts/directory_session');
 const descriptor:any={fact_kind:'teaching_assignment',fields:[{name:'assignment_ref',type:'reference'}]};
 const reference='f4:teaching_assignment:math-b';
 const chen='f3:assignment:canonical-chen';
 const drafts=teachingDraftRows(descriptor,[{reference,fact_kind:'teaching_assignment',label:'基础数学',fields:{assignment_ref:chen}} as any]);
 const expected=[{reference,operation:'upsert',assignment_source_key:chen}];
 expect(teachingImportRows(drafts)).toEqual(expected);
 expect(parseTeachingTable(`reference\toperation\tassignment_source_key\n${reference}\tupsert\t${chen}`,descriptor)).toEqual(expected);
});

it('renders verified relation names in full diff and timeline while retaining raw references in details',async()=>{
 const {createApp,nextTick}=await import('vue');const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const fields={assignment_ref:'signed:teacher',subject_refs:['signed:math']};
 const proposal={proposal_ref:'p',expires_at:stamp,summary:{...summary,fact_kind:'teaching_assignment',before:fields,after:fields,reference_labels:{'signed:teacher':'陈老师','signed:math':'数学'},before_segments:[{effective_at:stamp,valid_to:null,fields}]}};
 const host=document.createElement('div');const app=createApp(Proposal,{proposal,complete:true});app.mount(host);await nextTick();
 expect(host.querySelector('tbody')?.textContent).toContain('陈老师');expect(host.querySelector('dl')?.textContent).toContain('数学');
 expect([...host.querySelectorAll('details')].some(item=>item.textContent?.includes('signed:teacher'))).toBe(true);
 expect(isDirectoryProposal(proposal)).toBe(true);
 expect(isDirectoryProposal({...proposal,summary:{...proposal.summary,reference_labels:{'signed:teacher':3}}})).toBe(false);
 app.unmount();
});


it('states the factual name time for future labels without claiming names from historical segments',async()=>{
 const {createApp,nextTick}=await import('vue');const {default:Proposal}=await import('../src/features/school_facts/DirectoryProposal.vue');
 const future='2027-09-01T00:00:00Z';const fields={offering_ref:'signed:math'};
 const proposal={proposal_ref:'p',expires_at:stamp,summary:{...summary,fact_kind:'teaching_assignment',before:fields,after:fields,reference_labels:{'signed:math':'未来正式新名称'},reference_labels_as_of:future,before_segments:[{effective_at:stamp,valid_to:null,fields}]}};
 const host=document.createElement('div');const app=createApp(Proposal,{proposal,complete:true});app.mount(host);await nextTick();
 expect(host.textContent).toContain('名称核对事实时点：'+future);
 expect(host.textContent).toContain('不代表各历史时间段当时的名称');
 expect(host.textContent).not.toContain('本次预览时点');
 expect(host.textContent).toContain('signed:math');
 expect(isDirectoryProposal(proposal)).toBe(true);
 expect(isDirectoryProposal({...proposal,summary:{...proposal.summary,reference_labels_as_of:'invalid'}})).toBe(false);
 expect(isDirectoryProposal({...proposal,summary:{...proposal.summary,reference_labels_as_of:null}})).toBe(true);
 app.unmount();
});
