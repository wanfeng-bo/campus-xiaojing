<script setup lang="ts">
import { computed } from "vue";
import { displayField, operationLabel, type DirectoryProposal, type DirectoryDescriptor } from "./directory_contracts";
import { directoryFieldLabel, directoryFieldValue } from "./directory_labels";
const props = defineProps<{ proposal: DirectoryProposal; descriptor?: DirectoryDescriptor | null; complete?: boolean; disabled?: boolean }>();
defineEmits<{ review: [reference: string] }>();
const names = computed(() => [...new Set([...Object.keys(props.proposal.summary.before ?? {}), ...Object.keys(props.proposal.summary.after ?? {})])]);
const timelineItems = computed(() => {
  const seen = new Set<string>();
  return [props.proposal.summary, ...(props.proposal.summary.impacts ?? [])].filter(item => {
    // The batch includes its primary item. Drop only the identical timeline copy;
    // distinct targets, dates and full source/cause evidence remain visible.
    const key = JSON.stringify([item.fact_kind,item.reference,item.operation,item.effective_at,item.before_segments ?? null,item.after_segments ?? null]);
    if (seen.has(key)) return false;
    seen.add(key);return true;
  });
});
function label(name: string) { return props.descriptor?.fields.find(f => f.name === name)?.label ?? directoryFieldLabel(props.proposal.summary.fact_kind, name); }
function namedValue(item: DirectoryProposal["summary"], raw: unknown): unknown {
  if (Array.isArray(raw)) return raw.map(value => namedValue(item, value));
  return typeof raw === 'string' ? item.reference_labels?.[raw] ?? raw : raw;
}
function missingNames(item: DirectoryProposal["summary"]) {
  if (item.fact_kind !== 'teaching_assignment') return false;
  return [item.before, item.after, ...(item.before_segments ?? []).map(s => s.fields), ...(item.after_segments ?? []).map(s => s.fields)].some(fields => ['assignment_ref','offering_ref','subject_refs'].some(key => {
    const value = fields?.[key]; return (Array.isArray(value) ? value : [value]).some(ref => typeof ref === 'string' && !item.reference_labels?.[ref]);
  }));
}
function value(name: string, raw: unknown) { raw = namedValue(props.proposal.summary, raw); return displayField(props.descriptor?.fields.find(field => field.name === name)?.options.find(option => option.value === raw)?.label ?? directoryFieldValue(name, raw)); }
</script>
<template>
  <section class="proposal-card" aria-label="事实待确认修改">
    <h3>{{ complete ? "完整修改差异" : "待确认修改摘要" }}：{{ proposal.summary.label }}</h3>
    <p v-if="complete" aria-label="本次修改总数">本次全部修改（{{timelineItems.length}}项）</p>
    <p>操作：{{ operationLabel(proposal.summary.operation) }}</p>
    <p v-if="complete">对象引用：{{ proposal.summary.reference }}</p>
    <details v-else><summary>查看对象引用</summary><p>对象引用：{{ proposal.summary.reference }}</p></details>
    <p>生效时间：{{ proposal.summary.effective_at }}</p>
    <section v-if="complete && proposal.summary.regroup" aria-label="本批名单与去向">
      <h4>本批名单与去向</h4><p>{{proposal.summary.regroup.notice}}</p>
      <article v-for="period in proposal.summary.regroup.periods" :key="period.effective_at">
        <h5>{{period.effective_at}} · 本批 {{period.student_count}} 名学生</h5>
        <table><thead><tr><th>学生</th><th>原班</th><th>明确去向</th></tr></thead><tbody><tr v-for="row in period.departures" :key="row.student_ref"><td>{{row.student_ref}}</td><td>{{row.from_class_ref ?? '本批未涉及原班'}}</td><td>{{row.status==='unassigned'?'正式待分班':row.to_class_ref}}</td></tr></tbody></table>
        <table><thead><tr><th>班级</th><th>本批原班人数</th><th>本批进入人数</th><th>班级本次安排</th></tr></thead><tbody><tr v-for="row in period.classes" :key="row.class_ref"><td>{{row.label ?? row.class_ref}}</td><td>{{row.outgoing_count}}</td><td>{{row.incoming_count}}</td><td>{{row.class_state==='ended'?'本批明确结束班级':row.class_state==='recorded'?'本批明确登记班级':'本批没有更改班状态'}}</td></tr></tbody></table>
        <p>重复学生：无（重复名单不能确认）。正式待分班：{{period.unassigned.length ? period.unassigned.join('、') : '本批无'}}。</p>
        <p>新班主任、固定教室、班内职务须分别正式登记，不能继承原班安排。</p>
      </article>
    </section>
    <p v-for="notice in proposal.summary.regroup?.teaching_notices ?? []" :key="notice" role="note">{{notice}}</p>
    <p v-for="item in proposal.summary.regroup?.teaching_cancellations ?? []" :key="item.reference" role="note">取消未来安排：{{timelineItems.find(row=>row.reference===item.reference)?.label ?? '所选教学对象'}}，原开始时点 {{item.effective_at}}。请核对下方前后期间；后续独立安排保留，原独立截止不延长。</p>
    <section v-if="complete && proposal.summary.regroup?.teaching_impacts?.length" aria-label="教学完整影响"><h4>教学完整影响</h4><ul><li v-for="item in proposal.summary.regroup.teaching_impacts" :key="item.reference">{{item.label}} · {{item.reference}} · {{item.changed ? '本次正式修改，见完整差异' : '派生影响，本次不改写该对象'}}</li></ul></section>
    <p v-if="complete && timelineItems.some(missingNames)">部分关联名称尚未核实，请核对保留的原始对象引用。</p>
    <template v-if="complete"><p v-for="(item,index) in timelineItems.filter(item => Object.keys(item.reference_labels ?? {}).length)" :key="index">{{item.label}} · 名称核对事实时点：{{item.reference_labels_as_of ?? '未提供'}}。关联名称不代表各历史时间段当时的名称，关系仍以原始引用核对。</p></template>
    <table v-if="complete"><thead><tr><th>字段</th><th>修改前</th><th>修改后</th></tr></thead><tbody><tr v-for="name in names" :key="name"><th>{{ label(name) }}</th><td><pre>{{ value(name, proposal.summary.before?.[name]) }}</pre></td><td><pre>{{ value(name, proposal.summary.after?.[name]) }}</pre></td></tr></tbody></table>
    <section v-if="complete && proposal.summary.impacts?.length" aria-label="全部联动修改"><h4>关联修改明细（{{proposal.summary.impacts.length}}项）</h4><article v-for="(impact,index) in proposal.summary.impacts" :key="index"><h5>{{index+1}}. {{impact.label}} · {{operationLabel(impact.operation)}}</h5><p>生效时间：{{impact.effective_at}} · 原因：{{impact.reason}}</p><table><thead><tr><th>字段</th><th>修改前</th><th>修改后</th></tr></thead><tbody><tr v-for="field in [...new Set([...Object.keys(impact.before ?? {}),...Object.keys(impact.after ?? {})])]" :key="field"><th>{{directoryFieldLabel(impact.fact_kind,field)}}</th><td><pre>{{displayField(directoryFieldValue(field,namedValue(impact,impact.before?.[field])))}}</pre></td><td><pre>{{displayField(directoryFieldValue(field,namedValue(impact,impact.after?.[field])))}}</pre></td></tr></tbody></table><p>{{impact.before===null?'新增正式对象':impact.after===null?'结束或撤销原安排，保留历史':''}}</p><details><summary>该项来源与对象</summary><p>{{impact.source_label}} · {{impact.reference}}</p></details></article></section>
    <section v-if="complete && (proposal.summary.before_segments?.length || proposal.summary.after_segments?.length || proposal.summary.impacts?.some(i => i.before_segments?.length || i.after_segments?.length))" aria-label="完整时间分段">
      <h4>全部正式时间分段</h4>
      <article v-for="(item,index) in timelineItems" :key="index">
        <template v-if="item.before_segments?.length || item.after_segments?.length"><h5>{{item.label}}</h5>
          <section v-for="side in (['before_segments','after_segments'] as const)" :key="side"><h6>{{side==='before_segments'?'修改前各段':'修改后各段'}}</h6>
            <p v-if="!item[side]?.length">无正式时间段</p>
            <div v-for="(segment,segmentIndex) in item[side] ?? []" :key="segmentIndex"><p>第 {{segmentIndex+1}} 段：{{segment.effective_at}} 起，{{segment.valid_to ? '至 '+segment.valid_to+' 前' : '未登记结束时间'}}</p>
              <dl><template v-for="(raw,field) in segment.fields" :key="field"><dt>{{directoryFieldLabel(item.fact_kind,String(field))}}</dt><dd><pre>{{displayField(directoryFieldValue(String(field),namedValue(item,raw)))}}</pre></dd></template></dl>
              <p v-if="segment.cause_reference">关联变更：{{segment.cause_reference}}<span v-if="segment.cause_version">（版本 {{segment.cause_version}}）</span></p><details v-if="segment.source"><summary>该段正式来源</summary><p>{{segment.source.source_ref ?? "学校正式登记"}} · {{segment.source.published_at ?? "发布时间未提供"}}</p><p v-if="segment.source.publication_reference">发布凭据：{{segment.source.publication_reference}}</p></details>
            </div>
          </section>
        </template>
      </article>
    </section>
    <details v-if="complete && timelineItems.some(item => Object.keys(item.reference_labels ?? {}).length)"><summary>关联对象名称与原始引用</summary><p>名称来自所注明事实时点的获准正式资料；完整差异中的引用保持不变。</p><dl v-for="(item,index) in timelineItems" :key="index"><template v-for="(name,reference) in item.reference_labels ?? {}" :key="reference"><dt>{{name}}</dt><dd>{{reference}}</dd></template></dl></details>
    <p v-if="complete && proposal.summary.before === null">本次创建新对象。</p><p v-if="complete && proposal.summary.after === null">本次结束或撤销既有事实，不删除历史。</p>
    <p>修改原因：{{ proposal.summary.reason }}</p><p>来源：{{ proposal.summary.source_label }}</p><p>确认有效至 {{ proposal.expires_at }}</p>
    <p v-for="notice in proposal.original_notices ?? []" :key="notice">{{ notice }}</p>
    <p>尚未执行。请核对当前履职身份、对象及全部原文后明确确认。</p>
    <button v-if="!complete" type="button" data-action="review-directory-proposal" :disabled="disabled" @click="$emit('review', proposal.proposal_ref)">核对完整修改</button>
  </section>
</template>
<style scoped>
.proposal-card { padding: 1rem; margin: 1rem 0; border: 1px solid #bdd8c7; border-radius: 10px; background: #f2f8f3; overflow-wrap: anywhere; } table { width: 100%; table-layout: fixed; border-collapse: collapse; } th, td { padding: .5rem; text-align: left; vertical-align: top; border: 1px solid #bdd8c7; } pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; max-height: 28rem; overflow: auto; margin: 0; }
</style>
