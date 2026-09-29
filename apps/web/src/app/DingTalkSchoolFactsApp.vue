<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { requestDingTalkAuthCode } from "./dingtalk";
import SchoolFactsApp from "./SchoolFactsApp.vue";
import XiaojingBrand from "./XiaojingBrand.vue";
import { createProductApi } from "../features/school_facts/product_api";
import { isProductSession } from "../features/school_facts/product_contracts";

const ready = ref(false);
const busy = ref(false);
const error = ref("");
const filing = import.meta.env.VITE_ICP_FILING_NUMBER?.trim() ?? "";
let epoch = 0;
let disposed = false;
async function authenticate() {
  if (busy.value) return;
  const current = ++epoch;
  ready.value = false;
  busy.value = true;
  error.value = "";
  try {
    const auth_code = await requestDingTalkAuthCode();
    if (disposed || epoch !== current) return;
    const response = await fetch("/api/v1/school-fact-product/dingtalk/exchange", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auth_code }),
    });
    if (!response.ok) throw new Error(response.status === 401
      ? "钉钉身份验证失败，请重新进入。" : "身份服务暂时不可用，请稍后重试。");
    const session: unknown = await response.json();
    if (!isProductSession(session) || session.status !== "signed_in" || session.accounts.length !== 0)
      throw new Error("当前身份无法核验，请从学校钉钉入口重新进入。");
    if (!disposed && epoch === current) ready.value = true;
  } catch {
    if (!disposed && epoch === current) error.value = "身份验证未完成，请确认已从学校钉钉应用进入后重试。";
  } finally { if (!disposed && epoch === current) busy.value = false; }
}
onMounted(async () => {
  try {
    const session = await createProductApi().getSession();
    if (disposed) return;
    if (session.status === "signed_in" && session.accounts.length === 0) {
      ready.value = true;
      return;
    }
  } catch { /* A fresh verified login is the only fallback. */ }
  if (!disposed) await authenticate();
});
onUnmounted(() => { disposed = true; epoch += 1; });
</script>

<template>
  <SchoolFactsApp v-if="ready" formal @reauthenticate="authenticate" />
  <main v-else class="identity-entry">
    <span class="entry-name">小静</span>
    <XiaojingBrand orbit />
    <h1>你好，我是小静</h1>
    <p class="entry-tagline">让校园生活更简单、更美好</p>
    <section class="entry-status" aria-label="钉钉身份验证">
      <p v-if="busy" role="status"><span class="status-dot"></span>正在验证钉钉身份……</p>
      <template v-else-if="error">
        <p role="alert">{{ error }}</p>
        <button type="button" @click="authenticate">重新验证</button>
      </template>
      <p v-else role="status"><span class="status-dot"></span>正在读取当前身份……</p>
    </section>
  </main>
  <footer v-if="filing"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">{{ filing }}</a></footer>
</template>

<style scoped>
.identity-entry { min-height: 100dvh; box-sizing: border-box; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 5rem 1.5rem; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; color: #142052; background: radial-gradient(ellipse at 10% 8%,#c4e8ffbb,transparent 45%),radial-gradient(ellipse at 100% 65%,#cceeffab,transparent 48%),linear-gradient(145deg,#f9fdff,#eaf7ff); }
.entry-name { position: absolute; top: max(1.8rem,env(safe-area-inset-top)); font-weight: 700; font-size: 1.65rem; letter-spacing: .1em; }
h1 {font-size: clamp(1.9rem,5vw,2.5rem); margin: 2rem 0 .75rem; letter-spacing:.04em; }
.entry-tagline {color:#7184ac; margin:0 0 2rem; font-size:1rem;}
.entry-status {width:min(100%,28rem); padding:1.2rem; box-sizing:border-box; border:1px solid #fff; border-radius:24px; background:#ffffffb5; text-align:center; color:#557199; font-size:.92rem; line-height:1.8; box-shadow:0 12px 34px #77bae518;}
.status-dot {display:inline-block;width:.5rem;height:.5rem;border-radius:50%;background:#34a5f8;margin-right:.6rem;}
button {min-height:44px;padding:.65rem 1.5rem;border:1px solid #fff;border-radius:24px;background:linear-gradient(115deg,#28cce9,#2677ff);color:white;font:inherit;cursor:pointer;}
button:focus-visible{outline:3px solid #2488ff;outline-offset:3px;}
footer { text-align: center; margin: 0; padding: 1rem; background:#edf8ff;font-size:.75rem; } footer a{color:#7085a5;}
</style>
