<script setup lang="ts">
import {educationFieldLabels,educationValue,educationQuality,type FamilyEducation} from './family_education_contracts';
defineProps<{result:FamilyEducation}>();
</script>
<template>
<section class="fact-card" aria-label="孩子教育资料依据">
 <h3>孩子教育资料</h3>
 <p v-for="field in result.authorized_fields" :key="field">{{educationFieldLabels[field]}}：{{result.profile[field]?.length?result.profile[field]!.map(v=>educationValue(field,v)).join('、'):educationQuality(result.quality?.[field]?.status??'unknown',field)}}<span v-if="result.profile[field]?.length && ['conflict','source_unavailable'].includes(result.quality?.[field]?.status??'')">；{{educationQuality(result.quality![field]!.status,field)}}</span></p>
 <p>事实时点：{{result.as_of}}<span v-if="result.fact_timezone_name">；学校规则时区：{{result.fact_timezone_name}}</span>。本次范围未声明完整覆盖。</p>
 <details><summary>查看正式来源与质量依据</summary>
  <p v-for="(quality,field) in result.quality" :key="field">{{educationFieldLabels[field]}}：{{educationQuality(quality!.status,field)}}；{{quality!.coverage==='complete'?'本次所问范围有完整覆盖声明':quality!.coverage==='partial'?'本次所问范围为部分覆盖':'覆盖情况尚未声明'}}<span v-if="quality!.freshness==='review_overdue'">；已到复核期限</span></p>
  <p v-for="(source,index) in result.sources" :key="index">{{source.fact==='fact_quality'?'学校质量声明':'学校正式登记资料'}}<span v-if="typeof source.source_ref==='string'">：{{source.source_ref}}</span><span v-if="typeof source.effective_at==='string'">；生效时间 {{source.effective_at}}</span><span v-if="typeof source.valid_to==='string'">；截止时间 {{source.valid_to}}</span><span v-if="typeof source.published_at==='string'">；发布时间 {{source.published_at}}</span><span v-if="typeof source.publication_reference==='string'">；发布引用 {{source.publication_reference}}</span></p>
 </details>
</section>
</template>
