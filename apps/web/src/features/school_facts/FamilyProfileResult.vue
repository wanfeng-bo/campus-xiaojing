<script setup lang="ts">
import type { FamilyProfile } from "./family_contracts";
import { familyFieldLabels, familyFieldValue } from "./family_contracts";
defineProps<{result:FamilyProfile}>();
const notices:Record<string,string>={denied:"当前身份无权查看这项资料，请核对已选择的孩子和家校关系。",source_unavailable:"资料暂时无法读取，请稍后重试。",conflict:"正式记录存在不一致，暂不能确定答案，请联系学校核验。",not_recorded:"这项资料尚未录入，不能据此认定实际不存在。",no_current_records:"当前时点没有可提供的正式记录，不能据此认定实际不存在。"};
</script>
<template>
  <section data-role="family-profile-result" aria-label="当前孩子资料">
    <p v-if="notices[result.status]" :role="['denied','source_unavailable','conflict'].includes(result.status)?'alert':'status'">{{notices[result.status]}}</p>
    <dl><template v-for="(value,key) in result.profile" :key="key"><dt>{{familyFieldLabels[key]}}</dt><dd>{{result.conflicting_fields?.includes(key)?"记录不一致，暂无法确定":familyFieldValue(key,value)}}</dd></template></dl>
    <p v-if="result.missing.length">尚未提供：{{result.missing.map(key=>familyFieldLabels[key]).join('、')}}。</p>
    <details><summary>查看依据和适用范围</summary><p>{{result.query_scope}}</p><p>资料时点：{{result.as_of}}</p><p>{{result.coverage.meaning}}</p>
      <p v-for="(source,index) in result.sources" :key="index">{{source.source_name??'正式发布资料'}} · {{source.channel}} · {{source.published_at}}</p>
      <p v-if="Object.keys(result.field_restrictions).length">当前身份不可查看：{{Object.keys(result.field_restrictions).map(key=>familyFieldLabels[key]??key).join('、')}}。</p>
    </details>
  </section>
</template>
<style scoped>dl{display:grid;grid-template-columns:7em 1fr;gap:.6rem}dt{color:#587078}dd{margin:0;overflow-wrap:anywhere}details{margin-top:1rem}p{line-height:1.6}</style>
