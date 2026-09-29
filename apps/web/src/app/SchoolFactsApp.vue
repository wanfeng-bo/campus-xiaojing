<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";
import SchoolFactsHome from "./SchoolFactsHome.vue";
import SchoolFactIcon from "./SchoolFactIcon.vue";
import XiaojingBrand from "./XiaojingBrand.vue";
import AssistantConversation from "../features/assistant/AssistantConversation.vue";
import FamilyProfilePanel from "../features/school_facts/FamilyProfilePanel.vue";
import {createDirectoryApi} from "../features/school_facts/directory_api";
import DirectoryWorkbench from "../features/school_facts/DirectoryWorkbench.vue";
import SchoolFactPackageImport from "../features/school_facts/SchoolFactPackageImport.vue";
import ProductAnswer from "../features/school_facts/product_answer.vue";
import ProductContactResult from "../features/school_facts/product_contact_result.vue";
import { createProductSessionController, type ProductState } from "../features/school_facts/product_session";
import type { ProductDraft, ProductQuery } from "../features/school_facts/product_contracts";
import { hasFamilyConversation, isProductFeatures } from "../features/school_facts/product_contracts";
import type { DirectoryQuery } from "../features/school_facts/directory_contracts";

const props = withDefaults(defineProps<{ formal?: boolean }>(), { formal: false });
const emit = defineEmits<{ reauthenticate: [] }>();
const controller = createProductSessionController();
const state = shallowRef<Readonly<ProductState>>(controller.getState());
const account = ref("");
const assistantConversation = ref<InstanceType<typeof AssistantConversation> | null>(null);
const workCenter = ref<HTMLDialogElement | null>(null);
const workCenterOpen = ref(false);
const conversationHasActivity = ref(false);
let drawerReturnFocus: HTMLElement | null = null;
const directoryWorkbench = ref<InstanceType<typeof DirectoryWorkbench> | null>(null);
const educationWorkbench = ref<InstanceType<typeof DirectoryWorkbench> | null>(null);
const educationApi = createDirectoryApi("education");
const residenceWorkbench = ref<InstanceType<typeof DirectoryWorkbench> | null>(null);
const residenceApi = createDirectoryApi("residence");
const residenceEnabled = ref(false);
const packagesEnabled = ref(false);
const assistantFeatureEnabled = ref<boolean | null>(null);
const featuresLoading = ref(true);
const assistantEnabled = computed(() => !props.formal || assistantFeatureEnabled.value === true);
const packageFamilies = computed<import("../features/school_facts/package_api").PackageFamily[]>(()=>[
 ...(workspaceSession.value?.directory_enabled?["directory" as const]:[]),
 ...(workspaceSession.value?.family_enabled?["family" as const]:[]),
 ...(workspaceSession.value?.education_enabled?["education" as const]:[]),
 ...(residenceEnabled.value?["residence" as const]:[]),
]);
async function loadFeatures() {
  try {
    const response = await fetch("/api/v1/school-fact-product/features", {credentials: "same-origin"});
    if (!response.ok) return;
    const value: unknown = await response.json();
    if (!isProductFeatures(value)) return;
    residenceEnabled.value = value.f6_features_enabled === true;
    packagesEnabled.value = value.f7_packages_enabled === true;
    assistantFeatureEnabled.value = value.assistant_enabled ?? null;
  } catch { residenceEnabled.value = false; }
  finally { featuresLoading.value = false; }
}
const familyWorkbench = ref<InstanceType<typeof DirectoryWorkbench> | null>(null);
const maintenanceArea = ref<HTMLDetailsElement | null>(null);
const familyStaffApi = createDirectoryApi("family");
const campus = ref("");
const retainedSession = shallowRef<ProductState["session"]>(null);
const workspaceSession = computed(() => state.value.authentication_required ? retainedSession.value : state.value.session);
const unsubscribe = controller.subscribe((value) => {
  state.value = value;
  if (!value.authentication_required) retainedSession.value = value.session;
  if (!value.session?.accounts.some((item) => item.ref === account.value)) {
    account.value = value.session?.accounts[0]?.ref ?? "";
  }
});
const parentContext = computed(() => hasFamilyConversation(workspaceSession.value) || workspaceSession.value?.selected_context_kind === "guardian_relationship");
const active = computed(() => state.value.authentication_required ? !parentContext.value && !workspaceSession.value?.family_enabled :
  state.value.session?.status === "signed_in" && (state.value.session.selected_context_ref !== null || hasFamilyConversation(state.value.session)));
const assistantKey = computed(() => workspaceSession.value?.assistant_context_key ?? `legacy-context-${state.value.context_epoch}`);
const locked = computed(() => state.value.authentication_required || state.value.busy !== null || state.value.receipt?.status === "unknown");
const canPrepare = computed(() => !locked.value && state.value.draft.contact_value.trim() && state.value.draft.reason.trim());
const activeIdentityLabel = computed(() => workspaceSession.value?.contexts.find(item => item.ref === workspaceSession.value?.selected_context_ref)?.label
  ?? (parentContext.value ? "家长身份" : "身份与账户"));
const shortcuts = computed(() => {
  if (!active.value) return [];
  if (parentContext.value) return [{key:"family",title:"孩子资料",description:"班级、老师与学校资料",icon:"person"}];
  return [
    ...(workspaceSession.value?.directory_enabled ? [{key:"directory",title:"学校资料",description:"人员、组织与联系方式",icon:"book"}] : [{key:"contact",title:"官方联系",description:"校区与公开联系渠道",icon:"book"}]),
    ...(workspaceSession.value?.family_enabled ? [{key:"family",title:"学生与家校",description:"学生资料与家校关系",icon:"person"}] : []),
    ...(workspaceSession.value?.education_enabled ? [{key:"education",title:"班级与教学",description:"班级、课程与任课老师",icon:"people"}] : []),
    ...(residenceEnabled.value ? [{key:"residence",title:"住宿资料",description:"宿舍与住宿安排",icon:"home"}] : []),
  ];
});
async function openWorkCenter(area?: string) {
  if (!active.value || !workCenter.value) return;
  if (!workCenterOpen.value) drawerReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  workCenterOpen.value = true;
  if (!workCenter.value.open) {
    if (typeof workCenter.value.showModal === "function") workCenter.value.showModal();
    else workCenter.value.setAttribute("open", "");
  }
  if (maintenanceArea.value) maintenanceArea.value.open = true;
  await nextTick();
  workCenter.value?.querySelector<HTMLButtonElement>('[data-action="close-work-center"]')?.focus();
  if (area) workCenter.value?.querySelector(`[data-work-area="${area}"]`)?.scrollIntoView?.({block:"start"});
}
async function closeWorkCenter() {
  workCenterOpen.value = false;
  if (workCenter.value?.open) {
    if (typeof workCenter.value.close === "function") workCenter.value.close();
    else workCenter.value.removeAttribute("open");
  }
  await nextTick();
  if (drawerReturnFocus?.isConnected) drawerReturnFocus.focus();
  drawerReturnFocus = null;
}
function workCenterKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") { event.preventDefault(); void closeWorkCenter(); return; }
  if (event.key !== "Tab" || !workCenter.value) return;
  const controls = [...workCenter.value.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex="0"]')]
    .filter(element => {
      if (element.matches('[type="hidden"], :disabled') || element.closest('[hidden]')) return false;
      for (let ancestor = element.parentElement; ancestor && ancestor !== workCenter.value; ancestor = ancestor.parentElement) {
        if (ancestor.matches('details:not([open])') && element !== ancestor.querySelector(':scope > summary')) return false;
      }
      return true;
    });
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
watch(active, value => { if (!value) void closeWorkCenter(); });
function updateDraft(key: keyof ProductDraft, event: Event) {
  controller.setDraft({ [key]: (event.target as HTMLInputElement).value });
}
function contextChanged(event: Event) {
  void controller.changeContext((event.target as HTMLSelectElement).value);
}
function selectCampus(result: ProductQuery, reference: string) {
  void openWorkCenter("contact");
  return controller.selectCampus(result, reference);
}
function reviewFact(reference: string, family: boolean | "education" | "residence" = false) {
  void openWorkCenter();
  if (maintenanceArea.value) maintenanceArea.value.open = true;
  void (family === "residence" ? residenceWorkbench.value : family === "education" ? educationWorkbench.value : family ? familyWorkbench.value : directoryWorkbench.value)?.reviewProposal(reference);
}
function selectRelated(result: DirectoryQuery, reference: string) {
  void openWorkCenter();
  if (maintenanceArea.value) maintenanceArea.value.open = true;
  const family = "family_owner" in result && result.family_owner === "school_facts.family_staff";
  ("family_owner" in result && result.family_owner === "school_facts.residence" ? residenceWorkbench.value : "family_owner" in result && result.family_owner === "school_facts.education" ? educationWorkbench.value : family ? familyWorkbench.value : directoryWorkbench.value)?.selectRelated(result, reference);
}
onMounted(() => { void controller.open(); void loadFeatures(); });
onUnmounted(() => { unsubscribe(); controller.dispose(); });
</script>

<template>
  <main class="school-facts-app">
    <header class="product-header" :inert="workCenterOpen || undefined">
      <div class="header-leading">
        <button v-if="active && assistantEnabled" class="icon-button" type="button" data-action="open-conversations" aria-label="查找当前对话" @click="assistantConversation?.openConversations()"><SchoolFactIcon name="menu" /></button>
        <XiaojingBrand v-else />
      </div>
      <span class="product-name">小静</span>
      <nav class="header-actions" aria-label="工作区操作">
        <button v-if="active" type="button" class="icon-button work-button" data-action="open-work-center" aria-haspopup="dialog" :aria-expanded="workCenterOpen" aria-label="打开工作中心" @click="openWorkCenter()"><SchoolFactIcon name="work"/><span>工作中心</span></button>
        <button v-if="active && assistantEnabled" type="button" class="icon-button new-button" data-action="shell-new-conversation" aria-label="新对话" @click="assistantConversation?.newConversation()"><SchoolFactIcon name="plus" /></button>
      </nav>
    </header>

    <details class="identity-panel" :open="!active || state.authentication_required" :inert="workCenterOpen || undefined" :aria-label="props.formal ? '当前身份' : '工程身份'">
      <summary><span class="identity-dot"></span><span>{{ activeIdentityLabel }}</span><span class="identity-caption">{{ state.authentication_required ? '需要重新核对' : '身份与使用说明' }}</span><span v-if="!props.formal" class="engineering-badge">合成学校工程身份</span></summary>
      <div class="identity-content">
      <p v-if="state.busy === 'session'" role="status">正在核对身份……</p>
      <p v-if="state.authentication_required" role="alert">{{workspaceSession?.family_enabled ? "当前身份已失效，已关闭原资料视图；请重新核对身份并选择孩子或任职。" : "当前履职身份已失效，旧消息与草稿仅供查看和复制；请重新核对身份并显式选择。"}}</p>
      <button v-if="state.authentication_required" type="button" data-action="recheck-identity" :disabled="state.busy === 'session'" @click="props.formal ? emit('reauthenticate') : controller.recheckIdentity()">{{ props.formal ? "从钉钉重新进入" : "重新核对工程身份" }}</button>
      <template v-if="state.busy !== 'session' && state.session?.status === 'signed_out'">
        <template v-if="!props.formal">
        <label for="engineering-account">选择工程账号</label>
        <select id="engineering-account" v-model="account">
          <option v-for="item in state.session.accounts" :key="item.ref" :value="item.ref">{{ item.label }}</option>
        </select>
        <button type="button" data-action="login" :disabled="!account" @click="controller.login(account)">进入合成学校</button>
        </template>
        <button v-else type="button" @click="emit('reauthenticate')">从钉钉重新进入</button>
      </template>
      <template v-else-if="state.busy !== 'session' && state.session?.status === 'signed_in'">
        <p v-if="props.formal && state.session.contexts.length === 0" role="status">当前身份尚未完成绑定或暂无有效任职／孩子关系，请联系学校核实。</p>
        <label v-if="state.session.contexts.length > 0" for="engineering-context">{{ state.authentication_required ? "重新选择身份" : parentContext ? "资料卡查看孩子" : "当前身份" }}</label>
        <select v-if="state.session.contexts.length > 0" :disabled="state.authentication_required && !state.identity_rechecked" id="engineering-context" :value="state.authentication_required ? '' : state.session.selected_context_ref ?? ''" @change="contextChanged">
          <option v-if="state.authentication_required || state.session.selected_context_ref === null" value="" disabled>{{parentContext ? '选择孩子查看资料卡' : '请选择任职或孩子'}}</option>
          <option v-for="item in state.session.contexts" :key="item.ref" :value="item.ref">{{ item.label }}</option>
        </select>
        <button type="button" data-action="logout" @click="controller.logout">{{ props.formal ? "退出" : "退出工程身份" }}</button>
        <p v-if="assistantEnabled && parentContext && !state.authentication_required" class="identity-note">可以直接向小静询问任一已关联孩子；此处选择只切换资料卡，不限制对话可查询的孩子。</p>
        <p v-if="!state.authentication_required" class="identity-note">{{ state.session.directory_enabled ? "查看与维护按当前事实对象分别核验。" : state.session.can_maintain ? "当前可维护获准范围内的官方联系。" : "当前仅可查询获准信息。" }}</p>
      </template>
      <button v-else-if="state.busy !== 'session' && !state.authentication_required" type="button" @click="controller.open">重新连接身份</button>
      <p v-if="!props.formal" class="identity-note">用于验证本产品的工程流程，账号由受控工程环境提供。仅使用获准合成资料，请勿输入真实学校、学生或家庭资料。</p>
      <details v-else-if="assistantEnabled" class="identity-note">
        <summary>使用小静前，请了解资料使用方式</summary>
        <p>本次提问和必要的获准学校资料会发送给DeepSeek，用于理解问题和生成回答。请勿输入证件号码、私人电话、详细住址、密码或家庭敏感情况；需要查看的获准明细请使用页面资料卡。</p>
        <p>本产品的聊天上下文最多在内存中保留24小时，重启可能提前丢失；正式资料、操作记录和回执继续保留。这不代表模型服务商不留存资料，具体处理方式以学校向您提供的告知为准。</p>
      </details>
      <p v-else-if="assistantFeatureEnabled === false" class="identity-note" role="status">小静尚未启用，可继续查看获准资料并办理有权限的维护。</p>
      <p v-else-if="featuresLoading" class="identity-note" role="status">正在核对小静是否可用……</p>
      <p v-else class="identity-note" role="status">暂时无法确认小静是否可用，请刷新后重试。</p>
      </div>
    </details>

    <p v-if="state.error_message && !workCenterOpen" class="error" role="alert">{{ state.error_message }}</p>
    <SchoolFactsHome v-if="!active" :shortcuts="[]" :assistant-enabled="false" inactive />
    <div v-if="active" class="workspace" :class="{ 'without-assistant': !assistantEnabled, 'with-conversation': conversationHasActivity }" :inert="workCenterOpen || undefined">
      <section v-if="assistantEnabled" class="panel assistant-panel" aria-label="小静工作区">
        <AssistantConversation :key="assistantKey" ref="assistantConversation" :identity-invalid="state.authentication_required" :context-claim="hasFamilyConversation(workspaceSession) ? undefined : workspaceSession?.selection_transition_ref ?? undefined" @identity-invalid="controller.invalidateIdentity" @activity-change="conversationHasActivity = $event">
          <template #home><SchoolFactsHome :shortcuts="shortcuts" :assistant-enabled="assistantEnabled" @select="openWorkCenter" /></template>
          <template #display="{ payload }"><ProductAnswer @identity-invalid="controller.invalidateIdentity" :payload="payload" selectable :contact-selectable="!parentContext && !workspaceSession?.directory_enabled" :disabled="state.authentication_required || state.busy !== null" @select-campus="selectCampus" @review-directory="reviewFact($event)" @review-family="reviewFact($event, true)" @review-education="reviewFact($event, 'education')" @review-residence="reviewFact($event, 'residence')" @related-directory="selectRelated" /></template>
        </AssistantConversation>
      </section>
      <SchoolFactsHome v-else :shortcuts="shortcuts" :assistant-enabled="false" @select="openWorkCenter" />
    </div>
    <dialog v-if="active" ref="workCenter" class="work-center" data-role="work-center" aria-labelledby="work-center-title" aria-modal="true" @cancel.prevent="closeWorkCenter" @keydown="workCenterKeydown" @click="($event.target === workCenter) && closeWorkCenter()">
      <div class="work-center-header"><div><p class="eyebrow">学校资料，按需查看</p><h2 id="work-center-title">工作中心</h2></div><button type="button" class="icon-button" data-action="close-work-center" aria-label="关闭工作中心" @click="closeWorkCenter"><SchoolFactIcon name="close" /></button></div>
      <p class="work-identity">{{ activeIdentityLabel }}</p>
      <div v-if="parentContext && workspaceSession?.contexts.length" class="work-child-selection">
        <label for="work-center-child">资料卡查看孩子</label>
        <select id="work-center-child" :disabled="state.authentication_required && !state.identity_rechecked" :value="state.authentication_required ? '' : workspaceSession.selected_context_ref ?? ''" @change="contextChanged">
          <option v-if="state.authentication_required || workspaceSession.selected_context_ref === null" value="" disabled>选择孩子查看资料卡</option>
          <option v-for="item in workspaceSession.contexts" :key="item.ref" :value="item.ref">{{ item.label }}</option>
        </select>
        <p v-if="assistantEnabled">这里切换资料卡；对话仍可询问任一已关联孩子。</p>
      </div>
      <div class="work-tabs"><span>资料与维护</span><p>查询资料或核对修改，完成后继续对话。</p></div>
      <p v-if="state.error_message && workCenterOpen" class="error" role="alert">{{ state.error_message }}</p>
      <aside v-if="state.busy !== 'session'" :key="state.context_epoch" class="panel facts-panel" aria-label="学校事实与维护">
        <FamilyProfilePanel v-if="parentContext && workspaceSession?.selected_context_kind === 'guardian_relationship' && workspaceSession?.selected_context_ref" data-work-area="family" :assistant-enabled="assistantEnabled" :identity-ref="workspaceSession.selected_context_ref" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前孩子'" :identity-invalid="state.authentication_required" @identity-invalid="controller.invalidateIdentity" />
        <p v-else-if="parentContext">如需查看孩子资料卡，请在上方选择孩子<span v-if="assistantEnabled">；向小静提问不需要先切换</span>。</p>
        <template v-if="!parentContext && !workspaceSession?.directory_enabled">
        <h2>官方联系</h2>
        <p class="section-note">直接核对正式记录，并查看来源与生效时间。</p>
        <form data-role="contact-query" data-work-area="contact" class="query-form" @submit.prevent="controller.queryContacts(campus)">
          <label for="campus-name">校区名称</label>
          <input id="campus-name" v-model="campus" data-role="campus-name" required maxlength="100" autocomplete="off" :placeholder="props.formal ? '请输入校区名称' : '工程示例：合成工程校区'" />
          <button type="submit" data-action="query-contacts" :disabled="state.authentication_required || state.busy !== null || !campus.trim()">
            {{ state.busy === "query" ? "正在查询……" : "查询正式联系" }}
          </button>
        </form>
        <button type="button" data-action="list-campuses" :disabled="state.authentication_required || state.busy !== null" @click="controller.listCampuses">查看可查询校区</button>
        <div v-if="state.result" data-role="contact-results">
          <ProductContactResult :result="state.result" :editable="workspaceSession?.can_maintain"
            :disabled="locked" selectable @select-campus="controller.selectCampus" @edit="controller.editContact" />
        </div>
        <form v-if="state.selected_contact && workspaceSession?.can_maintain" class="maintenance-form"
          @submit.prevent="controller.prepare">
          <h3>维护官方联系</h3>
          <p>{{ state.selected_contact.campus_name }} · {{ state.selected_contact.contact_kind }}</p>
          <p class="object-reference">联系引用：{{ state.selected_contact.reference }}</p>
          <label for="contact-value">联系内容</label>
          <input id="contact-value" data-role="contact-value" :value="state.draft.contact_value"
            :disabled="locked && !state.authentication_required" :readonly="state.authentication_required" required maxlength="1000" autocomplete="off" @input="updateDraft('contact_value', $event)" />
          <label for="service-hours">服务时间（选填）</label>
          <input id="service-hours" data-role="service-hours" :value="state.draft.service_hours"
            :disabled="locked && !state.authentication_required" :readonly="state.authentication_required" maxlength="1000" autocomplete="off" @input="updateDraft('service_hours', $event)" />
          <label for="change-reason">修改原因</label>
          <textarea id="change-reason" data-role="change-reason" :value="state.draft.reason"
            :disabled="locked && !state.authentication_required" :readonly="state.authentication_required" required maxlength="1000" rows="2" @input="updateDraft('reason', $event)" />
          <button type="submit" data-action="prepare-contact" :disabled="!canPrepare">
            {{ state.busy === "prepare" ? "正在准备……" : "准备修改" }}
          </button>
        </form>
        <section v-if="state.proposal" class="confirmation-card" aria-label="待确认修改">
          <h3>请核对本次修改</h3>
          <p>{{ state.proposal.summary.before.campus_name }} · {{ state.proposal.summary.before.contact_kind }}</p>
          <p class="object-reference">联系引用：{{ state.proposal.summary.before.reference }}</p>
          <dl>
            <div><dt>原联系内容</dt><dd>{{ state.proposal.summary.before.contact_value }}</dd></div>
            <div><dt>新联系内容</dt><dd>{{ state.proposal.summary.after.contact_value }}</dd></div>
            <div><dt>原服务时间</dt><dd>{{ state.proposal.summary.before.service_hours ?? "尚未录入" }}</dd></div>
            <div><dt>新服务时间</dt><dd>{{ state.proposal.summary.after.service_hours ?? "未填写" }}</dd></div>
            <div><dt>修改原因</dt><dd>{{ state.proposal.summary.reason }}</dd></div>
          </dl>
          <p class="section-note">确认有效至 {{ state.proposal.expires_at }}；仅此按钮会提交修改。</p>
          <button type="button" data-action="confirm-contact" :disabled="locked" @click="controller.confirm">确认并发布修改</button>
        </section>
        <p v-if="state.busy === 'confirm'" role="status">正在提交，请等待正式回执……</p>
        <section v-if="state.receipt" data-role="official-receipt" class="receipt-card" aria-label="正式操作回执" aria-live="polite">
          <h3>{{ state.receipt.status === "succeeded" ? "正式修改已完成" : state.receipt.status === "rejected" ? "本次修改未执行" : "结果尚待核对" }}</h3>
          <p>{{ state.receipt.message }}</p>
          <p v-if="state.receipt.target_version !== null">学校事实总版本 {{ state.receipt.target_version }}</p>
          <p v-if="state.receipt.changed_count !== null">实际变更 {{ state.receipt.changed_count }} 项</p>
          <p v-if="state.receipt.request_id" class="section-note">请求编号：{{ state.receipt.request_id }}</p>
          <p class="section-note">回执引用：{{ state.receipt.reference }}</p>
          <button v-if="state.receipt.status === 'unknown' || state.receipt.status === 'succeeded'" type="button" data-action="check-receipt"
            :disabled="state.authentication_required || state.busy !== null" @click="controller.checkReceipt">查询原操作回执</button>
          <p v-if="state.receipt.status !== 'unknown'" class="section-note">可重新查询正式联系，核对当前发布版本。</p>
        </section>
        </template>
        <details v-if="!parentContext && (residenceEnabled || workspaceSession?.education_enabled || workspaceSession?.family_enabled || workspaceSession?.directory_enabled)" ref="maintenanceArea" data-role="staff-maintenance" class="maintenance-area">
        <summary>资料查询与维护</summary>
        <p class="section-note">按需查看完整资料或核对修改；每项读取和维护仍按当前职责分别核验。</p>
        <SchoolFactPackageImport v-if="packagesEnabled" :enabled-families="packageFamilies" :key="`package-${state.context_epoch}`" :identity-epoch="state.context_epoch" :identity-invalid="state.authentication_required" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前履职身份'" @identity-invalid="controller.invalidateIdentity" />
        <DirectoryWorkbench data-work-area="residence" @operation-result="assistantConversation?.continueOperation($event)" v-if="residenceEnabled" batch residence-imports family-corrections ref="residenceWorkbench" :api="residenceApi" title="住宿资料" :identity-invalid="state.authentication_required" @identity-invalid="controller.invalidateIdentity" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前履职身份'" />
        <DirectoryWorkbench data-work-area="education" @operation-result="assistantConversation?.continueOperation($event)" v-if="workspaceSession?.education_enabled" education family-corrections ref="educationWorkbench" :api="educationApi" title="班级与教育资料" :identity-invalid="state.authentication_required" @identity-invalid="controller.invalidateIdentity" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前履职身份'" />
        <DirectoryWorkbench data-work-area="family" @operation-result="assistantConversation?.continueOperation($event)" v-if="workspaceSession?.family_enabled" family-corrections ref="familyWorkbench" :api="familyStaffApi" title="学生与家校资料" :identity-invalid="state.authentication_required" @identity-invalid="controller.invalidateIdentity" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前履职身份'" />
        <DirectoryWorkbench data-work-area="directory" @operation-result="assistantConversation?.continueOperation($event)" :identity-invalid="state.authentication_required" @identity-invalid="controller.invalidateIdentity" v-if="!parentContext && workspaceSession?.directory_enabled" ref="directoryWorkbench" :identity-label="workspaceSession?.contexts.find(item => item.ref === workspaceSession?.selected_context_ref)?.label ?? '当前履职身份'" />
        </details>
      </aside>
    </dialog>
  </main>
</template>

<style scoped>
.school-facts-app { min-height: 100dvh; box-sizing: border-box; padding: max(.65rem, env(safe-area-inset-top)) clamp(.9rem, 3vw, 2.8rem) 1rem; color: #142052; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; background: radial-gradient(ellipse at 9% 5%, #c4e8ff94, transparent 32%), radial-gradient(ellipse at 98% 25%, #bce6ffad, transparent 38%), linear-gradient(150deg, #f7fdff 4%, #eaf7ff 57%, #f0f9ff); position: relative; overflow-x: clip; isolation: isolate; }
.school-facts-app:before { content: ''; position: absolute; width: 25rem; height: 25rem; border-radius: 50%; top: -19rem; left: -8rem; border: 1px solid #ffffffb3; background: #a9deff45; z-index: -1; }
.product-header { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; max-width: 1120px; min-height: 4rem; margin: 0 auto; gap: .5rem; }
.header-leading,.header-actions { display: flex; align-items: center; gap: .6rem; }
.header-actions { justify-content: flex-end; }
.product-name { font-size: 1.65rem; letter-spacing: .1em; font-weight: 750; }
.icon-button { display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px; flex: 0 0 auto; border: 0; padding: .6rem; background: transparent; color: #1a2d65; cursor: pointer; border-radius: 50%; font: inherit; }
.icon-button svg { width: 26px; height: 26px; }
.icon-button:hover { background: #dcefff; }
.icon-button:focus-visible { outline: 3px solid #2289ff; outline-offset: 3px; }
.work-button { width: auto; gap: .4rem; border-radius: 22px; font-size: .85rem; }
.new-button { background: #bee2ff8c; }
h2 { margin: 0; } h3 { font-size: 1rem; }
.eyebrow { margin: 0 0 .4rem; color: #7587ac; font-size: .8rem; }
.engineering-badge { display: inline-block; padding: .2rem .5rem; border-radius: 10px; background: #e9f2ff; color: #4e699d; font-size: .68rem; }
.section-note, .identity-note { color: #667b9b; line-height: 1.65; }
.identity-panel { max-width: 1080px; margin: .4rem auto .75rem; padding: 0 .75rem; border: 1px solid #ffffffad; border-radius: 16px; background: #ffffff52; font-size: .85rem; }
.identity-panel > summary { display: flex; align-items: center; gap: .45rem; min-height: 2.8rem; list-style: none; cursor: pointer; color: #475d86; }
.identity-panel > summary::-webkit-details-marker { display: none; }
.identity-panel > summary:after { content: '⌄'; margin-left: auto; }
.identity-panel[open] > summary:after { transform: rotate(180deg); }
.identity-dot { width: .4rem; height: .4rem; border-radius: 50%; background: #4aaadb; flex: 0 0 auto; }
.identity-caption { margin-left: auto; color: #7b8eac; font-size: .74rem; }
.identity-content { display: flex; align-items: center; flex-wrap: wrap; gap: .7rem; padding: .8rem .25rem 1rem; border-top: 1px solid #dceaf74d; }
.identity-content > p { margin: 0; }
.identity-note { width: 100%; margin: 0; font-size: .8rem; }
.identity-note > summary { cursor: pointer; }
.workspace { max-width: 960px; margin: 0 auto; min-width: 0; }
.panel { min-width: 0; }
.assistant-panel { padding: 0; }
.school-facts-app :deep(.assistant-conversation) { color: #1b2a55; }
.workspace:not(.with-conversation) :deep(.assistant-conversation) { min-height: calc(100dvh - 9rem); display: flex; flex-direction: column; }
.workspace:not(.with-conversation) :deep(.welcome-home) { flex: 1; }
.school-facts-app :deep(.composer-dock) { padding-bottom: max(.5rem, env(safe-area-inset-bottom)); }
.school-facts-app :deep(.composer) { min-height: 3.8rem; border: 1px solid #fff; border-radius: 2.2rem; box-shadow: 0 8px 25px #82c6f328; }
.school-facts-app :deep(.composer button) { width: 3.2rem; height: 3.2rem; border-radius: 50%; padding: .6rem; background: linear-gradient(150deg,#27d7eb 3%,#167fff 68%,#215aff); box-shadow: 0 4px 10px #287fff3d; }
.school-facts-app :deep(.composer button .send-label) { position: absolute; width: 1px; height: 1px; clip-path: inset(50%); overflow: hidden; }
.school-facts-app :deep(.composer button:has(span:not(.send-label))) { width: auto; border-radius: 1.5rem; padding-inline: .8rem; }
.school-facts-app :deep(.composer button svg) { width: 1.6rem; height: 1.6rem; }
.work-center { position: fixed; inset: 0 0 0 auto; margin: 0; width: min(660px,92vw); height: 100dvh; max-height: 100dvh; max-width: 100vw; box-sizing: border-box; padding: max(1.5rem,env(safe-area-inset-top)) clamp(1rem,3vw,1.75rem) max(2rem,env(safe-area-inset-bottom)); border: 1px solid #fff; border-radius: 28px 0 0 28px; background: linear-gradient(135deg,#f7fcff,#e8f5ff 80%,#f5fbff); color: #142052; box-shadow: -18px 0 60px #315e9426; overflow-y: auto; overscroll-behavior: contain; z-index: 110; }
.work-center::backdrop { background: #21417625; backdrop-filter: blur(3px); }
.work-center-header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.work-center-header h2 { font-size: 1.85rem; letter-spacing: .02em; }
.work-identity { color: #657ca4; font-size: .85rem; margin: 1rem 0; }
.work-child-selection { display: grid; gap: .45rem; padding: .8rem 1rem; background: #ffffffb0; border: 1px solid #fff; border-radius: 16px; font-size: .88rem; }
.work-child-selection select { width:100%; min-height:44px; padding:.6rem; border:1px solid #c6dbef; border-radius:10px; font:inherit; }
.work-child-selection p { margin:0; color:#667b9b; font-size:.78rem; line-height:1.6; }
.work-tabs { display: flex; align-items: center; flex-wrap: wrap; gap: .6rem 1rem; margin: 1.2rem 0; padding: .3rem; background: #ffffff70; border: 1px solid #fff; border-radius: 24px; }
.work-tabs > span { padding: .7rem 1.4rem; border-radius: 22px; color: #fff; background: linear-gradient(115deg,#28d0e9,#2477ff); box-shadow: 0 2px 6px #319fff30; }
.work-tabs > p { color: #6b7fa7; font-size: .78rem; margin: 0; padding: 0 .6rem; }
.facts-panel { padding: 1.25rem; background: #ffffffc7; border: 1px solid #fff; border-radius: 24px; }
.facts-panel :deep(.directory-workbench) { margin-top: 1rem; border-top: 1px solid #dbeaf6; padding-top: 1.2rem; scroll-margin: 1rem; }
.query-form, .maintenance-form { display: grid; gap: .6rem; margin-bottom: 1.25rem; }
.maintenance-form { margin-top: 1.5rem; border-top: 1px solid #dae8f5; padding-top: .7rem; }
.identity-content :deep(button), .facts-panel :deep(button), .identity-content input, .identity-content select, .facts-panel input, .facts-panel select, .facts-panel textarea { box-sizing: border-box; min-height: 44px; border: 1px solid #c6dbef; border-radius: 10px; padding: .6rem .8rem; font: inherit; max-width: 100%; }
.identity-content :deep(button), .facts-panel :deep(button) { cursor: pointer; color: #1767b7; background: #edf7ff; border-color: #b9dcff; }
.identity-content :deep(button:disabled), .facts-panel :deep(button:disabled) { cursor: default; opacity: .5; }
.identity-content :deep(button:focus-visible), .facts-panel :deep(button:focus-visible), input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 3px solid #81beff; outline-offset: 2px; }
input, select, textarea { color: #213a62; background: #fff; width: 100%; } select { width: auto; }
.confirmation-card, .receipt-card { padding: 1rem; border: 1px solid #c6dff2; border-radius: 16px; margin-top: 1rem; background: #f1f9ff; }
dl { display: grid; gap: .5rem; } dl div { display: grid; grid-template-columns: 6em 1fr; gap: .5rem; }
dd { margin: 0; overflow-wrap: anywhere; } dt { color: #60799b; }
.object-reference { font-size: .8rem; color: #647f9e; overflow-wrap: anywhere; }
.maintenance-area > summary { cursor: pointer; font-weight: 600; min-height: 44px; align-content: center; color: #284779; }
.error { padding: .8rem; background: #fff2ed; color: #963f2d; border-radius: 14px; max-width: 960px; margin: 1rem auto; }
@media (max-width: 600px) { .school-facts-app {padding-inline: .9rem;} .product-header {min-height: 3.5rem;} .product-name {font-size: 1.45rem;} .header-actions{gap: .15rem;} .work-button span {display: none;} .identity-panel {margin-top: .15rem; font-size: .78rem;} .identity-panel > summary {min-height:2.5rem;} .identity-caption {font-size:.7rem;} .identity-panel summary:has(.engineering-badge) .identity-caption{display:none;} .engineering-badge{margin-left:auto;white-space:nowrap;} .work-center {width:94vw;padding:1.15rem .85rem 1.5rem;border-radius:24px 0 0 24px;} .work-center-header h2 {font-size:1.65rem;} .work-tabs {border-radius:18px;} .work-tabs > span {padding:.6rem 1.1rem;} .work-tabs > p {line-height:1.6;} .facts-panel {padding:1rem;} .identity-content select {width:100%;} }
</style>

<style>
body:has(.school-facts-app), body:has(.identity-entry) { margin: 0; background: #edf8ff; }
</style>
