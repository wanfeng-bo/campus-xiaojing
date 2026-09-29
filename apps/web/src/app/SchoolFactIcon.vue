<script setup lang="ts">
defineProps<{ name: string }>();
</script>
<template>
  <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" focusable="false" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <template v-if="name === 'menu'"><path d="M5 8h22M5 16h14M5 24h22"/></template>
    <template v-else-if="name === 'plus'"><path d="M16 6v20M6 16h20"/></template>
    <template v-else-if="name === 'close'"><path d="m8 8 16 16M24 8 8 24"/></template>
    <template v-else-if="name === 'work'"><rect x="5" y="10" width="22" height="17" rx="4"/><path d="M11 10V7a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v3M5 17c7 4 15 4 22 0M14 18v3h4v-3"/></template>
    <template v-else-if="name === 'person'"><circle cx="16" cy="10" r="5" fill="currentColor" stroke="none"/><path d="M6 27v-3c0-5 4-8 10-8s10 3 10 8v3Z" fill="currentColor" stroke="none"/></template>
    <template v-else-if="name === 'people'"><circle cx="16" cy="10" r="4" fill="currentColor" stroke="none"/><circle cx="6" cy="13" r="3" fill="currentColor" opacity=".55" stroke="none"/><circle cx="26" cy="13" r="3" fill="currentColor" opacity=".55" stroke="none"/><path d="M9 26v-4c0-4 3-6 7-6s7 2 7 6v4ZM1 25v-4c0-3 3-5 6-5M31 25v-4c0-3-3-5-6-5" fill="currentColor" stroke-width="1"/></template>
    <template v-else-if="name === 'book'"><path d="M16 8C11 5 7 5 3 6v20c5-1 9 0 13 2 4-2 8-3 13-2V6c-4-1-8-1-13 2Z" fill="currentColor" opacity=".85" stroke="none"/><path d="M16 9v16" stroke="#fff"/></template>
    <template v-else-if="name === 'home'"><path d="m3 14 13-11 13 11M7 13v15h18V13"/><path d="M13 28V18h6v10" fill="currentColor" opacity=".65"/></template>
    <template v-else-if="name === 'chevron'"><path d="m12 7 9 9-9 9"/></template>
    <template v-else><rect x="7" y="3" width="18" height="26" rx="3" fill="currentColor" opacity=".85" stroke="none"/><path d="M12 10h8M12 16h8M12 22h5" stroke="#fff"/></template>
  </svg>
</template>
