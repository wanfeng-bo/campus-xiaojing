<script setup lang="ts">
import { useId } from "vue";
const id = useId();
defineProps<{ orbit?: boolean }>();
</script>
<template>
  <div class="xiaojing-brand" :class="{ orbit }" data-role="xiaojing-brand" aria-hidden="true">
    <span v-if="orbit" class="orbital orbital-outer"></span><span v-if="orbit" class="orbital orbital-inner"></span>
    <span class="brand-disc">
      <svg viewBox="0 0 160 160" focusable="false">
        <defs>
          <linearGradient :id="`${id}-book`" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#72d8ff"/><stop offset=".45" stop-color="#4384ff"/><stop offset="1" stop-color="#0870ff"/></linearGradient>
          <linearGradient :id="`${id}-spark`" x1="0" y1="1" x2="1" y2="0"><stop stop-color="#287bff"/><stop offset="1" stop-color="#10e5e8"/></linearGradient>
        </defs>
        <path d="M27 45C35 27 75 24 94 29c13 3 16 9 8 16l-11 7c-7 5-5 12 5 15l15 4c9 2 10 9 10 19v26c0 12-11 17-26 18l-15 10-22-12H37l-16 8c-5 2-9-2-7-7l6-18c-8-19-6-52 7-70Z" :fill="`url(#${id}-book)`"/>
        <path d="M133 34l5 15 16 6-15 7-6 17-6-17-16-7 16-6Z" :fill="`url(#${id}-spark)`"/>
        <path d="M137 91v32a9 9 0 0 1-9 9H103" fill="none" stroke="#18cee9" stroke-width="10" stroke-linecap="round"/>
        <path d="M39 74c13-2 29 3 41 17 9-13 21-18 34-17v42c-13-1-24 4-34 12-13-9-27-13-41-12Z" fill="#fff"/>
        <path d="M80 91v36" fill="none" stroke="#bce9ff" stroke-width="2"/>
        <circle cx="55" cy="53" r="3.3" fill="#c9f7ff"/><circle cx="76" cy="47" r="2.2" fill="#fff"/>
      </svg>
    </span>
  </div>
</template>
<style scoped>
.xiaojing-brand { width: 3rem; aspect-ratio: 1; position: relative; display: grid; place-items: center; flex: 0 0 auto; }
.brand-disc { display: grid; place-items: center; width: 100%; aspect-ratio: 1; border: 1px solid #ffffffed; border-radius: 50%; background: linear-gradient(145deg, #f9feffed, #dbf1ffbe); box-shadow: 0 8px 22px #429aef20, inset 0 2px 4px #fff; }
svg { width: 82%; height: 82%; filter: drop-shadow(0 5px 3px #408bdf30); }
.orbit { width: clamp(11rem, 30vw, 16.3rem); padding: 2rem; box-sizing: border-box; }
.orbital { position: absolute; inset: 0; border: 1px solid #97d6ff6e; border-radius: 50%; }
.orbital-inner { inset: 1rem; border-color: #ffffffe6; }
.orbital-outer:before,.orbital-outer:after { content: ''; width: .5rem; height: .5rem; background: #2a84ff; position: absolute; border-radius: 50%; top: 55%; left: -.23rem; }
.orbital-outer:after { left: auto; right: .48rem; top: 22%; width: .4rem; height: .4rem; background: #45c9f8; }
@media(max-width: 480px) { .orbit { width: 12rem; padding: 1.7rem; } }
</style>
