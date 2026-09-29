import {expect,it} from 'vitest';
import {isCorrectionOption,isCorrectionReceipt,correctionFieldLabels,correctionFieldsByKind,correctionReceiptMatches,correctionOptionLabel} from '../src/features/school_facts/family_corrections';
it('retains exact authorized object and field in the report preview',()=>{
 const option={field_name:'assignment_source_key',label:'基础数学 · 乙校区数学 · 合成陈老师 · 主讲任教',reference:'signed:reference'};
 expect(correctionOptionLabel(option)).toBe(option.label+' · 教师任职');
 expect(isCorrectionOption({...option,label:'课'.repeat(400)})).toBe(true);
 expect(isCorrectionOption({...option,label:'课'.repeat(801)})).toBe(false);
});
it.each(['display_name','position_title','contact_value','teaching_role','offering_status','course_source_key','valid_to'])('accepts approved %s correction field and readable label',field=>{
 expect(isCorrectionOption({field_name:field,label:'已展示内容',reference:'signed:reference'})).toBe(true);
 expect(isCorrectionReceipt({kind:'family_fact_correction_receipt',report_id:'r',status:'submitted',field_name:field,submitted_at:'2026-09-14T00:00:00Z'})).toBe(true);
 expect(correctionFieldLabels[field]).toBeTruthy();
});
it('limits membership expiry to its approved fact kind',()=>{
 expect(correctionFieldsByKind.teaching_group_member).toContain('valid_to');
 expect(correctionFieldsByKind.person).not.toContain('valid_to');
 expect(correctionFieldLabels.valid_to).toBe('成员有效截止时间');
});
it('keeps leadership and teaching owners distinct',()=>{
 expect(correctionFieldsByKind.assignment).toEqual(['position_title','person_source_key','organization_source_key']);
 expect(correctionFieldsByKind.teaching_assignment).toContain('assignment_source_key');
 expect(correctionFieldsByKind.person).toContain('display_name');
});
it.each(['correct','cancel','end'])('admits exact teaching %s receipts but not unrelated targets',operation=>{
 const reference='f4:teaching_assignment:signed';
 const target={reference,operation,fact_kind:'teaching_assignment'};
 expect(correctionReceiptMatches(target,reference,'assignment_source_key','maintain')).toBe(true);
 expect(correctionReceiptMatches({...target,reference:reference+'other'},reference,'assignment_source_key','maintain')).toBe(false);
 expect(correctionReceiptMatches(target,reference,'contact_value','maintain')).toBe(false);
});
it('leadership corrected receipt must not use educational operation semantics',()=>{
 const reference='f2:assignment:signed';
 expect(correctionReceiptMatches({reference,fact_kind:'assignment',operation:'correct'},reference,'position_title','maintain')).toBe(false);
 expect(correctionReceiptMatches({reference,fact_kind:'assignment',operation:'upsert'},reference,'position_title','maintain')).toBe(true);
});

it('accepts the optional official fact kind while retaining legacy receipts',async()=>{
 const {isCorrectionReceipt}=await import('../src/features/school_facts/family_corrections');
 const old={kind:'family_fact_correction_receipt',report_id:'own-report',status:'submitted',field_name:'assignment_source_key',submitted_at:'2026-09-14T00:00:00Z'};
 expect(isCorrectionReceipt(old)).toBe(true);
 expect(isCorrectionReceipt({...old,fact_kind:'teaching_assignment'})).toBe(true);
 expect(isCorrectionReceipt({...old,fact_kind:[] })).toBe(false);
});
