<script setup lang="ts">
import { computed } from "vue";
import { productContactKind, productSourceKind, type ProductQuery } from "./product_contracts";
const props = defineProps<{ result: ProductQuery; editable?: boolean; selectable?: boolean; disabled?: boolean }>();
defineEmits<{ edit: [reference: string]; "select-campus": [result: ProductQuery, reference: string] }>();
const statuses: Record<ProductQuery["status"], string> = {
  display_limited: "本次结果显示受限",
  known: "已取得正式查询结果", not_recorded: "所查校区尚未录入官方联系", unmatched: "未匹配到校区",
  selection_required: "请选择查询校区", no_current_records: "当前查询范围内没有可提供的正式联系记录",
  ambiguous: "需要明确校区", source_unavailable: "暂时无法取得正式来源", denied: "当前身份无权查看",
};
const statusTitle = computed(() => props.result.status === "no_current_records" && props.result.query.subject === "campuses"
  ? "当前查询范围内没有可提供的正式校区记录" : statuses[props.result.status]);
const coverages = { unknown: "来源覆盖未声明", partial: "来源声明部分覆盖", complete: "来源声明完整覆盖" };
const requestTarget = computed(() => props.result.query.scope === "all_authorized" ? "当前身份获准范围内的全部校区" :
  props.result.query.campus_name ? "校区名称：" + props.result.query.campus_name :
    props.result.query.campus_ref ? "已选择的校区" : "未指定校区");
const unit = computed(() => props.result.listing.entity === "campuses" ? "个校区" : "条");
const showListing = computed(() => !["denied", "source_unavailable", "display_limited"].includes(props.result.status));
</script>

<template>
  <section class="contact-result" aria-label="学校事实查询结果">
    <p class="result-status">{{ statusTitle }}</p>
    <p v-if="result.display_notice" class="display-notice">{{ result.display_notice }}</p>
    <p>查询对象：{{ result.query.subject === "campuses" ? "校区" : "官方联系" }} · {{ requestTarget }}</p>
    <template v-if="result.resolved_campuses.length">
      <p>{{ result.query.subject === "campuses" ? "本次返回的校区" : "实际查询校区" }}：</p>
      <ul aria-label="已解析校区">
        <li v-for="campus in result.resolved_campuses" :key="campus.reference">
          {{ campus.path || campus.label }}
          <button v-if="selectable && result.query.subject === 'campuses'" type="button" data-action="select-campus"
            :disabled="disabled" @click="$emit('select-campus', result, campus.reference)">查询此校区的官方联系</button>
        </li>
      </ul>
    </template>
    <p v-if="result.status === 'unmatched'">当前获准资料中没有匹配这个名称或选择的校区，不能据此认定该校区不存在。可核对名称或查看可查询校区。</p>
    <p v-if="result.status === 'not_recorded'">已核对上述校区，但当前没有可提供的正式联系记录，不能据此认定该联系渠道不存在。</p>
    <p v-if="result.status === 'no_current_records'">本次读取仅反映当前获准范围、发布状态和生效时间，不能据此认定{{ result.query.subject === "campuses" ? "现实中没有校区" : "该联系渠道不存在" }}。</p>
    <ul v-if="['ambiguous', 'selection_required'].includes(result.status)" aria-label="校区候选">
      <li v-for="candidate in result.candidates" :key="candidate.reference">
        {{ candidate.path || candidate.label }}
        <button v-if="selectable" type="button" data-action="select-campus" :disabled="disabled"
          @click="$emit('select-campus', result, candidate.reference)">查询此校区的官方联系</button>
      </li>
    </ul>
    <p v-if="['ambiguous', 'selection_required'].includes(result.status)">请根据完整位置选择校区，同名校区也可分别选择。</p>
    <p v-if="showListing" class="listing-count">
      本次显示 {{ result.listing.returned_count }} {{ unit }}<template v-if="result.listing.matched_count !== null">，共匹配 {{ result.listing.matched_count }} {{ unit }}</template><template v-else>；当前查询范围的匹配总数尚未核实</template>。
      <span v-if="result.listing.truncated">结果未全部显示，请缩小查询范围。</span>
    </p>
    <article v-for="item in result.items" :key="item.reference" class="contact-card">
      <h3>{{ result.resolved_campuses.find(campus => campus.reference === item.campus_ref)?.path || item.campus_name }} · {{ productContactKind(item.contact_kind) }}</h3>
      <p class="object-reference">联系引用：{{ item.reference }}</p>
      <p class="contact-value">{{ item.contact_value }}</p>
      <dl>
        <div><dt>服务时间</dt><dd>{{ item.service_hours ?? "尚未录入" }}</dd></div>
        <div><dt>来源</dt><dd>{{ item.source.label }}</dd></div>
        <div v-if="item.source.kind"><dt>来源通道</dt><dd>{{ productSourceKind(item.source.kind) }}</dd></div>
        <div v-if="item.source.published_at"><dt>发布时间</dt><dd>{{ item.source.published_at }}</dd></div>
        <div><dt>生效时间</dt><dd>{{ item.valid_from }}{{ item.valid_to ? " 至 " + item.valid_to : " 起" }}</dd></div>
        <div><dt>记录版本</dt><dd>版本 {{ item.version }}</dd></div>
      </dl>
      <button v-if="editable" type="button" data-action="edit-contact" :disabled="disabled" @click="$emit('edit', item.reference)">维护这条联系</button>
    </article>
    <p class="coverage">{{ coverages[result.coverage.status] }} · {{ result.coverage.description }}</p>
    <p class="query-time">查询时间：{{ result.queried_at }}</p>
  </section>
</template>

<style scoped>
.contact-result { display: grid; gap: 0.7rem; }
.result-status { margin: 0; font-weight: 700; }
.contact-card { padding: 1rem; border: 1px solid #d5e3e3; border-radius: 12px; background: #f7fbfb; }
h3 { margin: 0 0 .5rem; font-size: 1rem; }
.contact-value { margin: .5rem 0 1rem; font-size: 1.2rem; font-weight: 700; overflow-wrap: anywhere; }
dl { display: grid; gap: .4rem; margin: 0 0 1rem; font-size: .88rem; }
dl div { display: grid; grid-template-columns: 5em 1fr; gap: .5rem; }
dt { color: #546c71; } dd { margin: 0; overflow-wrap: anywhere; }
.object-reference { margin: .3rem 0; font-size: .8rem; color: #546c71; overflow-wrap: anywhere; }
.coverage, .query-time { margin: 0; font-size: .8rem; color: #546c71; overflow-wrap: anywhere; }
</style>
