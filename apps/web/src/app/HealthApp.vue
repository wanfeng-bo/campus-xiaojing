<script setup lang="ts">
import { onMounted, ref } from "vue";

import { fetchHealth } from "./health";


type HealthState = "error" | "loading" | "ready";

const healthState = ref<HealthState>("loading");


/** 重新读取健康状态，并只向用户呈现可理解的加载、成功或重试结果。 */
async function refreshHealth(): Promise<void> {
  healthState.value = "loading";

  try {
    await fetchHealth();
    healthState.value = "ready";
  } catch {
    healthState.value = "error";
  }
}


onMounted(refreshHealth);
</script>

<template>
  <main>
    <h1>中学校园综合智能体</h1>
    <p v-if="healthState === 'loading'">正在检查后端服务……</p>
    <p v-else-if="healthState === 'ready'">服务和数据库连接正常。</p>
    <template v-else>
      <p>暂时无法连接服务，请稍后重试。</p>
      <button type="button" @click="refreshHealth">重新检查</button>
    </template>
  </main>
</template>

<style scoped>
main {
  margin: 4rem auto;
  max-width: 42rem;
  font-family: system-ui, sans-serif;
  line-height: 1.6;
}
</style>
