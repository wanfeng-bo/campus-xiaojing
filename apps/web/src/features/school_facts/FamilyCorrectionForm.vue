<script setup lang="ts">
import {computed,onUnmounted,ref,shallowRef,watch} from 'vue';
import {isCorrectionReceipt,correctionOptionLabel,type CorrectionOption,type CorrectionReceipt} from './family_corrections';

const props=defineProps<{identityLabel:string;options:CorrectionOption[];identityInvalid?:boolean}>();
const emit=defineEmits<{identityInvalid:[]}>();
const field=ref('');const statement=ref('');const acknowledged=ref(false);const busy=ref(false);const error=ref('');
const preview=shallowRef<{reference:string;field_name:string;label:string;statement:string;idempotency_key:string}|null>(null);
const receipt=shallowRef<CorrectionReceipt|null>(null);const outcome=ref<'draft'|'unknown'|'received'|'rejected'>('draft');
const activity=ref<'submitting'|'lookup'|null>(null);
const selectable=computed(()=>props.options.filter(item=>item.reference&&item.label));
let epoch=0;let disposed=false;let pending:AbortController|null=null;
function invalidate(){epoch++;pending?.abort();pending=null;busy.value=false;field.value='';statement.value='';preview.value=null;receipt.value=null;acknowledged.value=false;outcome.value='draft';error.value='';}
watch(()=>props.identityInvalid,v=>{if(v)invalidate();},{flush:'sync'});
watch([field,statement],()=>{if(outcome.value==='draft'){preview.value=null;acknowledged.value=false;}});
function review(){
  if(props.identityInvalid||busy.value||outcome.value!=='draft')return;
  const option=selectable.value.find(item=>item.reference===field.value);const text=statement.value.trim();
  if(!option||!text||text.length>1000)return;
  preview.value={reference:option.reference,field_name:option.field_name,label:correctionOptionLabel(option),statement:text,idempotency_key:crypto.randomUUID()};
  acknowledged.value=false;error.value='';
}
async function request(lookup:boolean){
  const entry=preview.value;
  if(disposed||props.identityInvalid||busy.value||!entry||(!lookup&&(!acknowledged.value||outcome.value!=='draft'))||(lookup&&outcome.value!=='unknown'))return;
  if(!lookup&&!selectable.value.some(option=>option.field_name===entry.field_name&&option.reference===entry.reference)){error.value='资料已变化，请重新查询后核对报错内容。';preview.value=null;return;}
  const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;activity.value=lookup?'lookup':'submitting';error.value='';
  // After dispatch the result may already be durable, even if the browser disconnects.
  outcome.value='unknown';
  try{
    const response=await fetch(lookup?'/api/v1/school-fact-product/family/corrections/by-key/'+encodeURIComponent(entry.idempotency_key):'/api/v1/school-fact-product/family/corrections',{
      method:lookup?'GET':'POST',credentials:'same-origin',headers:lookup?{}:{'Content-Type':'application/json'},
      ...(lookup?{}:{body:JSON.stringify({reference:entry.reference,field_name:entry.field_name,statement:entry.statement,idempotency_key:entry.idempotency_key})}),signal:controller.signal,
    });
    if(disposed||current!==epoch||props.identityInvalid)return;
    if(response.status===401){invalidate();emit('identityInvalid');return;}
    if([403,422].includes(response.status)){outcome.value='rejected';error.value=response.status===403?'当前身份不能提交或查看这项报错，请重新核对身份。':'本次报错内容未被接受，请重新查询并核对。';return;}
    if(!response.ok)throw new Error('unresolved');
    const value:unknown=await response.json();
    if(disposed||current!==epoch||props.identityInvalid)return;
    if(!isCorrectionReceipt(value))throw new Error('unresolved');
    receipt.value=value;outcome.value='received';
  }catch{
    if(!disposed&&current===epoch&&!props.identityInvalid)error.value='暂时无法确定学校是否收到，请查询本次报错回执，不要重复提交。';
  }finally{if(!disposed&&current===epoch){busy.value=false;activity.value=null;pending=null;}}
}
function edit(){if(busy.value||outcome.value==='unknown')return;preview.value=null;receipt.value=null;acknowledged.value=false;outcome.value='draft';error.value='';}
onUnmounted(()=>{disposed=true;invalidate();});
</script>
<template>
  <section aria-label="孩子资料报错">
    <h3>这项资料有误</h3><p>请说明哪里不对，由学校有权人员核对。提交报错不会直接修改孩子资料，也不会重新建立家校关联。</p>
    <form v-if="!identityInvalid && outcome==='draft'" @submit.prevent="review">
      <label>需要核对的内容<select v-model="field" data-role="correction-field" :disabled="busy"><option value="">请选择</option><option v-for="item in selectable" :key="item.reference" :value="item.reference">{{correctionOptionLabel(item)}}</option></select></label>
      <label>哪里不对<textarea v-model="statement" maxlength="1000" rows="3" :disabled="busy" placeholder="仅说明这项资料的问题，请勿填写证件号码或无关私人资料。" /></label>
      <button type="submit" data-action="review-correction" :disabled="busy||!field||!statement.trim()">核对报错内容</button>
    </form>
    <section v-if="preview&&!identityInvalid&&outcome==='draft'" aria-label="核对报错内容">
      <p>{{identityLabel}}</p><p>需要核对：{{preview.label}}</p><p class="statement">{{preview.statement}}</p><p>只提交核对线索，不会直接修改孩子资料。</p>
      <label class="ack"><input v-model="acknowledged" type="checkbox" :disabled="busy" />我已核对孩子、报错内容并同意提交</label>
      <button type="button" data-action="confirm-correction" :disabled="busy||!acknowledged" @click="request(false)">确认提交报错</button>
    </section>
    <p v-if="busy" role="status">{{activity==='lookup'?'正在核对本次报错回执……':'正在提交报错……'}}</p>
    <p v-if="error" role="alert">{{error}}</p>
    <button v-if="outcome==='unknown'&&!identityInvalid" type="button" data-action="lookup-correction" :disabled="busy" @click="request(true)">查询本次报错回执</button>
    <section v-if="receipt&&!identityInvalid" aria-label="报错回执">
      <p>{{receipt.status==='resolved'?'学校已登记这项报错的正式处理结果。请重新查询当前资料。':'学校已收到，尚未登记处理结果。'}}</p>
      <details><summary>查看提交记录</summary><p>提交时间：{{new Date(receipt.submitted_at).toLocaleString()}}</p><p>回执编号：{{receipt.report_id}}</p></details>
    </section>
    <button v-if="outcome==='rejected'&&!identityInvalid" type="button" :disabled="busy" @click="edit">重新核对报错内容</button>
  </section>
</template>
<style scoped>
form,label{display:grid;gap:.55rem;margin:.6rem 0}select,textarea{font:inherit;max-width:100%;box-sizing:border-box;padding:.6rem;border:1px solid #afc8cb;border-radius:7px}p{line-height:1.6}.statement{white-space:pre-wrap;overflow-wrap:anywhere}.ack{display:flex;align-items:flex-start}details{color:#587078;overflow-wrap:anywhere}
</style>
