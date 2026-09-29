<script setup lang="ts">
import {computed} from 'vue';
import {teachingNotices,teachingQueryOutcome,type TeachingEvidence} from './teaching_contracts';
const props=defineProps<{result:TeachingEvidence;queryLabel?:string}>();
type Row=Record<string,unknown>;
function rows(value:unknown):Row[]{return Array.isArray(value)?value.filter((r):r is Row=>!!r&&typeof r==='object'&&!Array.isArray(r)):[];}
function words(value:unknown):string[]{return Array.isArray(value)?value.filter((v):v is string=>typeof v==='string'):[];}
function text(value:unknown){return typeof value==='string'?value:'';}
function date(value:unknown){
 const raw=text(value);const stamp=new Date(raw);
 if(!raw||Number.isNaN(stamp.getTime()))return '时间尚未明确';
 const configured=props.result.fact_timezone_name;
 let zone=typeof configured==='string'&&configured?configured:'UTC';
 let known=typeof configured==='string'&&!!configured;
 try{new Intl.DateTimeFormat('zh-CN',{timeZone:zone}).format(stamp);}catch{zone='UTC';known=false;}
 return stamp.toLocaleString('zh-CN',{hour12:false,timeZone:zone})+`（${known?zone:'UTC；学校时区未明确'}）`;
}
const leaders=computed(()=>props.result.kind==='family_school_leadership');
const parts=computed(()=>Array.isArray(props.result.timeline)&&props.result.timeline.length?rows(props.result.timeline):[{items:props.result.items}]);
const sources=computed(()=>{
 const labels:string[]=[];
 const visit=(value:unknown)=>{for(const r of rows(value)){const source=r.source;if(source&&typeof source==='object'){for(const k of ['summary','label'])if(k in source && typeof source[k as keyof typeof source]==='string')labels.push(String(source[k as keyof typeof source]));}}};
 visit(props.result.sources);visit(props.result.evidence);return [...new Set(labels)];
});
const contactLabels:Record<string,string>={email:'邮箱',phone:'电话',website:'网站',address:'地址',url:'网站'};
</script>
<template>
 <section class="fact-card" :aria-label="leaders?'学校领导依据':'教学安排依据'">
   <p v-if="queryLabel">{{queryLabel}}</p>
   <p v-if="teachingQueryOutcome(result)" data-role="query-outcome">{{teachingQueryOutcome(result)}}</p>
  <p v-if="result.status==='denied'">{{teachingNotices(result,queryLabel)[0]}}</p>
  <template v-if="leaders">
   <h3>学校领导</h3>
   <article v-for="(leader,i) in rows(result.leaders)" :key="i">
    <p>{{text(leader.display_name)}} · {{text(leader.position_title)}}</p><p v-if="leader.organization">{{text(leader.organization)}}</p>
    <p v-for="(channel,j) in rows(leader.official_contacts)" :key="j">{{text(channel.owner_name)}} · {{contactLabels[text(channel.contact_kind)]??'官方渠道'}}：{{text(channel.contact_value)}}</p>
   </article>
   <p v-if="result.channel_meaning">{{text(result.channel_meaning)}}</p>
  </template>
  <template v-else>
   <h3>教学安排</h3>
   <p v-for="(candidate,index) in rows(result.candidates)" :key="'candidate'+index" data-role="query-candidate">{{text(candidate.name)}}<span v-if="text(candidate.grade_name)"> · {{text(candidate.grade_name)}}</span><span v-if="text(candidate.campus_name)"> · {{text(candidate.campus_name)}}</span><span v-if="typeof candidate.class_number==='number' && Number.isInteger(candidate.class_number) && candidate.class_number>0"> · {{candidate.class_number}}班</span></p>
   <p v-if="result.query_window">查询期间：{{date(result.query_window.start)}} 至 {{date(result.query_window.end_exclusive)}}（含起点，不含终点）；此查询范围不代表任教或开课结束。</p>
   <section v-for="(part,p) in parts" :key="p">
    <p v-if="part.from">{{date(part.from)}} 至 {{date(part.until)}}（含起点，不含终点）</p>
    <p v-if="text(part.participation)">{{text(part.participation)}}</p>
    <article v-for="(item,i) in rows(part.items)" :key="i">
     <h4>{{text(item.course)||text(item.organization)||text(item.name)||'正式教学安排'}}<span v-if="item.offering && item.offering!==item.course"> · {{text(item.offering)}}</span></h4>
     <p v-for="(scope,s) in rows(item.scopes)" :key="'scope'+s">{{text(scope.subject)}}<span v-if="scope.stage"> · {{text(scope.stage)}}</span><span v-if="scope.grade"> · {{text(scope.grade)}}</span><span v-if="scope.academic_grade"> · {{text(scope.academic_grade)}}</span><span v-if="scope.academic_year"> · {{String(scope.academic_year)}}学年</span><span v-if="scope.term"> · {{text(scope.term)}}</span></p>
     <p v-if="item.period_start">{{date(item.period_start)}} 至 {{date(item.period_end)}}（含起点，不含终点）</p>
     <p v-if="words(item.subjects).length">{{words(item.subjects).join('、')}}</p>
     <p v-for="(audience,j) in rows(item.audiences)" :key="'a'+j">{{text(audience.name)}} · {{text(audience.source)}}</p>
     <p v-if="item.teacher_status==='no_current_records'">尚无有效任课记录，不能据此确定岗位空缺。</p>
     <template v-if="item.role_status && typeof item.role_status==='object'"><p v-for="(state,role) in item.role_status" :key="String(role)">{{role}}：{{text(state)}}</p></template>
     <section v-for="(teacher,j) in rows(item.teachers)" :key="j">
      <p>{{text(teacher.name)||'教师姓名暂不可提供'}} · {{text(teacher.role)}}<span v-if="teacher.responsibility"> · {{text(teacher.responsibility)}}</span></p>
      <p v-if="teacher.basis && typeof teacher.basis==='object' && 'label' in teacher.basis">任课来源：{{text(teacher.basis.label)}}</p>
      <p v-for="(channel,k) in rows(teacher.official_contacts)" :key="k">{{text(channel.owner)||text(channel.owner_name)}} · {{contactLabels[text(channel.contact_kind)]??'官方渠道'}}：{{text(channel.contact_value)}}</p>
     </section>
     <p v-if="item.offering_status">{{text(item.offering_status)}}</p>
     <p v-for="notice in words(item.notices)" :key="notice">{{notice}}</p>
    </article>
    <p v-if="part.status==='no_current_records'">该时间段内没有符合所问条件的有效教学记录。</p>
   </section>
   <p v-if="result.period_meaning">{{text(result.period_meaning)}}</p>
   <p v-if="result.future_basis">{{text(result.future_basis)}}</p>
  </template>
  <p v-for="source in sources" :key="source">来源：{{source}}</p>
  <p v-if="result.as_of">资料时点：{{date(result.as_of)}}</p>
  <p v-if="result.coverage==='unknown'">当前结果只说明已查到的获准资料，登记是否齐全尚未声明。</p>
 </section>
</template>
