<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import XiaojingBrand from "./XiaojingBrand.vue";
import { requestDingTalkAuthCode } from "./dingtalk";

type Phase = "ready" | "authenticating" | "delivering" | "finished";
const phase = ref<Phase>("ready");
const message = ref("");
let alive = true;
let timer: ReturnType<typeof setTimeout> | undefined;
let controller: AbortController | undefined;
const label = computed(() => ({ ready: "验证我的身份", authenticating: "正在核验钉钉身份", delivering: "正在交付身份", finished: "本次操作已结束" })[phase.value]);
const unknown = "暂时无法核实交付结果。请联系本次接入负责人核对原交付记录，不要重复操作。";
const active = () => alive && phase.value !== "finished";
function finish(text: string) {
  if (!alive || phase.value === "finished") return;
  clearTimeout(timer); phase.value = "finished"; message.value = text;
}
async function verify() {
  if (phase.value !== "ready" || !alive) return;
  phase.value = "authenticating";
  controller = new AbortController();
  timer = setTimeout(() => {
    const text = phase.value === "authenticating"
      ? "钉钉身份响应超时，尚未发起身份交付。请联系本次接入负责人。" : unknown;
    controller?.abort(); finish(text);
  }, 30_000);
  try {
    const code = await requestDingTalkAuthCode();
    if (!active()) return;
    phase.value = "delivering";
    const response = await fetch("/api/v1/school-fact-initialization/dingtalk/exchange", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auth_code: code }), signal: controller.signal,
    });
    if (!active()) return;
    if (response.status === 403) return finish("本次身份核验入口已关闭，或当前账号不在本次接入范围内。请联系本次接入负责人核对。");
    if (response.status === 409) return finish("本次接入已有交付记录，请先核对原交付结果，不要重复操作。");
    if (response.status === 401) return finish("未完成身份验证。请由本次接入负责人核对账号和有效期。");
    if (response.status === 429) return finish("操作较频繁，请联系本次接入负责人后再处理。");
    if (!response.ok) return finish(unknown);
    const body: unknown = await response.json();
    finish(body && typeof body === "object" && "status" in body && body.status === "identity_delivered"
      ? "身份已核验，等待资料配置。接入负责人完成学校资料与职责配置后，会通知你从正式入口进入。" : unknown);
  } catch {
    finish(phase.value === "authenticating" ? "未完成身份验证，请从钉钉打开本次指定的入口。" : unknown);
  }
}
onBeforeUnmount(() => { alive = false; clearTimeout(timer); controller?.abort(); });
</script>

<template>
  <main class="initialization-entry">
    <section aria-labelledby="initialization-title">
      <XiaojingBrand class="entry-brand" />
      <h1 id="initialization-title">首次接入身份核验</h1>
      <p>仅供本次指定的首位资料维护人使用。请确认正在使用你本人的钉钉账号。</p>
      <p>本步骤只核验身份，不会自动授予学校资料权限。</p>
      <button type="button" :disabled="phase !== 'ready'" @click="verify">{{ label }}</button>
      <p role="status" aria-live="polite">{{ message }}</p>
    </section>
  </main>
</template>

<style scoped>
.initialization-entry { min-height: 100dvh; box-sizing: border-box; display: grid; place-items: center; padding: 28px 20px; background: linear-gradient(145deg, #d7f4ff, #f7fcff 55%, #d8edff); color: #142153; font-family: system-ui, sans-serif; }
section { width: min(100%, 460px); padding: 30px 24px; box-sizing: border-box; border: 1px solid white; border-radius: 28px; background: #ffffffb8; box-shadow: 0 12px 36px #1a65ab14; }
.entry-brand { width: 6rem; }
h1 { font-size: 1.55rem; margin: 20px 0; }
p { line-height: 1.7; color: #4b607c; }
button { width: 100%; margin-top: 12px; border: 0; border-radius: 16px; padding: 15px; background: linear-gradient(110deg, #15bfec, #2462ff); color: white; font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { cursor: default; opacity: .65; }
button:focus-visible { outline: 3px solid #12309f; outline-offset: 3px; }
[role=status] { margin-bottom: 0; }
</style>
