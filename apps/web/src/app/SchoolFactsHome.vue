<script setup lang="ts">
import XiaojingBrand from "./XiaojingBrand.vue";
import SchoolFactIcon from "./SchoolFactIcon.vue";
defineProps<{ shortcuts: {key:string; title:string; description:string; icon:string}[]; assistantEnabled:boolean; inactive?:boolean }>();
const emit = defineEmits<{ select:[key:string] }>();
</script>
<template>
  <section class="welcome-home" aria-label="小静首页">
    <XiaojingBrand orbit />
    <h1>你好，我是小静</h1>
    <p class="welcome-line">{{ inactive ? '欢迎来到你的校园工作空间' : assistantEnabled ? '今天想帮你处理什么校园事务？' : '学校资料，随时查阅' }}</p>
    <div v-if="shortcuts.length" class="shortcut-grid">
      <button v-for="item in shortcuts" :key="item.key" type="button" data-role="home-shortcut" :data-area="item.key" @click="emit('select',item.key)">
        <span class="shortcut-icon"><SchoolFactIcon :name="item.icon" /></span>
        <span class="shortcut-copy"><strong>{{item.title}}</strong><span>{{item.description}}</span></span>
        <SchoolFactIcon name="chevron" class="shortcut-chevron" />
      </button>
    </div>
    <div class="campus-scene" aria-hidden="true">
      <p>让校园生活<br><span>更简单、更美好</span></p>
      <svg viewBox="0 0 420 200" fill="none" focusable="false">
        <path d="M0 181c93-30 196-11 250-32 59-23 111-29 170-18v69H0Z" fill="#b6e2ff" opacity=".3"/>
        <path d="M37 129h143v48H37zM232 97h151v80H232z" fill="#c6e8ff"/>
        <path d="m22 129 38-21h120v21ZM237 98l18-20h106l28 20Z" fill="#a9d7f4"/>
        <path d="M171 51h72v127h-72zM177 36h60v15h-60zM185 24h44v12h-44z" fill="#b3dcf7"/>
        <path d="M182 54h50v122h-50z" fill="#d6efff"/><path d="M188 3h5v21h-5z" fill="#add5ee"/><path d="m193 4 29 4-29 9Z" fill="#ccecff"/>
        <circle cx="207" cy="78" r="18" fill="#effaff" stroke="#b0d9f4" stroke-width="3"/><path d="M207 65v14l-8 5" stroke="#9fd2f5" stroke-width="3"/>
        <path d="M198 145h18v33h-18zM193 113h10v18h-10zM211 113h10v18h-10z" fill="#a6d3f2"/>
        <path d="M48 139h12v12H48zM70 139h12v12H70zM92 139h12v12H92zM114 139h12v12h-12zM136 139h12v12h-12zM48 158h12v12H48zM70 158h12v12H70zM92 158h12v12H92zM114 158h12v12h-12zM136 158h12v12h-12zM254 111h14v17h-14zM280 111h14v17h-14zM306 111h14v17h-14zM332 111h14v17h-14zM254 139h14v17h-14zM280 139h14v17h-14zM306 139h14v17h-14zM332 139h14v17h-14z" fill="#a1cff0"/>
        <path d="M22 171V143M392 173v-36" stroke="#a5d3ed" stroke-width="4"/><path d="M22 149c-26-1-20-18-13-20-5-13 14-21 21-11 13-7 24 7 14 15 6 10-9 18-22 16ZM392 144c-19 5-27-13-17-22-5-15 15-25 25-13 18-6 30 17 14 23-1 9-10 13-22 12Z" fill="#bee6fb"/>
        <path d="M203 181c-63 6-106 15-119 19M221 181c-23 7-41 13-51 19" stroke="#fff" stroke-width="3" opacity=".8"/>
      </svg>
    </div>
  </section>
</template>
<style scoped>
.welcome-home { display: flex; flex-direction: column; align-items: center; margin: 0 auto; width: min(100%, 52rem); padding: .5rem 0 0; text-align: center; color: #111b55; }
.welcome-home :deep(.orbit) { width: clamp(9.5rem, 22vh, 12rem); padding: 1.6rem; }
h1 { font-size: clamp(1.85rem,5vw,2.4rem); letter-spacing: .035em; margin: .9rem 0 .4rem; font-weight: 700; }
.welcome-line { color: #63719f; font-size: clamp(.98rem,2.6vw,1.24rem); margin: 0 0 1.4rem; line-height: 1.7; }
.shortcut-grid { display: grid; grid-template-columns: 1fr 1fr; width: 100%; gap: 1rem; text-align: left; }
.shortcut-grid button { width: 100%; display: flex; align-items: center; gap: .9rem; min-height: 5.5rem; padding: 1.2rem; background: linear-gradient(125deg,#ffffffe8,#ffffff8c); color: #111b55; border: 1.5px solid #fff; border-radius: 1.4rem; box-shadow: 0 10px 30px #6dc0ee0d; cursor: pointer; font: inherit; text-align: left; transition: transform .16s, box-shadow .16s; }
.shortcut-grid button:hover { transform: translateY(-2px); box-shadow: 0 8px 25px #5ea9e620; }
.shortcut-grid button:focus-visible { outline: 3px solid #2488ff; outline-offset: 3px; }
.shortcut-icon { display: grid; place-items: center; width: 3.65rem; height: 3.65rem; flex: 0 0 auto; background: linear-gradient(140deg,#dcf4ff,#eaf8ff); border-radius: 50%; color: #1786ff; }
.shortcut-icon svg { width: 2rem; height: 2rem; filter: drop-shadow(0 3px 3px #237bff24); }
.shortcut-copy { display: grid; gap: .3rem; min-width: 0; }
.shortcut-copy strong { font-size: 1.15rem; font-weight: 650; }
.shortcut-copy > span { font-size: .86rem; color: #7583ad; line-height: 1.5; }
.shortcut-chevron { width: 1.2rem; height: 1.2rem; flex: 0 0 auto; margin-left: auto; color: #7b8db7; }
.campus-scene { display: flex; align-items: flex-end; width: 100%; height: clamp(5.5rem,13vh,7.5rem); margin-top: .7rem; overflow: hidden; }
.campus-scene p { color: #79ade0; font-family: KaiTi, STKaiti, serif; font-size: clamp(1.1rem,2.8vw,1.65rem); transform: rotate(-7deg); letter-spacing: .22em; width: 48%; white-space: nowrap; padding-bottom: .7rem; line-height: 1.8; }
.campus-scene p span { padding-left: 1.4rem; }
.campus-scene svg { height: 100%; width: 58%; margin-left: -6%; opacity: .77; }
@media(max-width: 600px) { .shortcut-grid {gap: .65rem;} .shortcut-grid button {min-height: 5.1rem; padding: .8rem .65rem; gap: .55rem; border-radius: 1.2rem;} .shortcut-icon {width: 2.65rem; height: 2.65rem;} .shortcut-icon svg{width:1.65rem;height:1.65rem;} .shortcut-copy strong{font-size: .98rem;} .shortcut-copy > span{font-size: .74rem;} .shortcut-chevron{width:.8rem;height:.8rem;} .welcome-line{margin-bottom:1.1rem;} .campus-scene {height: 5.5rem;} .welcome-home :deep(.orbit) {width:9.5rem;} }
@media(max-width: 350px) { .shortcut-grid {grid-template-columns: 1fr;} }
@media(prefers-reduced-motion: reduce) { .shortcut-grid button {transition: none;} }
</style>
