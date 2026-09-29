<script setup lang="ts">
import {computed} from 'vue';
import {type ResidenceResult,residenceStatusLabel,residenceLimitation,residenceNotices} from './family_contracts';
const props=defineProps<{result:ResidenceResult;queryLabel?:string}>();
const queryNotices=computed(()=>residenceNotices(props.result,props.queryLabel??'本次住宿查询'));
const zone=computed(()=>{
 const name=props.result.fact_timezone_name;
 if(typeof name==='string'&&name.length>0&&name.length<=100){
  try{new Intl.DateTimeFormat('zh-CN',{timeZone:name});return {name,label:name};}catch{/* Unusable authority value must never fall back to browser-local time. */}
 }
 return {name:'UTC',label:'UTC；学校时区待确认'};
});
function date(value:string){const stamp=new Date(value);return Number.isFinite(stamp.getTime())?stamp.toLocaleString('zh-CN',{timeZone:zone.value.name,hour12:false})+'（'+zone.value.label+'）':'时间尚未核实';}
</script>
<template>
 <section aria-label="住宿安排依据">
  <p v-if="queryLabel">{{queryLabel}}<span v-if="result.as_of"> · 所问时点：{{date(result.as_of)}}</span></p>
  <p>{{ residenceStatusLabel(result.status) }}</p>
  <p v-if="result.future_basis">{{ result.future_basis }}</p>
  <article v-for="(record,index) in result.records" :key="index">
   <p>{{ [record.display_name,record.student_name,record.building_name,record.room_name,record.bed_name].filter(Boolean).join(' · ') }}</p>
   <p>{{ date(record.valid_from) }}起<span v-if="record.valid_to">，至{{ date(record.valid_to) }}（不含截止时点）</span></p>
   <details v-if="record.source"><summary>资料来源</summary><p>{{ record.source.label }}</p><p v-if="record.source.published_at">{{ date(record.source.published_at) }}</p></details>
  </article>
  <p v-if="result.has_more">还有获准资料尚未显示，本页不能代表全部安排。</p>
  <p v-for="(code,index) in result.limitations??[]" :key="'limit-'+index">{{ residenceLimitation(code) }}</p>
  <details v-if="queryNotices.length"><summary>本次查询的限制与核验说明</summary><p v-for="(notice,index) in queryNotices" :key="index">{{notice}}</p></details>
 </section>
</template>
