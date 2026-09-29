<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { createDirectoryApi } from "./directory_api";
import { ProductApiError } from "./product_api";
import { displayField, qualityLabel, type DirectoryQuery, type DirectoryOriginal } from "./directory_contracts";
import { directoryFieldLabel, directoryFieldValue, fieldLabels } from "./directory_labels";
const props = defineProps<{ result: DirectoryQuery; editable?: boolean; disabled?: boolean; selectable?: boolean; evidenceOnly?: boolean }>();
const emit = defineEmits<{ edit: [reference: string]; related: [reference: string]; identityInvalid: [] }>();
const original = ref<DirectoryOriginal | null>(null); const busy = ref(false); const error = ref(""); let disposed = false; let epoch = 0; const identityInvalid = ref(false);
watch(() => props.result, () => { epoch += 1; original.value = null; busy.value = false; error.value = ""; }, { flush: "sync" });
watch(() => props.disabled, disabled => { if (disabled) { epoch += 1; original.value = null; busy.value = false; } }, { flush: "sync" });
onUnmounted(() => { disposed = true; original.value = null; });
const status = computed(() => props.evidenceOnly && ["ambiguous", "selection_required"].includes(props.result.status)
  ? "该次查询返回的候选对象，供核对依据。" : ({ unsupported_selection: "查询条件不受此事实类型支持", known: "已取得正式资料", denied: "当前身份无权查询此范围", forbidden: "当前身份无权查询此范围", object_unresolved: "当前获准资料中尚未明确查询对象", conflict: "正式资料存在不一致，需核验", source_unavailable: "来源暂时不可用", not_recorded: "尚未录入或覆盖未确认", no_current_records: "当前查询范围内没有可提供的有效记录", unknown: "暂无足够正式资料", ambiguous: "存在多个候选，请明确对象", selection_required: "请选择查询对象", unmatched: "没有匹配到获权对象" }[props.result.status] ?? "请结合查询范围核对结果"));
async function readOriginal(reference: string) {
  if (disposed || props.disabled || identityInvalid.value || busy.value) return;
  const current = epoch;
  busy.value = true; original.value = null; error.value = "";
  try {
    const value = await createDirectoryApi().original(reference, props.result.factual_at);
    if (!disposed && !props.disabled && !identityInvalid.value && current === epoch) original.value = value;
  } catch (cause) {
    if (!disposed && !props.disabled && current === epoch) {
      error.value = cause instanceof ProductApiError ? cause.message : "原文暂时无法读取。";
      if (cause instanceof ProductApiError && cause.status === 401) {
        identityInvalid.value = true; busy.value = false;
        emit("identityInvalid");
      }
    }
  } finally { if (!disposed && current === epoch) busy.value = false; }
}
</script>
<template>
  <section class="directory-result" aria-label="学校事实查询结果">
    <p role="status">{{ status }}</p>
    <template v-if="evidenceOnly">
      <p>{{ result.coverage.description }}</p>
      <p v-if="result.truncated">结果未完整展示，请缩小查询范围。</p>
      <p v-if="!result.records.length">未返回记录不等于学校正式声明不存在。</p>
      <p v-if="result.quality">{{ result.quality.notice }}</p>
      <div v-for="record in result.records" :key="record.reference" class="evidence-summary">
        <h3>{{ record.label }}</h3><p>来源：{{ record.source.label }}</p>
      </div>
    </template>
    <component :is="evidenceOnly ? 'details' : 'div'" class="maintenance-information">
    <summary v-if="evidenceOnly">维护核查信息</summary>
    <p v-if="result.query_support" aria-label="查询条件说明">{{ result.query_support.guidance }}</p>
    <p>事实时点：{{ result.factual_at }} · 查询时间：{{ result.queried_at }}</p>
    <p>覆盖说明：{{ result.coverage.description }}（{{ ({ complete: "完整", partial: "部分", unknown: "未确认" }[result.coverage.status] ?? result.coverage.status) }}）</p>
    <p>本次返回 {{ result.returned_count }} 项{{ result.matched_count === null ? "；匹配总数未知" : `；匹配 ${result.matched_count} 项` }}{{ result.truncated ? "；结果未完整展示，请缩小查询条件。" : "" }}</p>
    <p v-if="!result.records.length">未返回记录不等于学校正式声明不存在。</p>
    <section v-if="result.quality" aria-label="正式事实质量说明" class="fact-card">
      <h3>正式事实质量说明</h3><p>{{ result.quality.notice }}</p>
      <p>存在性：{{ qualityLabel(result.quality.existence) }}；覆盖：{{ qualityLabel(result.quality.coverage) }}；来源：{{ qualityLabel(result.quality.source_availability) }}</p>
      <p>时效：{{ qualityLabel(result.quality.freshness) }}；争议：{{ qualityLabel(result.quality.conflict) }}；质量证据读取：{{ qualityLabel(result.quality.evidence_status) }}</p>
      <p v-if="result.quality.evidence_truncated">质量声明未完整返回，不据此认定完整覆盖。</p>
      <p v-if="result.records.length && result.quality.source_availability === 'unavailable'">质量声明提示来源不可用；下方已取得的正式事实仍按其来源与时点展示。</p>
      <article v-for="claim in result.quality.statements" :key="claim.reference">
        <h4>{{ qualityLabel(claim.kind) }}</h4><p>所属{{ qualityLabel(claim.owner_kind) }}：{{ claim.owner_ref ?? '当前学校' }}；目标对象：{{ claim.target_ref ?? '该所属对象的明确集合' }}</p>
        <p>{{ claim.applies_to_query ? '适用于本次查询' : '仅限列明对象及用途，不扩大为本次查询全范围结论' }}</p>
        <p v-if="claim.applies_to_records.length" class="reference">适用记录：{{ claim.applies_to_records.join('；') }}</p>
        <dl><template v-for="key in ['purpose', 'contact_kind', 'basis_reference', 'responsibility', 'verified_at', 'evidence_reference', 'coverage_status', 'negative_code', 'dispute_status', 'source_status', 'last_success_at', 'review_due_at', 'replacement_ref']" :key="key"><template v-if="claim[key] !== undefined"><dt>{{ fieldLabels[key] ?? key }}</dt><dd>{{ ['coverage_status', 'dispute_status', 'source_status'].includes(key) ? qualityLabel(String(claim[key])) : displayField(claim[key]) }}</dd></template></template></dl>
        <p>声明有效时间：{{ claim.valid_from }} 至 {{ claim.valid_to ?? '未设结束时间' }}</p><p class="reference">声明引用：{{ claim.reference }}</p>
      </article>
    </section>
    <article v-for="record in result.records" :key="record.reference" class="fact-card">
      <h3>{{ record.label }}</h3><p class="reference">对象引用：{{ record.reference }} · 版本 {{ record.version }}</p>
      <dl><template v-for="(value, name) in record.fields" :key="name"><template v-if="name !== 'content_text'"><dt>{{ directoryFieldLabel(record.fact_kind, name) }}</dt><dd>{{ displayField(directoryFieldValue(name, value)) }}</dd></template></template></dl>
      <template v-if="record.fact_kind === 'policy_version'"><p>制度正文请通过正式原文核对；模型解释和节选不能代替原文。</p><p v-if="record.original_text" class="original-text">{{ record.original_text.excerpt }}<br />{{ record.original_text.notice }}（共 {{ record.original_text.length }} 字）</p><button type="button" :disabled="disabled || identityInvalid || busy" @click="readOriginal(record.reference)">查看完整制度原文</button></template>
      <p>来源：{{ record.source.label }} · 发布时间：{{ record.source.published_at ?? "未提供" }}</p><p v-if="record.source.reference" class="reference">来源引用：{{ record.source.reference }}</p>
      <p>有效时间：{{ record.valid_from }} 至 {{ record.valid_to ?? "未设结束时间" }}</p>
      <button v-if="!evidenceOnly && editable" type="button" data-action="edit-directory" :disabled="disabled || identityInvalid" @click="$emit('edit', record.reference)">准备维护此对象</button>
      <button v-if="!evidenceOnly && (editable || selectable)" type="button" :disabled="disabled || identityInvalid" @click="$emit('related', record.reference)">用此对象查询关联事实</button>
    </article>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="original" aria-label="完整制度原文" class="fact-card"><h3>完整制度原文 · 版本 {{ original.version }}</h3><p>{{ original.notice }} 共 {{ original.length }} 字。</p><p>来源：{{ original.source.label }}</p><p class="reference">制度版本引用：{{ original.reference }}</p><pre>{{ original.text }}</pre><button type="button" @click="original = null">收起原文</button></section>
    </component>
  </section>
</template>
<style scoped>
.directory-result { font-size: .9rem; } .fact-card { margin: .8rem 0; padding: 1rem; border: 1px solid #d5e3e3; border-radius: 12px; } dl { display: grid; grid-template-columns: 8em minmax(0, 1fr); gap: .5rem; } dd { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; } .reference { color: #546c71; font-size: .8rem; overflow-wrap: anywhere; } pre, .original-text { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; max-height: 34rem; overflow: auto; } button { margin: .3rem; }
.evidence-summary h3 { margin-bottom: .3rem; } .evidence-summary p { margin-top: 0; } summary { cursor: pointer; color: #587078; }
</style>
