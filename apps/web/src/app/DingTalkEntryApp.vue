<script setup lang="ts">
import { onMounted, ref } from "vue";

import { requestDingTalkAuthCode } from "./dingtalk";
import { exchangeDingTalkAuthCode } from "./identity_api";


type IdentityEntryState = "error" | "loading" | "pending_configuration" | "ready";

const entry_state = ref<IdentityEntryState>("loading");
const error_message = ref("");
const icp_filing_number = import.meta.env.VITE_ICP_FILING_NUMBER?.trim() ?? "";
let is_authenticating = false;


/** 每次进入或重试都重新申请免登码，避免重复使用已经消费的一次性 code。 */
async function authenticate_with_dingtalk(): Promise<void> {
  if (is_authenticating) {
    return;
  }

  is_authenticating = true;
  entry_state.value = "loading";
  error_message.value = "";

  try {
    const auth_code = await requestDingTalkAuthCode();
    const status = await exchangeDingTalkAuthCode(auth_code);
    entry_state.value = status.access_state;
  } catch (error) {
    entry_state.value = "error";
    error_message.value = error instanceof Error ? error.message : "身份验证暂时不可用，请稍后重试。";
  } finally {
    is_authenticating = false;
  }
}


onMounted(authenticate_with_dingtalk);
</script>

<template>
  <header class="test-notice" aria-label="测试站说明">
    <strong>个人技术展示 · 测试站</strong>
    <p>仅验证钉钉身份入口，不用于正式学校业务办理。</p>
  </header>
  <main v-if="entry_state !== 'ready'" class="identity-entry">
    <h1>中学校园综合智能体</h1>
    <p v-if="entry_state === 'loading'">正在验证钉钉身份……</p>
    <p v-else-if="entry_state === 'pending_configuration'">
      当前钉钉身份尚未配置系统权限，请联系系统管理员。
    </p>
    <template v-else>
      <p>{{ error_message }}</p>
      <button type="button" @click="authenticate_with_dingtalk">重新验证</button>
    </template>
  </main>
  <main v-else class="identity-entry">
    <p class="visually-hidden">身份验证完成。</p>
    <h1>中学校园综合智能体</h1>
    <p>当前暂无正式启用模块。</p>
  </main>
  <footer v-if="icp_filing_number.length > 0">
    <a
      href="https://beian.miit.gov.cn/"
      target="_blank"
      rel="noopener noreferrer"
    >
      {{ icp_filing_number }}
    </a>
  </footer>
</template>

<style scoped>
.test-notice {
  margin: 1rem auto;
  max-width: 42rem;
  padding: 1rem;
  border: 1px solid #9a6700;
  background: #fff8c5;
  color: #533f03;
  font-family: system-ui, sans-serif;
}

.identity-entry {
  margin: 4rem auto;
  max-width: 42rem;
  font-family: system-ui, sans-serif;
  line-height: 1.6;
}

footer {
  margin: 2rem auto;
  max-width: 390px;
  font-size: 0.875rem;
  text-align: center;
}

.visually-hidden {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
</style>
