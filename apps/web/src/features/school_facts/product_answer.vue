<script setup lang="ts">
import { computed } from "vue";
import TeachingResult from "./TeachingResult.vue";
import ResidenceResult from "./ResidenceResult.vue";
import {familySubjectLabel,validFamilyFactSubject,isResidenceResult,residenceNotices} from "./family_contracts";
import {isCorrectionHistory} from "./family_corrections";
import {isTeachingEvidence,teachingNotices,teachingQueryLabel} from "./teaching_contracts";
import FamilyEducationResult from "./FamilyEducationResult.vue";
import {isFamilyEducation} from "./family_education_contracts";
import FamilyProfileResult from "./FamilyProfileResult.vue";
import {isFamilyProfile, isStaffStudentProfile, familyFieldLabels, staffProfileStateLabel} from "./family_contracts";
import DirectoryResult from "./DirectoryResult.vue";
import DirectoryProposal from "./DirectoryProposal.vue";
import AnswerText from "./AnswerText.vue";
import { directoryPresentation } from "./directory_presentation";
import { isOwnReceipt, isOwnReceiptPage, isDirectoryAccess, accessStatusLabel, isDirectoryBundle, isDirectoryQuery, isDirectoryProposal, isDirectoryDescriptor, isDirectoryOriginalSegment, normalizeDirectoryFact, type DirectoryDescriptor, type DirectoryQuery } from "./directory_contracts";
import ProductContactResult from "./product_contact_result.vue";
import { isProductContact, isProductQuery, isProductReceipt, isProductProposal, productContactKind, productSourceKind, type ProductQuery } from "./product_contracts";

const props = withDefaults(defineProps<{ payload: unknown; selectable?: boolean; contactSelectable?: boolean; disabled?: boolean }>(), {contactSelectable: undefined});
defineEmits<{ identityInvalid: []; "select-campus": [result: ProductQuery, reference: string]; "review-directory": [reference: string]; "review-family": [reference: string]; "review-education": [reference: string]; "review-residence": [reference: string]; "related-directory": [result: DirectoryQuery, reference: string] }>();
const payload = computed(() => typeof props.payload === "object" && props.payload !== null && !Array.isArray(props.payload)
  ? props.payload as Record<string, unknown> : null);
const answer = computed(() => typeof props.payload === "string" ? props.payload :
  typeof payload.value?.text === "string" ? payload.value.text : "");
const historyNotice = computed(() => typeof payload.value?.history_notice === "string" && payload.value.history_notice.trim()
  ? payload.value.history_notice : null);
const facts = computed(() => Array.isArray(payload.value?.facts) ? payload.value.facts.map(normalizeDirectoryFact)
  .map(fact=>validFamilyFactSubject(fact)?fact:{kind:"invalid_subject"}) : []);
function subjectQueryLabel(value:unknown,fallback:string):string {
  const label=familySubjectLabel(value); return label?`${label} · ${fallback}`:fallback;
}
function isFieldEvidence(value: unknown): value is DirectoryDescriptor {
  return value !== null && typeof value === "object" && "kind" in value && value.kind === "fact_fields" && isDirectoryDescriptor(value);
}
const directoryView = computed(() => directoryPresentation(facts.value));
const selectionQueries = computed(() => directoryView.value.candidates);
const evidenceGroups = computed(() => directoryView.value.evidenceGroups);
const evidenceQueries = computed(() => evidenceGroups.value.flatMap(group => group.results));
const productReadQueries = computed(() => facts.value.filter((fact): fact is ProductQuery =>
  isProductQuery(fact) && !["ambiguous", "selection_required"].includes(fact.status)));
const productReadContacts = computed(() => facts.value.filter(isProductContact));
const accessEvidence = computed(() => facts.value.filter(isDirectoryAccess));
const fieldEvidence = computed(() => facts.value.filter(isFieldEvidence));
const originalEvidence = computed(() => facts.value.filter(isDirectoryOriginalSegment));
const familyEducationEvidence = computed(() => facts.value.filter(isFamilyEducation));
const correctionEvidence = computed(() => facts.value.filter(isCorrectionHistory));
const residenceEvidence = computed(() => facts.value.filter(isResidenceResult));
const teachingEvidence = computed(() => facts.value.filter(isTeachingEvidence));
const familyEvidence = computed(() => facts.value.filter(isFamilyProfile));
const staffProfiles = computed(() => facts.value.filter(isStaffStudentProfile));
function staffSources(sources:Record<string,unknown>|undefined) {
  const entries:Array<{label:string;value:string;reference?:string;publishedAt?:string}> = [];
  const add=(label:string,value:unknown) => {
    if (!value || typeof value!=="object" || !("label" in value) || typeof value.label!=="string") return;
    entries.push({label,value:value.label,reference:"reference" in value && typeof value.reference==="string"?value.reference:undefined,publishedAt:"published_at" in value && typeof value.published_at==="string"?value.published_at:undefined});
  };
  for(const [key,source] of Object.entries(sources ?? {})) {
    if (!familyFieldLabels[key]) continue;
    if (key==="campus" && source && typeof source==="object" && "relationship" in source && "location" in source) {
      add("校区关系来源",source.relationship); add("校区名称来源",source.location);
    } else add(`${familyFieldLabels[key]}来源`,source);
  }
  return entries;
}
function isEducationQuery(value:unknown): value is {family_owner:string;text:string;name?:string;status:string;evidence?:unknown[]} { return !!value && typeof value === "object" && "family_owner" in value && value.family_owner === "school_facts.education" && "text" in value && typeof value.text === "string" && "status" in value && typeof value.status === "string"; }
const educationEvidence = computed(() => facts.value.filter(isEducationQuery));
function educationSources(value: unknown): string[] { if (!Array.isArray(value)) return []; const refs:string[]=[]; for(const row of value) { if (!row || typeof row !== "object") continue; const source="source" in row?row.source:null; if(source && typeof source==="object" && "source_ref" in source && typeof source.source_ref==="string") refs.push(source.source_ref); } return [...new Set(refs)]; }
const receiptHistory = computed(() => facts.value.filter(isOwnReceiptPage));
const hasEvidence = computed(() => residenceEvidence.value.length > 0 || correctionEvidence.value.length > 0 || teachingEvidence.value.length > 0 || familyEducationEvidence.value.length > 0 || educationEvidence.value.length > 0 || staffProfiles.value.length > 0 || familyEvidence.value.length > 0 || evidenceGroups.value.length > 0 || productReadQueries.value.length > 0 || productReadContacts.value.length > 0 || accessEvidence.value.length > 0 || fieldEvidence.value.length > 0 || originalEvidence.value.length > 0);
const displayFacts = computed(() => facts.value.filter(fact =>
  !isResidenceResult(fact) && !isCorrectionHistory(fact) && !isTeachingEvidence(fact) && !isFamilyEducation(fact) && !isEducationQuery(fact) && !isStaffStudentProfile(fact) && !isFamilyProfile(fact) && !isDirectoryQuery(fact) && !isDirectoryBundle(fact) && !isProductContact(fact) &&
  !isDirectoryAccess(fact) && !isFieldEvidence(fact) && !isDirectoryOriginalSegment(fact) && !isOwnReceiptPage(fact) &&
  !(isProductQuery(fact) && !["ambiguous", "selection_required"].includes(fact.status))));
const hasReadResult = computed(() => teachingEvidence.value.some(item => item.status === "available")
  || staffProfiles.value.some(item => item.status === "known")
  || familyEvidence.value.some(item => item.status === "available")
  || familyEducationEvidence.value.some(item => item.status === "available")
  || evidenceQueries.value.some(item => item.records.length > 0)
  || productReadQueries.value.some(item => item.items.length > 0 || item.resolved_campuses.length > 0));
function scopedDenial(message:string) { return `${hasReadResult.value ? "部分查询未获准：" : "该项查询："}${message}`; }
// A fact card records one query, not the final task state. Its ordinary outcome
// remains in that card; later successes and answer wording never resolve risks.
const queryOutcomeStates = new Set(["ambiguous", "selection_required", "object_unresolved", "unmatched", "not_recorded", "no_current_records"]);
const evidenceNotices = computed(() => {
  // Mixed attempts do not prove that one result supersedes another. Preserve
  // each denial in its evidence and keep a neutral visible access reminder.
  const mixedResidenceAccess = residenceEvidence.value.some(item=>item.status==="denied") &&
    residenceEvidence.value.some(item=>["available","no_current_records"].includes(item.status));
  const notices: string[] = mixedResidenceAccess ? ["有住宿查询未获准，可展开依据查看对应资料。"] : [];
  notices.push(...residenceEvidence.value.flatMap((item,index)=>residenceNotices(item,
    subjectQueryLabel(item,`住宿查询${index+1}`), {includeDenied:!mixedResidenceAccess,concise:true})));
  notices.push(...teachingEvidence.value.flatMap((item,index) => teachingNotices(item,teachingQueryLabel(item,index),hasReadResult.value)
    .map(message=>familySubjectLabel(item)?`${familySubjectLabel(item)}：${message}`:message)));
  for (const item of staffProfiles.value) {
    if (item.status !== "known" && !queryOutcomeStates.has(item.status)) notices.push(["denied","forbidden"].includes(item.status) ? scopedDenial(`学生资料：${staffProfileStateLabel(item.status)}`) : staffProfileStateLabel(item.status));
    for (const [field, result] of Object.entries(item.profile ?? {})) {
      if (["denied","forbidden","conflict","ambiguous","source_unavailable","unavailable","not_returned"].includes(result.status)) notices.push(`${familyFieldLabels[field]}：${staffProfileStateLabel(result.status)}`);
    }
    for (const issue of item.limitations) {
      if (!queryOutcomeStates.has(issue.status)) notices.push(["denied","forbidden"].includes(issue.status) ? scopedDenial(`学生资料：${staffProfileStateLabel(issue.status)}`) : staffProfileStateLabel(issue.status));
      if (issue.truncated) notices.push("当前候选未完整返回，需要进一步缩小范围");
    }
  }
  for(const item of familyEvidence.value){
    if(item.status === "denied")notices.push(scopedDenial(subjectQueryLabel(item,"当前身份无权查看这项孩子资料")));
    if(item.status === "source_unavailable")notices.push(subjectQueryLabel(item,"孩子资料暂时无法读取，请稍后重试"));
    if(item.status === "conflict")notices.push(subjectQueryLabel(item,"孩子正式资料存在不一致，请联系学校核验"));
  }
  for (const fact of facts.value) {
    if (!isDirectoryBundle(fact)) continue;
    const subject = fact.subject_label ? `${fact.subject_label}：` : "";
    if (fact.status === "partial") notices.push(`${subject}只取得部分正式依据`);
    if (["denied", "forbidden"].includes(fact.status)) notices.push(scopedDenial(`${subject}当前身份无权查询此范围`));
    if (["source_unavailable", "error", "unavailable"].includes(fact.status)) notices.push(`${subject}正式资料来源暂时不可用，请稍后重试`);
    if (fact.status === "unsupported_selection") notices.push(`${subject}目前不能按这个条件查询，请换个条件`);
    for (const item of fact.results) {
      if (["denied", "forbidden"].includes(item.status)) notices.push(scopedDenial("当前身份无权查询此范围"));
      if (["source_unavailable", "error", "unavailable"].includes(item.status)) notices.push("正式资料来源暂时不可用，请稍后重试");
      if (item.status === "unsupported_selection") notices.push("目前不能按这个条件查询，请换个条件");
      if (item.status === "partial") notices.push("本次查询尚未完成，请结合已取得的资料使用");
    }
  }
  for (const result of evidenceQueries.value) {
    if (["denied", "forbidden"].includes(result.status)) notices.push(scopedDenial("当前身份无权查询此范围"));
    if (result.status === "conflict") notices.push("正式资料存在不一致，需核验");
    if (["source_unavailable", "error", "unavailable"].includes(result.status)) notices.push("正式资料来源暂时不可用，请稍后重试");
    if (result.status === "unsupported_selection") notices.push("目前不能按这个条件查询，请换个条件");
    if (result.status === "partial") notices.push("本次查询尚未完成，请结合已取得的资料使用");
    if (result.truncated) notices.push("查询结果未全部显示，请缩小查询范围");
    if (["known", "no_current_records", "not_recorded"].includes(result.status) &&
      (result.coverage.status === "partial" || result.quality?.coverage === "partial"))
      notices.push("这次只查到部分资料，不能据此判断全部情况");
    if (result.coverage.status === "conflicting_statements" || result.quality?.coverage === "conflicting_statements")
      notices.push("这次查到的资料范围说法不一致，需核实后使用");
    if (["ambiguous", "selection_required"].includes(result.status)) {
      if (result.coverage.status === "partial" || result.quality?.coverage === "partial") notices.push("这次只查到部分候选资料，不能据此判断全部情况");
    }
    if (result.quality?.evidence_truncated) notices.push("本次没有查全核验记录，不能据此断定资料完整");
    if (result.quality?.source_availability === "unavailable") notices.push("这项资料的原来源目前不可用；已查到的正式记录仍可查看，来源状态需核实");
    if (result.quality?.evidence_status === "source_unavailable") notices.push("本次无法核对资料依据，请稍后再查");
    if (["denied", "forbidden"].includes(result.quality?.evidence_status ?? "")) notices.push("这项资料的核验依据不在你的可查看范围");
    if (["conflicting_statements", "contradictory_evidence"].includes(result.quality?.conflict ?? "")) notices.push("这项资料有相互矛盾的记录，请核实后使用");
    if (result.quality?.conflict === "open") notices.push("这项资料有待处理的争议，请核实后使用");
    if (result.quality?.freshness === "review_overdue") notices.push("这项资料已过登记的复核日期，使用前请再核实");
  }
  for (const result of accessEvidence.value) {
    if (result.requested_fact_kind === null) {
      if (result.families.some(row => row.status === "source_unavailable")) notices.push("部分读取权限暂时无法核验，可展开查看详情。");
      continue;
    }
    for (const row of result.families) {
      if (["denied", "source_unavailable"].includes(row.status)) notices.push(`${row.label}：${accessStatusLabel(row.status, result.purpose)}`);
    }
  }
  if (originalEvidence.value.some(result => !result.complete)) notices.push("已取得原文片段，尚非完整原文；可展开依据核对。");
  for (const result of productReadQueries.value) {
    if (result.status === "denied") notices.push(scopedDenial("当前身份无权查看这项正式资料"));
    if (result.status === "source_unavailable") notices.push("正式资料来源暂时不可用，请稍后重试");
    if (result.status === "display_limited") notices.push(result.display_notice ?? "本次结果无法完整展示，请缩小查询范围");
    if (result.listing.truncated) notices.push("查询结果未全部显示，请缩小查询范围");
  }
  return [...new Set(notices)];
});
function isResidenceOwned(value:unknown): boolean { return value !== null && typeof value === "object" && "family_owner" in value && value.family_owner === "school_facts.residence"; }
function isEducationOwned(value:unknown): boolean { return value !== null && typeof value === "object" && "family_owner" in value && value.family_owner === "school_facts.education"; }
function isFamilyOwned(value:unknown): boolean { return value !== null && typeof value === "object" && "family_owner" in value && value.family_owner === "school_facts.family_staff"; }
function isDisplayReceipt(value: unknown): value is import("./product_contracts").ProductReceipt {
  return isProductReceipt(value) && (!("kind" in value) || value.kind !== "own_fact_receipt" || isOwnReceipt(value));
}
const receipt = computed(() => isDisplayReceipt(props.payload) ? props.payload : null);
</script>

<template>
  <div class="product-answer">
    <AnswerText v-if="answer" :text="answer" />
    <section v-if="evidenceNotices.length" class="system-notices" aria-label="系统提示">
      <p class="system-notice-label">系统提示</p>
      <p v-for="notice in evidenceNotices" :key="notice" class="important-notice" role="alert">{{ notice }}</p>
    </section>
    <details v-if="historyNotice" class="conversation-explanation">
      <summary>对话说明</summary>
      <p class="history-notice">{{ historyNotice }}</p>
    </details>
    <details v-if="hasEvidence" class="evidence">
      <summary>查看依据</summary>
      <section v-for="(correctionPage,index) in correctionEvidence" :key="'corrections-'+index" class="fact-card" aria-label="本人报错处理依据">
        <h3>本人报错处理记录</h3><p>{{correctionPage.scope}}</p>
        <article v-for="report in correctionPage.items" :key="report.report_id">
          <p>{{report.target_label}} · {{report.field_label}}：{{report.status_label}}</p>
          <p>{{report.status_meaning}}</p><p>提交：{{report.submitted_at}}<span v-if="report.resolved_at"> · 处理：{{report.resolved_at}}</span></p>
          <p v-if="report.review_explanation">核实说明：{{report.review_explanation}}</p>
          <p>报告定位：{{report.report_id}}</p>
        </article><p>{{correctionPage.meaning}}</p>
      </section>
      <ResidenceResult v-for="(residence,index) in residenceEvidence" :key="'residence-'+index" :result="residence" :query-label="subjectQueryLabel(residence,`住宿查询${index+1}`)" />
      <TeachingResult v-for="(teaching,index) in teachingEvidence" :key="'teaching-'+index" :result="teaching" :query-label="subjectQueryLabel(teaching,teachingQueryLabel(teaching,index))" />
      <section v-for="(education,index) in educationEvidence" :key="'education-'+index" aria-label="班级与教育资料依据" class="fact-card"><h3>{{education.name ?? '教育资料'}}</h3><p>{{education.text}}</p><p>查询结果仅代表本次获准读取的正式登记；未登记不等于确定空缺或名单完整。</p><p v-for="source in educationSources(education.evidence)" :key="source">来源：{{source}}</p></section>
      <section v-for="(family,index) in familyEducationEvidence" :key="'family-education-'+index"><p v-if="familySubjectLabel(family)">{{familySubjectLabel(family)}}</p><FamilyEducationResult :result="family" /></section>
      <section v-for="(family,index) in familyEvidence" :key="'family-'+index"><p v-if="familySubjectLabel(family)">{{familySubjectLabel(family)}}</p><FamilyProfileResult :result="family" /></section>
      <section v-for="(profile,index) in staffProfiles" :key="'staff-profile-'+index" aria-label="学生资料依据" class="fact-card">
        <h3>学生资料</h3>
        <p v-if="profile.status !== 'known'" data-role="query-outcome">该次查询：{{ staffProfileStateLabel(profile.status) }}</p>
        <p v-for="(field,key) in profile.profile" :key="key">{{ familyFieldLabels[key] }}：{{ field.status === 'known' ? field.value : staffProfileStateLabel(field.status) }}</p>
        <p v-for="(candidate,candidateIndex) in profile.candidates" :key="candidateIndex">{{ candidate.name }}<span v-if="candidate.student_number"> · 学号 {{ candidate.student_number }}</span></p>
        <p v-for="source in staffSources(profile.sources)" :key="source.label">{{ source.label }}：{{ source.value }}</p>
        <p>{{ profile.coverage.description }}</p>
        <p v-for="(issue,issueIndex) in profile.limitations" :key="issueIndex" data-role="query-limitation">该次查询的限制：{{ staffProfileStateLabel(issue.status) }}<span v-if="issue.truncated">；候选未完整返回</span></p>
        <details><summary>维护核查信息</summary>
          <p v-for="source in staffSources(profile.sources)" :key="source.label">{{ source.label }}<span v-if="source.reference"> · 来源引用 {{ source.reference }}</span><span v-if="source.publishedAt"> · 发布时间 {{ source.publishedAt }}</span></p>
          <DirectoryResult v-for="(query,queryIndex) in profile.evidence.filter(isDirectoryQuery)" :key="queryIndex" :result="query" :disabled="disabled" evidence-only @identity-invalid="$emit('identityInvalid')" />
        </details>
      </section>
      <section v-for="(group, groupIndex) in evidenceGroups" :key="group.label + groupIndex" class="evidence-group">
        <h3>{{ group.label }}</h3>
        <DirectoryResult @identity-invalid="$emit('identityInvalid')" v-for="(result, resultIndex) in group.results" :key="result.fact_kind + result.queried_at + resultIndex" :result="result" :disabled="disabled" evidence-only />
      </section>
      <ProductContactResult v-for="(result, resultIndex) in productReadQueries" :key="'contact-query-' + result.queried_at + resultIndex"
        :result="result" :selectable="contactSelectable ?? selectable" :disabled="disabled"
        @select-campus="(selected, reference) => $emit('select-campus', selected, reference)" />
      <article v-for="fact in productReadContacts" :key="fact.reference" class="fact-card">
        <strong>{{ fact.campus_name }} · {{ productContactKind(fact.contact_kind) }}</strong>
        <p class="object-reference">联系引用：{{ fact.reference }}</p>
        <p>{{ fact.contact_value }}</p>
        <p>服务时间：{{ fact.service_hours ?? "尚未录入" }}</p>
        <p>来源：{{ fact.source.label }} · 版本 {{ fact.version }}</p>
        <p v-if="fact.source.kind">来源通道：{{ productSourceKind(fact.source.kind) }}</p>
        <p>生效时间：{{ fact.valid_from }}{{ fact.valid_to ? " 至 " + fact.valid_to : " 起" }}</p>
      </article>
      <section v-for="(fact, index) in accessEvidence" :key="'access-' + index" class="fact-card" :aria-label="fact.purpose === 'maintain' ? '当前身份维护资格' : '当前身份读取授权范围'">
        <h3>{{ fact.purpose === 'maintain' ? '当前身份维护资格' : '当前身份读取授权范围' }}</h3>
        <p>{{ fact.notice }}</p>
        <ul><li v-for="row in fact.families" :key="row.fact_kind">{{ row.label }}：{{ accessStatusLabel(row.status, fact.purpose) }}<span v-if="row.status === 'authorized_scope'">；授权目标 {{ row.authorized_target_count }} 个（不是资料条数）</span></li></ul>
        <p>核验时间：{{ fact.queried_at }}</p>
      </section>
      <section v-for="(fact, index) in fieldEvidence" :key="'fields-' + index" class="fact-card" aria-label="正式字段说明">
        <h3>{{ fact.label }} · 正式字段说明</h3>
        <p v-if="fact.query_support">{{ fact.query_support.guidance }}</p>
        <ul><li v-for="field in fact.fields" :key="field.name">{{ field.label }}（{{ field.required ? "必填" : "选填" }}）<span v-if="field.options.length">：{{ field.options.map(option => option.label).join("、") }}</span></li></ul>
      </section>
      <section v-for="(fact, index) in originalEvidence" :key="'original-' + index" class="fact-card" aria-label="正式原文依据">
        <h3>{{ fact.complete ? "完整制度原文" : "制度原文片段" }} · 版本 {{ fact.version }}</h3>
        <p>{{ fact.notice }} 原文共 {{ fact.length }} 字。</p>
        <p v-if="!fact.complete && fact.text">本段为原文第 {{ fact.offset + 1 }} 至 {{ fact.end }} 字{{ fact.has_more ? "，后续仍有原文" : "" }}；不代替完整原文。</p>
        <p v-if="!fact.complete && !fact.text">本次原文片段没有文字；不代替完整原文。</p>
        <p class="original-text">{{ fact.text }}</p>
        <p>来源：{{ fact.source.label }} · 发布时间：{{ fact.source.published_at ?? "未提供" }}</p>
        <p class="object-reference">制度版本引用：{{ fact.reference }}<span v-if="fact.source.reference"> · 来源引用：{{ fact.source.reference }}</span></p>
      </section>
    </details>
    <section v-for="(entry, index) in selectionQueries" :key="'selection-' + index" class="candidate-entry" aria-label="候选对象入口">
      <p>{{ entry.pending ? "查询对象尚待定位，可选择候选对象继续查询。" : "可按需选择候选对象继续查询。" }}</p>
      <ul><li v-for="candidate in entry.candidates" :key="candidate.reference">
        <button v-if="selectable" type="button" :disabled="disabled" @click="$emit('related-directory', entry.result, candidate.reference)">{{ candidate.label }}</button>
        <span v-else>{{ candidate.label }}</span>
      </li></ul>
      <p v-if="!entry.candidates.length">本次没有可展示的候选对象，请结合查询依据核对条件。</p>
    </section>
    <template v-for="(fact, index) in displayFacts" :key="index">
      <DirectoryProposal v-if="isDirectoryProposal(fact)" :proposal="fact" :disabled="disabled" @review="isResidenceOwned(fact) ? $emit('review-residence', $event) : isEducationOwned(fact) ? $emit('review-education', $event) : isFamilyOwned(fact) ? $emit('review-family', $event) : $emit('review-directory', $event)" />
      <ProductContactResult v-else-if="isProductQuery(fact)" :result="fact" :selectable="contactSelectable ?? selectable" :disabled="disabled"
        @select-campus="(result, reference) => $emit('select-campus', result, reference)" />
      <section v-else-if="isProductProposal(fact)" class="fact-card" aria-label="待确认修改">
        <h3>请核对本次修改</h3>
        <p>{{ fact.summary.before.campus_name }} · {{ productContactKind(fact.summary.before.contact_kind) }}</p>
        <details><summary>查看对象引用</summary><p class="object-reference">联系引用：{{ fact.summary.before.reference }}</p></details>
        <p>原联系内容：{{ fact.summary.before.contact_value }}</p>
        <p>新联系内容：{{ fact.summary.after.contact_value }}</p>
        <p>原服务时间：{{ fact.summary.before.service_hours ?? "尚未录入" }}</p>
        <p>新服务时间：{{ fact.summary.after.service_hours ?? "未填写" }}</p>
        <p>修改原因：{{ fact.summary.reason }}</p>
        <p>确认有效至 {{ fact.expires_at }}</p>
        <p>此处为待确认摘要，尚未执行；请使用本条回复的正式确认控件。</p>
      </section>
      <section v-else-if="isDisplayReceipt(fact)" class="fact-card" aria-label="正式操作回执">
        <h3>{{ fact.status === "succeeded" ? "已完成" : fact.status === "rejected" ? "未执行" : "结果待核对" }}</h3>
        <p>{{ isOwnReceipt(fact) ? "这是之前那次修改的结果，没有再次修改。" : fact.message }}</p>
        <p v-if="fact.changed_count !== null">实际变更 {{ fact.changed_count }} 项</p>
        <details><summary>查看回执追踪信息</summary>
          <template v-if="isOwnReceipt(fact)"><p>{{ fact.message }}</p><p>原确认时间：{{ fact.confirmed_at }}；{{ fact.targets.map(target => target.label).join("、") }}。当前内容需另行查询。</p></template>
          <p v-if="fact.target_version !== null">学校事实总版本 {{ fact.target_version }}</p>
          <p v-if="fact.request_id">请求编号：{{ fact.request_id }}</p><p>回执引用：{{ fact.reference }}</p>
        </details>
      </section>
      <p v-else class="unavailable-card">这条资料暂时无法展示，请重新查询。</p>
    </template>
    <details v-if="receiptHistory.length" class="evidence" data-role="receipt-history-evidence">
      <summary>维护核查信息：本人历史回执</summary>
      <section v-for="(page, index) in receiptHistory" :key="index" class="fact-card" aria-label="本人历史正式回执">
        <h3>本人历史正式回执</h3>
        <p>{{ page.notice }}</p>
        <p v-if="!page.items.length">当前范围内没有可提供的本人已完成回执，不能据此断定没有历史操作。</p>
        <article v-for="item in page.items" :key="item.reference">
          <p>{{ item.targets.map(target => target.label).join("、") }} · 原确认时间 {{ item.confirmed_at }}</p>
          <p>{{ item.message }}</p>
          <p>原修改共 {{ item.changed_count }} 项；当时学校事实总版本 {{ item.target_version }}。当前内容需另行查询。</p>
          <details><summary>查看回执依据</summary><p>只读回执引用：{{ item.reference }}</p><p v-for="target in item.targets" :key="target.reference">{{ target.label }}：{{ target.reference }}</p></details>
        </article>
        <p v-if="page.has_more">还有更早的可读回执，本次尚未完整列出。</p>
      </section>
    </details>
    <section v-if="receipt" aria-label="正式操作回执">
      <strong>{{ receipt.status === "succeeded" ? "已完成" : receipt.status === "rejected" ? "未执行" : "结果待核对" }}</strong>
      <p>{{ isOwnReceipt(receipt) ? "这是之前那次修改的结果，没有再次修改。" : receipt.message }}</p>
      <p v-if="receipt.changed_count !== null">实际变更 {{ receipt.changed_count }} 项</p>
      <details><summary>查看回执追踪信息</summary>
        <template v-if="isOwnReceipt(receipt)"><p>{{ receipt.message }}</p><p>原确认时间：{{ receipt.confirmed_at }}；{{ receipt.targets.map(target => target.label).join("、") }}</p></template>
        <p v-if="receipt.target_version !== null">学校事实总版本 {{ receipt.target_version }}</p>
        <p v-if="receipt.request_id">请求编号：{{ receipt.request_id }}</p><p>回执引用：{{ receipt.reference }}</p>
      </details>
    </section>
    <p v-if="!answer && facts.length === 0 && !receipt">这条回复暂时无法展示，请刷新后重试。</p>
  </div>
</template>

<style scoped>
.product-answer { display: grid; gap: .8rem; }
.candidate-entry { padding: .7rem 1rem; border: 1px solid #d5e3e3; border-radius: 12px; }
.candidate-entry p { margin: .2rem 0; }
.candidate-entry ul { display: flex; flex-wrap: wrap; gap: .5rem; padding: 0; list-style: none; }
.candidate-entry button { padding: .45rem .65rem; max-width: 100%; overflow-wrap: anywhere; }
.original-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.fact-card { padding: 1rem; border: 1px solid #d5e3e3; border-radius: 12px; }
.object-reference { font-size: .8rem; color: #546c71; overflow-wrap: anywhere; }
.unavailable-card { color: #596b74; }
.system-notices { display: grid; gap: .5rem; }
.system-notice-label { margin: 0; font-weight: 650; }
.important-notice { margin: 0; padding: .75rem 1rem; border-left: 4px solid #a35b00; background: #fff6e8; color: #663800; }
.evidence, .conversation-explanation { border: 1px solid #d5e3e3; border-radius: 12px; padding: .7rem 1rem; }
.evidence > summary, .conversation-explanation > summary { cursor: pointer; font-weight: 650; }
.evidence-group > h3 { margin-bottom: .3rem; }
</style>
