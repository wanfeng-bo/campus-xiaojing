<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";

import type {
  AssistantControl,
  AssistantJsonValue,
} from "./assistant_contracts";
import {
  createAssistantSessionController,
  type AssistantSessionState,
} from "./assistant_session";


const props = defineProps<{ identityInvalid?: boolean; contextClaim?: string }>();
const emit = defineEmits<{ identityInvalid: []; "activity-change": [active: boolean] }>();
const controller = createAssistantSessionController();
const state = shallowRef<Readonly<AssistantSessionState>>(controller.getState());
const conversationRoot = ref<HTMLElement | null>(null);
const composerInput = ref<HTMLTextAreaElement | null>(null);
const conversationDrawer = ref<HTMLElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const conversationsOpen = ref(false);
const questionSearch = ref("");
let returnFocus: HTMLElement | null = null;
const unsubscribe = controller.subscribe((next_state) => {
  state.value = next_state;
  if (next_state.authentication_required) emit("identityInvalid");
});

watch(() => props.identityInvalid, value => { if (value) controller.invalidateIdentity(); }, { immediate: true, flush: "sync" });
const hasActivity = computed(() => Boolean(state.value.snapshot?.turns.length || state.value.pending_message || state.value.failed_messages.length));
watch(hasActivity, value => emit("activity-change", value), { immediate: true, flush: "sync" });
watch([() => state.value.authentication_required, () => state.value.snapshot?.conversation_ref], () => {
  closeConversations(false);
}, { flush: "sync" });
const currentQuestions = computed(() => {
  if (state.value.authentication_required) return [];
  const query = questionSearch.value.trim().toLocaleLowerCase();
  return (state.value.snapshot?.turns ?? []).filter(turn => !turn.continuation_of &&
    (!query || turn.teacher_utterance.toLocaleLowerCase().includes(query)));
});

function closeConversations(restoreFocus = true): void {
  conversationsOpen.value = false;
  questionSearch.value = "";
  const previous = returnFocus;
  returnFocus = null;
  if (restoreFocus && previous?.isConnected) previous.focus();
}

function openConversations(): void {
  if (state.value.authentication_required || props.identityInvalid) return;
  if (!conversationsOpen.value) returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  conversationsOpen.value = true;
  void nextTick(() => { if (conversationsOpen.value) searchInput.value?.focus(); });
}

function focusInput(): void {
  closeConversations(false);
  void nextTick(() => { if (!state.value.authentication_required) composerInput.value?.focus(); });
}

function newConversation(): Promise<boolean> {
  closeConversations();
  return controller.newConversation();
}

function locateTurn(reference: string): void {
  if (!currentQuestions.value.some(turn => turn.turn_ref === reference)) return;
  const target = Array.from(conversationRoot.value?.querySelectorAll<HTMLElement>("[data-turn-ref]") ?? [])
    .find(element => element.dataset.turnRef === reference);
  closeConversations(false);
  void nextTick(() => {
    if (!state.value.authentication_required && target?.isConnected) {
      target.scrollIntoView?.({ block: "center", behavior: "auto" });
      target.focus({ preventScroll: true });
    }
  });
}

function drawerKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.preventDefault(); event.stopPropagation(); closeConversations(); return;
  }
  if (event.key !== "Tab") return;
  const controls = Array.from(conversationDrawer.value?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)") ?? []);
  const first = controls[0]; const last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}

defineExpose({ continueOperation: controller.continueOperation, openConversations, newConversation, focusInput });

const can_send = computed(() => {
  const value = state.value;
  return !value.authentication_required && value.snapshot !== null &&
    value.snapshot.active_run === null &&
    !value.conversation_closed &&
    !value.navigation_uncertain &&
    !value.opening &&
    !value.starting_new &&
    !value.delivery_uncertain &&
    !value.sending &&
    !value.stopping &&
    value.pending_control === null &&
    value.input.length <= 8_000 &&
    /\S/.test(value.input);
});


function update_input(event: Event): void {
  controller.setInput((event.target as HTMLTextAreaElement).value);
}


function activate(control: AssistantControl): void {
  void controller.activateControl(control);
}


function answer_failed(payload: AssistantJsonValue): boolean {
  return payload !== null && typeof payload === "object" && !Array.isArray(payload) && typeof payload.error_code === "string";
}

function pending_label(status: string): string {
  return ({ sending: "正在发送，尚未确认送达", uncertain: "送达状态待核对，请勿重复发送", failed: "未获受理", stopped: "已停止本对话接收" } as Record<string, string>)[status] ?? "状态待核对";
}

function display_text(payload: AssistantJsonValue): string {
  if (typeof payload === "string") {
    return payload;
  }
  if (
    payload !== null &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    typeof payload.text === "string"
  ) {
    return payload.text;
  }
  return JSON.stringify(payload);
}


onMounted(() => {
  void controller.open(props.contextClaim);
});
onUnmounted(() => { returnFocus = null; unsubscribe(); controller.dispose(); });
</script>

<template>
  <section ref="conversationRoot" class="assistant-conversation" :class="{ 'welcome-conversation': !!$slots.home && !hasActivity }" aria-label="小静对话" :data-navigation-phase="state.navigation_phase">
    <header class="conversation-header">
      <h2><span class="conversation-mark" aria-hidden="true">✦</span> 当前对话</h2>
      <div class="conversation-actions">
      <button type="button" class="quiet-button" data-action="open-conversations" :disabled="state.authentication_required" @click="openConversations">查找</button>
      <button
        v-if="state.snapshot !== null"
        type="button"
        data-action="refresh"
        :disabled="state.authentication_required || state.starting_new || state.refreshing || state.pending_control !== null"
        @click="controller.refresh"
      >
        {{ state.refreshing ? "正在刷新……" : "刷新" }}
      </button>
      <button v-if="state.snapshot !== null" type="button" data-action="new-conversation"
        :disabled="state.authentication_required || state.starting_new || state.opening || state.refreshing || state.stopping || state.pending_control !== null"
        @click="newConversation">
        {{ state.starting_new ? "正在建立新对话……" : "新对话" }}
      </button>
      </div>
    </header>
    <p class="conversation-note">新对话会停止旧对话中仍在进行的回答，并使用独立上下文；不会撤销已提交的操作。</p>

    <p v-if="state.opening">正在建立对话……</p>
    <p v-if="state.error_message !== null" role="alert">
      {{ state.error_message }}
    </p>
    <button
      v-if="state.snapshot === null && !state.opening && !state.authentication_required"
      type="button"
      data-action="open"
      @click="controller.open()"
    >
      重新连接
    </button>

    <slot v-if="!hasActivity" name="home" />

    <ol v-if="state.snapshot !== null" class="turn-list">
      <li
        v-for="turn in state.snapshot.turns"
        :key="`${turn.turn_ref}:${turn.generation}`"
        class="turn" :data-turn-ref="turn.turn_ref" tabindex="-1"
      >
        <p v-if="turn.continuation_of" class="turn-status" aria-label="系统续接">继续处理原任务</p>
        <section v-else class="user-message" aria-label="用户消息"><strong>你</strong>
          <p class="teacher-utterance">{{ turn.teacher_utterance }}</p>
        </section>
        <section class="assistant-message" aria-label="对应的小静回答"><strong>小静</strong>
        <p v-if="turn.visible_state === 'active'" class="turn-status" role="status">{{ state.terminal_failure_run_ref === turn.run_ref ? "本轮处理已中止，未取得完整回答" : state.error_message && !state.sending ? "连接中断，回答状态待核对" : "已受理，正在等待回答……" }}</p>
        <p v-else-if="turn.visible_state === 'stopped'" class="turn-status">已停止</p>
        <p v-if="answer_failed(turn.display_payload)" class="turn-status" role="status">{{ turn.continuation_of ? "后续回答未完成；正式操作结果仍以原回执为准。" : "本轮未能完成" }}</p>
        <slot v-if="turn.visible_state === 'completed'" name="display" :payload="turn.display_payload" :kind="turn.display_kind" :turn-ref="turn.turn_ref">
          <p v-if="turn.display_kind === 'text'" class="assistant-response">
            {{ display_text(turn.display_payload) }}
          </p>
          <pre v-else-if="turn.display_kind === 'card'" class="assistant-card">{{ JSON.stringify(turn.display_payload, null, 2) }}</pre>
        </slot>

        <div v-if="turn.controls.length > 0" class="turn-controls">
          <button
            v-for="control in turn.controls"
            :key="`${control.kind}:${control.handle}`"
            type="button"
            :data-control-kind="control.kind"
            :disabled="state.authentication_required || state.starting_new || state.pending_control !== null || state.refreshing || state.stopping || state.sending"
            @click="activate(control)"
          >
            {{ control.safe_label }}
          </button>
        </div>
        <button v-if="!turn.continuation_of && turn.visible_state === 'completed' && answer_failed(turn.display_payload)" type="button" data-action="retry-turn"
          :disabled="state.authentication_required || state.sending || !!state.snapshot?.active_run || state.conversation_closed || state.navigation_uncertain || state.starting_new || state.delivery_uncertain"
          @click="controller.retry(turn.teacher_utterance)">重试这条消息</button>
        <button v-if="turn.continuation_of && turn.visible_state === 'completed' && answer_failed(turn.display_payload)" type="button" data-action="retry-continuation"
          :disabled="state.authentication_required || state.sending || !!state.snapshot?.active_run || state.conversation_closed || state.navigation_uncertain || state.starting_new || state.delivery_uncertain"
          @click="controller.retryContinuation(turn.continuation_of)">核对原任务续接</button>
        </section>
      </li>
      <li v-for="message in state.failed_messages" :key="message.id" class="turn failed-turn">
        <section class="user-message" aria-label="用户消息"><strong>你</strong><p class="teacher-utterance">{{ message.text }}</p></section>
        <p class="turn-status">此前请求未获受理</p>
        <button type="button" :disabled="state.authentication_required || state.sending || !!state.snapshot?.active_run || state.starting_new || state.conversation_closed || state.navigation_uncertain || state.delivery_uncertain" @click="controller.retry(message.text)">重试这条消息</button>
      </li>
      <li v-if="state.pending_message" class="turn pending-turn" :data-local-message="state.pending_message.id">
        <section class="user-message" aria-label="用户消息"><strong>你</strong><p class="teacher-utterance">{{ state.pending_message.text }}</p></section>
        <p class="turn-status" role="status">{{ pending_label(state.pending_message.status) }}</p>
        <button v-if="state.pending_message.status === 'failed'" type="button" data-action="retry-message"
          :disabled="state.authentication_required || state.sending || state.starting_new || state.conversation_closed || state.navigation_uncertain" @click="controller.retry(state.pending_message.text)">重试这条消息</button>
      </li>
    </ol>

    <div class="composer-dock">
    <button
      v-if="!state.terminal_failure_run_ref && (!!state.snapshot?.active_run || state.sending || state.delivery_uncertain)"
      type="button"
      data-action="stop"
      class="stop-button"
      :disabled="state.authentication_required || state.starting_new || state.stopping"
      @click="controller.stop"
    >
      {{ state.stopping ? "正在停止……" : "停止" }}
    </button>

    <form class="composer" @submit.prevent="controller.send">
      <label for="assistant-input" class="visually-hidden">输入本次要说的话</label>
      <textarea
        ref="composerInput"
        id="assistant-input"
        data-role="assistant-input"
        :value="state.input"
        maxlength="8000"
        rows="2"
        placeholder="有什么想了解的，问问小静……"
        @input="update_input"
      />
      <button type="submit" data-action="send" :disabled="!can_send">
        <svg v-if="!state.terminal_failure_run_ref && !state.sending && !state.snapshot?.active_run" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        <span :class="{ 'send-label': !state.terminal_failure_run_ref && !state.sending && !state.snapshot?.active_run }">{{ state.terminal_failure_run_ref ? "先核对对话" : state.pending_message?.status === "sending" ? "发送中" : state.sending || state.snapshot?.active_run ? "等待回答" : "发送" }}</span>
      </button>
    </form>
    <p v-if="!state.terminal_failure_run_ref && (state.sending || state.snapshot?.active_run)" class="conversation-note composer-note">可继续编辑下一条草稿；当前回答完成或停止后再发送。停止回答不会撤销已提交的正式操作。</p>
    </div>

    <div v-if="conversationsOpen && !state.authentication_required" class="conversation-overlay" @click.self="closeConversations()">
      <aside ref="conversationDrawer" class="conversation-drawer" role="dialog" aria-modal="true" aria-labelledby="conversation-drawer-title" @keydown="drawerKeydown">
        <header class="drawer-header"><div><p class="drawer-eyebrow">小静</p><h2 id="conversation-drawer-title">当前对话</h2></div><button type="button" class="drawer-close" aria-label="关闭对话查找" @click="closeConversations()">×</button></header>
        <label class="visually-hidden" for="conversation-search">查找当前对话中的问题</label>
        <input id="conversation-search" ref="searchInput" v-model="questionSearch" type="search" placeholder="查找当前对话中的问题" autocomplete="off" />
        <button type="button" class="drawer-new" data-action="drawer-new-conversation" :disabled="state.snapshot === null || state.starting_new || state.opening || state.refreshing || state.stopping || state.pending_control !== null" @click="newConversation"><span aria-hidden="true">＋</span> 新对话</button>
        <p class="drawer-section-label">本次提问</p>
        <ol v-if="currentQuestions.length" class="question-index"><li v-for="turn in currentQuestions" :key="turn.turn_ref"><button type="button" data-action="locate-turn" @click="locateTurn(turn.turn_ref)">{{ turn.teacher_utterance }}</button></li></ol>
        <p v-else class="drawer-empty">{{ questionSearch.trim() ? "没有找到相符的问题。" : "当前还没有已受理的问题。" }}</p>
        <p class="drawer-retention">这里只查找当前对话。聊天最多在内存中保留24小时，刷新或重启可能丢失；新对话会停止旧回答且不保留旧对话入口，不会撤销已提交的操作。</p>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.assistant-conversation {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1.2rem;
  min-width: 0;
  color: #101b59;
  overflow-wrap: anywhere;
}
.welcome-conversation > .conversation-header,
.welcome-conversation > .conversation-note { display: none; }
.welcome-conversation > .turn-list:empty { display: none; }

.conversation-header,
.conversation-actions,
.turn-controls {
  display: flex;
  align-items: center;
  gap: .5rem;
  flex-wrap: wrap;
}

.conversation-note { margin: 0; color: #647593; font-size: .78rem; line-height: 1.6; }

.conversation-header {
  justify-content: space-between;
  gap: .5rem;
}
.conversation-header h2 { display: flex; align-items: center; gap: .5rem; margin: 0; font-size: .9rem; font-weight: 600; color: #627496; }
.conversation-mark { color: #2187ff; font-size: 1.4rem; }
.assistant-conversation .conversation-actions button { padding: .35rem .65rem; min-height: 36px; border: 1px solid #d9e8f6; border-radius: 12px; color: #3a6094; background: rgb(255 255 255 / 65%); font-size: .8rem; }

.turn-list {
  display: grid;
  gap: 1.65rem;
  margin: 0;
  padding: 0;
  list-style: none;
}

.turn {
  display: grid;
  gap: 1rem;
  min-width: 0;
  scroll-margin: 6rem 0;
  border-radius: 20px;
}
.turn:focus-visible { outline: 3px solid #83c0ff; outline-offset: 6px; }

.user-message { justify-self: end; max-width: 88%; box-sizing: border-box; padding: .85rem 1.1rem; background: linear-gradient(130deg, #2486ff, #5daaff); color: #fff; border: 1px solid rgb(255 255 255 / 40%); border-radius: 22px 22px 6px 22px; box-shadow: 0 6px 18px rgb(47 123 213 / 10%); }
.user-message > strong { font-size: .72rem; opacity: .85; font-weight: 500; }
.assistant-message { min-width: 0; padding: 1rem 1.15rem; border: 1px solid rgb(198 221 245 / 70%); border-radius: 6px 22px 22px; background: rgb(255 255 255 / 78%); box-shadow: 0 6px 22px rgb(52 90 153 / 4%); }
.assistant-message > strong { display: flex; align-items: center; gap: .4rem; margin-bottom: .5rem; color: #235aa0; font-size: .85rem; }
.assistant-message > strong::before { content: "✦"; color: #2187ff; font-size: 1.2rem; }
.turn-status { color: #647593; font-size: .85rem; }
.turn-controls { margin-top: .8rem; }
.assistant-conversation .turn-controls button { border: 1px solid #c4ddfa; border-radius: 12px; color: #1f62b5; background: #edf6ff; }

.teacher-utterance,
.assistant-response,
.turn-status {
  margin: 0.35rem 0;
  white-space: pre-wrap;
  line-height: 1.7;
}

.assistant-card {
  overflow-x: auto;
  white-space: pre-wrap;
  font-size: .85rem;
}

.composer-dock { position: sticky; bottom: 0; z-index: 5; margin-top: .5rem; padding: .65rem 0 max(.65rem, env(safe-area-inset-bottom)); background: linear-gradient(0deg, #eff6ff 0%, rgb(239 246 255 / 90%) 70%, rgb(239 246 255 / 0%) 100%); }
.composer { display: flex; align-items: flex-end; gap: .65rem; padding: .5rem .55rem .5rem 1.05rem; background: rgb(255 255 255 / 94%); border: 1px solid #d6e7fb; border-radius: 28px; box-shadow: 0 5px 24px rgb(58 110 173 / 9%); }
.composer:focus-within { border-color: #8fc4ff; box-shadow: 0 0 0 3px rgb(33 135 255 / 9%); }
.assistant-conversation .composer textarea { flex: 1; min-width: 0; width: 100%; box-sizing: border-box; padding: .45rem .15rem; min-height: 48px; max-height: 10rem; resize: vertical; border: 0; border-radius: 0; outline: none; box-shadow: none; color: #101b59; background: transparent; line-height: 1.55; }
.composer textarea::placeholder { color: #8394b0; }
.assistant-conversation .composer button { display: flex; align-items: center; justify-content: center; gap: .2rem; flex: 0 0 auto; min-height: 44px; min-width: 44px; padding: .5rem .8rem; border: 0; border-radius: 23px; color: #fff; background: linear-gradient(135deg, #268dff, #416fea); font-size: .83rem; }
.composer button svg { width: 18px; height: 18px; }
.composer-note { padding: .5rem .9rem 0; }
.assistant-conversation .stop-button { display: block; margin: 0 auto .65rem; padding: .5rem 1rem; color: #315887; background: #fff; border: 1px solid #ccdef3; border-radius: 22px; }
.assistant-conversation button:disabled { opacity: .5; cursor: default; }
.assistant-conversation button:focus-visible, .conversation-drawer input:focus-visible { outline: 3px solid #80bfff; outline-offset: 3px; }

textarea,
button {
  min-height: 44px;
  font: inherit;
}

.visually-hidden { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.conversation-overlay { position: fixed; inset: 0; z-index: 100; display: flex; background: rgb(12 28 64 / 30%); }
.conversation-drawer { display: flex; flex-direction: column; gap: 1rem; width: min(340px, 86vw); height: 100%; box-sizing: border-box; padding: max(1.25rem, env(safe-area-inset-top)) 1.15rem max(1.25rem, env(safe-area-inset-bottom)); border-right: 1px solid rgb(255 255 255 / 75%); background: linear-gradient(155deg, #f8fcff, #edf5ff 65%, #f4fbff); box-shadow: 12px 0 45px rgb(12 40 81 / 10%); overflow-y: auto; }
.drawer-header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.drawer-eyebrow { margin: 0 0 .2rem; color: #7390b4; font-size: .8rem; }
.drawer-header h2 { margin: 0; font-size: 1.4rem; color: #101b59; }
.assistant-conversation .drawer-close { min-width: 44px; padding: 0; border: 1px solid #d9e7f7; border-radius: 50%; color: #46658d; background: #fff; font-size: 1.6rem; }
.conversation-drawer input { width: 100%; min-height: 46px; box-sizing: border-box; padding: .7rem .85rem; border: 1px solid #d9e7f7; border-radius: 15px; color: #183963; background: rgb(255 255 255 / 85%); font: inherit; font-size: .85rem; }
.assistant-conversation .drawer-new { display: flex; align-items: center; justify-content: center; gap: .5rem; min-height: 48px; border: 1px solid #cce0ff; border-radius: 15px; color: #2374d1; background: #e9f3ff; font-weight: 600; }
.drawer-new span { font-size: 1.3rem; }
.drawer-section-label { margin: .5rem 0 0; color: #7890ae; font-size: .77rem; }
.question-index { display: grid; gap: .5rem; margin: 0; padding: 0; list-style: none; }
.assistant-conversation .question-index button { display: block; width: 100%; box-sizing: border-box; padding: .85rem .9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; color: #24466f; background: rgb(255 255 255 / 80%); border: 1px solid #e0ebf8; border-radius: 14px; font-size: .88rem; }
.drawer-empty { margin: 0; color: #7285a0; font-size: .85rem; }
.drawer-retention { margin: auto 0 0; padding-top: 1.5rem; color: #788ba7; font-size: .75rem; line-height: 1.7; }
@media (max-width: 600px) {
  .assistant-conversation { gap: 1rem; }
  .conversation-header h2 { font-size: .8rem; }
  .assistant-conversation .conversation-actions button { padding: .3rem .5rem; font-size: .75rem; }
  .user-message { max-width: 92%; padding: .7rem .9rem; }
  .assistant-message { padding: .85rem; }
  .composer { gap: .4rem; padding-left: .8rem; }
  .assistant-conversation .composer textarea { font-size: 16px; }
}
</style>
