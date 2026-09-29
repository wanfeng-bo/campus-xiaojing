export type TeachingEvidence = Record<string, unknown> & {status:string;kind?:string;family_owner?:string;fact_timezone_name?:string;query_window?:{start:string;end_exclusive:string;meaning?:string}};
export function isTeachingEvidence(value:unknown):value is TeachingEvidence {
  if(!value || typeof value!=='object' || !('status' in value) || typeof value.status!=='string') return false;
  return ('kind' in value && ['family_teaching','family_school_leadership'].includes(String(value.kind))) || ('family_owner' in value && value.family_owner==='school_facts.teaching');
}
export function teachingQueryLabel(value:TeachingEvidence,index:number):string {
 return `${value.kind==='family_school_leadership'?'学校领导查询':'教学查询'} ${index+1}`;
}
export function teachingQueryOutcome(value:TeachingEvidence):string|undefined {
 return ({ambiguous:'该次查询返回多个可能的对象。',selection_required:'该次查询尚未选定对象。',no_current_records:'该次查询条件下没有当前有效记录。',not_recorded:'该次查询未取得已登记资料。'} as Record<string,string>)[value.status];
}
export function teachingNotices(value:TeachingEvidence,queryLabel='该项教学查询',partial=false):string[] {
  const state:Record<string,string>={denied:`${partial?'部分查询未获准：':''}${queryLabel}：该项查询未获准，当前身份无权读取其资料。`,conflict:'正式资料存在不一致，需要学校核对。',unavailable:'所问资料暂时无法读取。',source_unavailable:'所问资料来源暂时无法核实。',stale:'资料或身份已变化，请重新查询。'};
 const notices=state[value.status]?[state[value.status]]:[];
 if(typeof value.participation==='string')notices.push(value.participation);
 if(Array.isArray(value.items))for(const item of value.items){
  if(item && typeof item==='object' && Array.isArray(item.notices))notices.push(...item.notices.filter((n:unknown):n is string=>typeof n==='string'));
 }
 if(Array.isArray(value.quality))for(const q of value.quality){
  if(!q || typeof q!=='object')continue;
  if(q.dispute_status==='open')notices.push('相关资料有尚未解决的争议。');
  if(q.source_status==='unavailable')notices.push('相关资料的原来源目前不可用。');
 }
 if(Array.isArray(value.timeline))value.timeline.forEach((part,index)=>{
  if(!part || typeof part!=='object' || Array.isArray(part))return;
  // Only the published period level participates; unknown nested structures
  // are not interpreted as additional evidence or instructions.
  const label=`${queryLabel} 第${index+1}时间段`;
  const segment:TeachingEvidence={...part,status:typeof part.status==='string'?part.status:'',timeline:undefined};
  notices.push(...teachingNotices(segment,label,partial).map(notice=>`${label}：${notice}`));
 });
 return [...new Set(notices)];
}
