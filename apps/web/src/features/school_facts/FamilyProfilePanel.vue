<script setup lang="ts">
import { onUnmounted, ref, shallowRef, watch } from "vue";
import FamilyProfileResult from "./FamilyProfileResult.vue";
import FamilyCorrectionForm from "./FamilyCorrectionForm.vue";
import FamilyCorrectionRecords from "./FamilyCorrectionRecords.vue";
import { isFamilyProfile, type FamilyProfile } from "./family_contracts";
import { isCorrectionOption, type CorrectionOption } from "./family_corrections";
const props=withDefaults(defineProps<{identityLabel:string;identityRef?:string;identityInvalid?:boolean;assistantEnabled?:boolean}>(),{assistantEnabled:true});
const emit=defineEmits<{identityInvalid:[]}>();
const result=shallowRef<FamilyProfile|null>(null);const busy=ref(false);const error=ref("");const asOf=ref("");
let epoch=0;let disposed=false;let pending:AbortController|null=null;
const correctionOptions=shallowRef<CorrectionOption[]>([]);const correctionBusy=ref(false);const correctionError=ref('');
let correctionEpoch=0;let correctionPending:AbortController|null=null;
function stopCorrection(){correctionEpoch++;correctionPending?.abort();correctionPending=null;correctionBusy.value=false;correctionOptions.value=[];correctionError.value='';}
function stop(){epoch++;pending?.abort();pending=null;busy.value=false;result.value=null;}
watch(()=>props.identityInvalid,value=>{if(value){stop();stopCorrection();asOf.value="";error.value="当前身份已失效，请重新选择后查询。";}},{flush:"sync"});
watch(()=>[props.identityRef,props.identityLabel],()=>{stop();stopCorrection();asOf.value='';error.value='';},{flush:'sync'});
async function prepareCorrection(){
  if(disposed||props.identityInvalid||correctionBusy.value)return;
  stopCorrection();const current=correctionEpoch;const controller=new AbortController();correctionPending=controller;correctionBusy.value=true;
  try{
    const response=await fetch('/api/v1/school-fact-product/family/corrections/options',{credentials:'same-origin',signal:controller.signal});
    if(disposed||current!==correctionEpoch||props.identityInvalid)return;
    if(response.status===401){stopCorrection();emit('identityInvalid');return;}
    if(response.status===403){correctionError.value='当前身份不能提交这项资料的报错，请重新核对身份。';return;}
    if(!response.ok)throw new Error('unavailable');
    const value:unknown=await response.json();
    if(disposed||current!==correctionEpoch||props.identityInvalid)return;
    if(typeof value!=='object'||value===null||!('items' in value)||!Array.isArray(value.items)||value.items.length>107||!value.items.every(isCorrectionOption)||new Set(value.items.map(x=>x.reference)).size!==value.items.length)throw new Error('invalid options');
    correctionOptions.value=value.items;
    if(!value.items.length)correctionError.value='当前没有可提交报错的资料项，请联系学校核对。';
  }catch{if(!disposed&&current===correctionEpoch&&!props.identityInvalid)correctionError.value='暂时无法核对报错范围，现有查询结果不受影响，请稍后重试。';}
  finally{if(!disposed&&current===correctionEpoch){correctionBusy.value=false;correctionPending=null;}}
}
async function query(){
  if(disposed||props.identityInvalid||busy.value)return;
  const current=++epoch;const controller=new AbortController();pending=controller;busy.value=true;error.value="";result.value=null;
  try{
    const response=await fetch("/api/v1/school-fact-product/family/profile",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(asOf.value?{as_of:new Date(asOf.value).toISOString()}:{}),signal:controller.signal});
    if(disposed||epoch!==current||props.identityInvalid)return;
    if(response.status===401){stop();emit("identityInvalid");return;}
    if(!response.ok)throw new Error("unavailable");
    const value:unknown=await response.json();
    if(disposed||epoch!==current||props.identityInvalid)return;
    if(!isFamilyProfile(value))throw new Error("invalid projection");
    result.value=value;
  }catch{if(!disposed&&epoch===current&&!props.identityInvalid)error.value="本次未能读取正式资料，请重试；不能据此判断资料不存在。";}
  finally{if(!disposed&&epoch===current){busy.value=false;pending=null;}}
}
onUnmounted(()=>{disposed=true;stop();stopCorrection();});
</script>
<template><section aria-label="孩子资料"><h2>孩子资料</h2><p>{{identityLabel}}</p><p>本资料卡查看上方选定的孩子。<span v-if="assistantEnabled">向小静提问时，可以直接说明要问哪个孩子，不需要先切换资料卡。</span></p>
  <form @submit.prevent="query"><label>历史时点（选填）<input v-model="asOf" type="datetime-local" :disabled="identityInvalid||busy" /></label><button type="submit" data-action="query-family-profile" :disabled="identityInvalid||busy">查询正式资料</button></form>
  <p v-if="busy" role="status">正在读取正式资料……</p><p v-if="error" role="alert">{{error}}</p><FamilyProfileResult v-if="result&&!identityInvalid" :result="result" />
  <button v-if="!correctionOptions.length" type="button" data-action="prepare-family-correction" :disabled="identityInvalid||correctionBusy" @click="prepareCorrection">资料有误，提交报错</button>
  <p v-if="correctionBusy" role="status">正在核对可报错的资料范围……</p><p v-if="correctionError" role="alert">{{correctionError}}</p>
  <FamilyCorrectionForm v-if="correctionOptions.length&&!identityInvalid" :key="correctionEpoch" :identity-label="identityLabel" :options="correctionOptions" :identity-invalid="identityInvalid" @identity-invalid="emit('identityInvalid')" />
  <FamilyCorrectionRecords :identity-label="identityLabel" :identity-invalid="identityInvalid" @identity-invalid="emit('identityInvalid')" />
</section></template>
<style scoped>form,label{display:grid;gap:.6rem}input{font:inherit;padding:.6rem;border:1px solid #afc8cb;border-radius:7px}p{line-height:1.6}</style>
