<script setup lang="ts">
import {computed,onUnmounted,ref,shallowRef,watch} from 'vue';
import {familyFieldLabels} from './family_contracts';
import {isCorrectionReceipt,type CorrectionReceipt,correctionFieldLabels as sharedCorrectionLabels,correctionReceiptMatches} from './family_corrections';
const props=defineProps<{identityInvalid?:boolean;staff?:boolean;identityLabel:string;targetReference?:string;fieldName?:string;requiredAction?:'maintain'|'end'|'revoke'}>();
const emit=defineEmits<{identityInvalid:[]}>();
const correctionFieldLabels:Record<string,string>={...familyFieldLabels,...sharedCorrectionLabels,class_source_key:'孩子所在班级',assignment_source_key:'班主任任职／任课教师任职'};
type Item=CorrectionReceipt&{reference?:string;student_label?:string;field_label?:string;statement?:string;required_action?:string;target_reference?:string};
type Choice={reference:string;label:string;confirmed_at:string};
const items=shallowRef<Item[]|null>(null);const busy=ref(false);const error=ref('');const chosen=shallowRef<Item|null>(null);const choices=shallowRef<Choice[]>([]);const receiptRef=ref('');const acknowledged=ref(false);const unresolved=ref(false);
type ReviewPreparation={kind:'family_correction_review_preparation';report_id:string;field_name:string;explanation:string;target_version:number;expires_at:string;confirmation_token:string};
const reviewTarget=shallowRef<Item|null>(null);const reviewExplanation=ref('');const reviewPrepared=shallowRef<ReviewPreparation|null>(null);const reviewAcknowledged=ref(false);const reviewUnknown=ref(false);
const usable=computed(()=>!props.identityInvalid&&(!props.staff||!!props.targetReference&&!!props.fieldName&&!!props.requiredAction));
let epoch=0;let pending:AbortController|null=null;let disposed=false;
function invalidate(){epoch++;pending?.abort();pending=null;busy.value=false;items.value=null;chosen.value=null;choices.value=[];receiptRef.value='';acknowledged.value=false;error.value='';unresolved.value=false;reviewTarget.value=null;reviewExplanation.value='';reviewPrepared.value=null;reviewAcknowledged.value=false;reviewUnknown.value=false;}
watch(()=>[props.identityInvalid,props.targetReference,props.fieldName,props.requiredAction],()=>invalidate(),{flush:'sync'});
watch(receiptRef,()=>{acknowledged.value=false;});
function endpoint(){
  if(!props.staff)return '/api/v1/school-fact-product/family/corrections?limit=20';
  return '/api/v1/school-fact-product/family-facts/corrections?'+new URLSearchParams({target_reference:props.targetReference!,field_name:props.fieldName!,required_action:props.requiredAction!,limit:'20'});
}
function validItem(value:unknown):value is Item {
  if(!isCorrectionReceipt(value))return false;
  if(!props.staff)return true;
  const v=value as Item;return typeof v.reference==='string'&&typeof v.student_label==='string'&&typeof v.field_label==='string'&&typeof v.statement==='string'&&v.statement.length<=1000&&v.target_reference===props.targetReference&&v.required_action===props.requiredAction&&v.field_name===props.fieldName;
}
async function read(response:Response){if(response.status===401){invalidate();emit('identityInvalid');throw new Error('invalid');}if(!response.ok)throw new Error(response.status===403?'denied':'unavailable');return response.json();}
async function load(){
  if(!usable.value||busy.value||disposed||reviewUnknown.value)return;const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;error.value='';
  try{const response=await fetch(endpoint(),{credentials:'same-origin',signal:controller.signal});if(disposed||epoch!==current||!usable.value)return;const value:unknown=await read(response);if(disposed||epoch!==current||!usable.value)return;
    const rows=(value as {items?:unknown})?.items;if(!Array.isArray(rows)||rows.length>20||!rows.every(validItem))throw new Error('invalid');items.value=rows;chosen.value=null;choices.value=[];receiptRef.value='';acknowledged.value=false;unresolved.value=false;
  }catch(e){if(!disposed&&epoch===current&&usable.value)error.value=e instanceof Error&&e.message==='denied'?'当前身份不能查看这些报错。':'暂时未能查询报错记录，不能据此判断没有报错。';}
  finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
function matchingReceiptTarget(target:unknown):boolean {
  return correctionReceiptMatches(target,props.targetReference,props.fieldName,props.requiredAction);
}
async function choose(item:Item){
  if(!props.staff||!usable.value||busy.value||unresolved.value||reviewUnknown.value||item.status!=='submitted')return;
  reviewTarget.value=null;reviewPrepared.value=null;
  chosen.value=item;choices.value=[];receiptRef.value='';acknowledged.value=false;error.value='';
  const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;
  try{const response=await fetch('/api/v1/school-fact-product/family-facts/history-receipts?limit=20',{credentials:'same-origin',signal:controller.signal});if(disposed||epoch!==current||!usable.value)return;const data=await read(response);if(disposed||epoch!==current||!usable.value)return;
    if(data?.kind!=='own_fact_receipts'||!Array.isArray(data.items)||data.items.length>20)throw new Error('invalid');
    choices.value=data.items.filter((row:any)=>row?.status==='succeeded'&&typeof row.reference==='string'&&typeof row.confirmed_at==='string'&&Number.isFinite(Date.parse(row.confirmed_at))&&Array.isArray(row.targets)&&row.targets.some(matchingReceiptTarget))
      .map((row:any)=>({reference:row.reference,confirmed_at:row.confirmed_at,label:row.targets.filter((target:any)=>target.reference===props.targetReference).map((target:any)=>typeof target.label==='string'?target.label:'本次对象').join('、')}));
  }catch{if(!disposed&&epoch===current&&usable.value)error.value='暂时未能取得本人获准查看的正式处理回执。';}
  finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
async function resolve(){
  const item=chosen.value;if(!item||!usable.value||busy.value||unresolved.value||!acknowledged.value||!choices.value.some(c=>c.reference===receiptRef.value))return;
  const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;error.value='';unresolved.value=true;
  try{const response=await fetch('/api/v1/school-fact-product/family-facts/corrections/'+encodeURIComponent(item.report_id)+'/resolve',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({writer_receipt_ref:receiptRef.value}),signal:controller.signal});if(disposed||epoch!==current||!usable.value)return;
    if([403,409,422].includes(response.status)){unresolved.value=false;error.value='本次回执或当前处理权限不符合这项报错，请核对对象、字段和实际修改。';return;}
    const data:unknown=await read(response);if(disposed||epoch!==current||!usable.value)return;if(!isCorrectionReceipt(data)||data.report_id!==item.report_id||data.status!=='resolved')throw new Error('unknown');
    items.value=items.value?.map(row=>row.report_id===item.report_id?{...row,...data}:row)??null;unresolved.value=false;chosen.value=null;choices.value=[];
  }catch{if(!disposed&&epoch===current&&usable.value)error.value='处理登记结果暂时不明，请刷新本次报错记录核对，不要重复提交。';}
  finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
function startReview(item:Item){
 if(!props.staff||!usable.value||busy.value||unresolved.value||reviewUnknown.value||item.status!=='submitted')return;
 chosen.value=null;reviewTarget.value=item;reviewExplanation.value='';reviewPrepared.value=null;reviewAcknowledged.value=false;error.value='';
}
watch(reviewExplanation,()=>{if(!reviewUnknown.value){reviewPrepared.value=null;reviewAcknowledged.value=false;}});
async function prepareReview(){
 const item=reviewTarget.value;if(!item||!usable.value||busy.value||unresolved.value||reviewUnknown.value||!reviewExplanation.value.trim())return;
 const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;error.value='';reviewPrepared.value=null;reviewAcknowledged.value=false;
 try{const response=await fetch('/api/v1/school-fact-product/family-facts/corrections/'+encodeURIComponent(item.report_id)+'/prepare-review',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({explanation:reviewExplanation.value,required_action:props.requiredAction}),signal:controller.signal});if(disposed||epoch!==current||!usable.value)return;const data=await read(response);if(disposed||epoch!==current||!usable.value)return;
  if(data?.kind!=='family_correction_review_preparation'||data.report_id!==item.report_id||data.field_name!==item.field_name||typeof data.confirmation_token!=='string'||typeof data.explanation!=='string'||!data.explanation.trim()||data.explanation.length>1000||!Number.isInteger(data.target_version)||data.target_version<1||typeof data.expires_at!=='string'||!Number.isFinite(Date.parse(data.expires_at)))throw new Error('invalid');reviewPrepared.value=data;
 }catch{if(!disposed&&epoch===current&&usable.value)error.value='暂时无法准备核实结果，请重新核对当前对象和权限。';}finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
async function submitReview(checkOnly=false){
 const item=reviewTarget.value,prep=reviewPrepared.value;if(!item||!prep||!usable.value||busy.value||unresolved.value||(!checkOnly&&(!reviewAcknowledged.value||reviewUnknown.value)))return;
 const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;error.value='';if(!checkOnly)reviewUnknown.value=true;
 try{const response=await fetch('/api/v1/school-fact-product/family-facts/corrections/'+encodeURIComponent(item.report_id)+(checkOnly?'/review-result':'/confirm-review'),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation_token:prep.confirmation_token,...(checkOnly?{}:{confirmed:true})}),signal:controller.signal});if(disposed||epoch!==current||!usable.value)return;
  if(!checkOnly&&[403,409,422].includes(response.status)){reviewUnknown.value=false;reviewPrepared.value=null;error.value='当前权限、资料或确认已变化，请重新核对。';return;}
  const data=await read(response);if(disposed||epoch!==current||!usable.value)return;if(!isCorrectionReceipt(data)||data.report_id!==item.report_id||data.status!=='reviewed')throw new Error('unknown');
  items.value=items.value?.map(row=>row.report_id===item.report_id?{...row,...data}:row)??null;reviewUnknown.value=false;reviewTarget.value=null;reviewPrepared.value=null;
 }catch{if(!disposed&&epoch===current&&usable.value)error.value='核实登记结果暂时不明，请查询本次原核实结果，不要重复提交。';}finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
onUnmounted(()=>{disposed=true;invalidate();});
</script>
<template><section :aria-label="staff?'当前对象的资料报错':'我的报错记录'">
  <h3>{{staff?'当前对象的资料报错':'我的报错记录'}}</h3><p v-if="staff">{{identityLabel}}。仅查询当前选中对象和内容下有权处理的报错。</p>
  <button type="button" data-action="list-corrections" :disabled="!usable||busy||reviewUnknown" @click="load">{{staff?'查看或刷新当前对象报错':'查看或刷新我的报错'}}</button>
  <p v-if="busy" role="status">正在核对报错记录……</p><p v-if="error" role="alert">{{error}}</p>
  <template v-if="items&&!identityInvalid"><p v-if="!items.length">本次获准范围内未查到报错记录。</p><article v-for="item in items" :key="item.report_id">
    <p><template v-if="staff">{{item.student_label}} · </template>{{item.field_label??correctionFieldLabels[item.field_name]}}</p>
    <p>{{item.status==='reviewed'?'已核实，原资料无误；未修改正式资料。':item.status==='resolved'?'已登记正式处理结果，请重新查询当前资料。':'已收到，尚未登记处理结果。'}}</p>
    <p v-if="item.status==='reviewed'" class="statement">{{item.review_explanation}}</p>
    <p v-if="staff" class="statement">{{item.statement}}</p>
    <details><summary>查看提交记录</summary><p>提交时间：{{new Date(item.submitted_at).toLocaleString()}}</p><p>回执编号：{{item.report_id}}</p></details>
    <button v-if="staff&&item.status==='submitted'" type="button" :disabled="busy||unresolved||reviewUnknown" @click="choose(item)">关联已完成的正式处理</button>
    <button v-if="staff&&item.status==='submitted'" type="button" data-action="start-review" :disabled="busy||unresolved||reviewUnknown" @click="startReview(item)">核实原资料无误</button>
  </article><p>只显示本次查询返回的至多20项记录，不代表全部历史。</p></template>
  <section v-if="chosen&&!identityInvalid" aria-label="核对报错处理结果"><h4>核对已完成的正式处理</h4><p>{{chosen.student_label}} · {{chosen.field_label}}</p><p class="statement">{{chosen.statement}}</p>
    <p>请先通过上方事实维护完成核对、修改和明确确认，再选择对应的本人正式回执。这里仅登记处理依据，不会再次执行原修改。</p>
    <label>本人正式处理记录<select v-model="receiptRef" :disabled="busy||unresolved"><option value="">请选择</option><option v-for="item in choices" :key="item.reference" :value="item.reference">{{item.label}} · {{new Date(item.confirmed_at).toLocaleString()}}</option></select></label>
    <p v-if="!busy&&!choices.length">本次查询中没有找到对应的本人正式回执。</p>
    <label class="ack"><input v-model="acknowledged" type="checkbox" :disabled="busy||unresolved||!receiptRef" />我已核对报错对象、内容与所选正式处理结果</label>
    <button type="button" data-action="resolve-correction" :disabled="busy||unresolved||!acknowledged||!receiptRef" @click="resolve">确认登记处理结果</button>
  </section>
  <section v-if="reviewTarget&&!identityInvalid" aria-label="核实原资料无误"><h4>核实原资料无误</h4><p>{{reviewTarget.student_label}} · {{reviewTarget.field_label}}</p><p class="statement">{{reviewTarget.statement}}</p>
    <label>核实说明<textarea v-model="reviewExplanation" data-review-explanation maxlength="1000" :disabled="busy||reviewUnknown" /></label>
    <button type="button" data-action="prepare-review" :disabled="busy||reviewUnknown||!reviewExplanation.trim()" @click="prepareReview">核对本次核实结果</button>
    <template v-if="reviewPrepared"><p class="statement">{{reviewPrepared.explanation}}</p><p>将记录“已核实，原资料无误”，不修改正式资料。</p>
      <label class="ack"><input v-model="reviewAcknowledged" data-review-ack type="checkbox" :disabled="busy||reviewUnknown" />我已核对当前正式资料、报错对象和核实说明</label>
      <button type="button" data-action="confirm-review" :disabled="busy||reviewUnknown||!reviewAcknowledged" @click="submitReview(false)">确认登记核实结果</button>
      <button v-if="reviewUnknown" type="button" data-action="review-result" :disabled="busy" @click="submitReview(true)">查询本次原核实结果</button>
    </template>
  </section>
</section></template>
<style scoped>p{line-height:1.6}article{border-top:1px solid #d9e5e5;padding:.7rem 0}.statement{white-space:pre-wrap;overflow-wrap:anywhere}label{display:grid;gap:.5rem;margin:.6rem 0}select{font:inherit;max-width:100%;padding:.5rem}.ack{display:flex;align-items:flex-start}details{color:#587078;overflow-wrap:anywhere}</style>
