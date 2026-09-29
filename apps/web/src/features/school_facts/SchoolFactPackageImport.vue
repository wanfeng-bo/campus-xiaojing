<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { readPackageFile, type PackageApi, type PackageSelectedInput, type PackageFamily } from "./package_api";
import { PACKAGE_MAX_BYTES, parsePackageManifest, type PackageManifest, type PackageValue } from "./package_contracts";
import DirectoryWorkbench from "./DirectoryWorkbench.vue";
import {createDirectoryApi, type DirectoryApi} from "./directory_api";
import type {DirectoryChange} from "./directory_contracts";
import { createPackageSession } from "./package_session";
const props=defineProps<{identityLabel:string;identityEpoch:number|string;identityInvalid?:boolean;api?:PackageApi;enabledFamilies?:PackageFamily[];directoryApis?:Partial<Record<PackageFamily,DirectoryApi>>}>();
const emit=defineEmits<{identityInvalid:[]}>();
const session=createPackageSession(props.api), state=session.state;
const selectedFamily=ref<PackageFamily>(props.enabledFamilies?.[0]??"directory");
const selectedDrafts=ref<(PackageSelectedInput&{label:string})[]>([]);
const familyLabels:Record<PackageFamily,string>={directory:"基础目录",family:"学生与家校",education:"教育与教学",residence:"寄宿资料"};
const formApis:Record<PackageFamily,DirectoryApi>={directory:props.directoryApis?.directory??createDirectoryApi("directory"),family:props.directoryApis?.family??createDirectoryApi("family"),education:props.directoryApis?.education??createDirectoryApi("education"),residence:props.directoryApis?.residence??createDirectoryApi("residence")};
function stageInput(request:DirectoryChange,label:string){
  if(inputLocked.value)return;
  if(selectedDrafts.value.length>=32){error.value="本包最多 32 份资料，请先处理当前草稿。";return;}
  selectedDrafts.value.push({family:selectedFamily.value,request:structuredClone(request),label});
  error.value="";inputKey.value++;
}
async function preflightSelected(){
  if(inputLocked.value||!selectedDrafts.value.length)return;
  error.value="";acknowledged.value=false;
  const items=selectedDrafts.value.map(({family,request})=>({family,request}));
  if(await session.preflightSelected(items)){selectedDrafts.value=[];inputKey.value++;locator.value=state.locator;}
}

const manifest=ref<PackageManifest|null>(null), files=ref<File[]>([]), slots=ref<Record<string,string>>({});
const error=ref(""), acknowledged=ref(false), replacementAcknowledged=ref(false), locator=ref("");
const inputKey=ref(0), page=ref(0), snapshotPage=ref(0), issuePage=ref(0), loadingManifest=ref(false);
let lifetime=0;
const locked=computed(()=>state.identityInvalid||state.busy||loadingManifest.value);
const inputLocked=computed(()=>locked.value||state.plan!==null||state.submitted);
const changes=computed(()=>state.plan?.changes.slice(page.value*20,page.value*20+20)??[]);
const snapshots=computed(()=>state.plan?.replacement_preview.slice(snapshotPage.value*20,snapshotPage.value*20+20)??[]);
const completePreviews=computed(()=>state.plan?.change_preview?.slice(page.value*20,page.value*20+20)??[]);
const issues=computed(()=>state.plan?.issues.slice(issuePage.value*20,issuePage.value*20+20)??[]);
function clearDraft() {lifetime++;selectedDrafts.value=[];manifest.value=null;files.value=[];slots.value={};error.value="";acknowledged.value=false;replacementAcknowledged.value=false;locator.value="";inputKey.value++;loadingManifest.value=false;page.value=0;snapshotPage.value=0;issuePage.value=0;}
function expire(){session.invalidate();clearDraft();}
watch(()=>props.identityInvalid,value=>{if(value)expire();},{immediate:true,flush:"sync"});
watch(()=>props.identityEpoch,expire,{flush:"sync"});
watch(()=>state.identityInvalid,value=>{if(value){clearDraft();emit("identityInvalid");}},{flush:"sync"});
async function chooseManifest(event:Event) {
  if(inputLocked.value)return;
  const file=(event.target as HTMLInputElement).files?.[0];
  manifest.value=null;slots.value={};replacementAcknowledged.value=false;error.value="";
  if(!file)return;
  if(file.size>PACKAGE_MAX_BYTES){error.value="清单文件过大，请核对资料包。";return;}
  const token=++lifetime;loadingManifest.value=true;
  try {const value=parsePackageManifest(JSON.parse(await readPackageFile(file)));if(token===lifetime&&!state.identityInvalid)manifest.value=value;}
  catch {if(token===lifetime)error.value="清单无法识别，请选择已批准的 school_facts_v5 JSON 清单。";}
  finally {if(token===lifetime)loadingManifest.value=false;}
}
function chooseFiles(event:Event) {
  if(inputLocked.value)return;
  const chosen=Array.from((event.target as HTMLInputElement).files??[]);
  slots.value={};error.value="";
  if(chosen.length>32||chosen.reduce((sum,f)=>sum+f.size,0)>PACKAGE_MAX_BYTES){files.value=[];error.value="来源最多 32 份、总计 10 MiB，请拆分为独立资料包。";return;}
  files.value=chosen;
}
async function preflight() {
  if(inputLocked.value||!manifest.value)return;
  if(manifest.value.mode==="replace"&&!replacementAcknowledged.value){error.value="请明确核对清单中的完整替换选集。";return;}
  const selected=manifest.value.sources.map(s=>files.value[Number(slots.value[s.logical_source_code])]);
  if(manifest.value.sources.some(s=>slots.value[s.logical_source_code]===undefined||slots.value[s.logical_source_code]==="")||selected.some(f=>!f)||new Set(selected).size!==selected.length||selected.length!==files.value.length){error.value="请把每份已选文件对应到清单来源，一份文件只能使用一次。";return;}
  error.value="";acknowledged.value=false;
  if(await session.preflight(manifest.value,selected)){files.value=[];inputKey.value++;locator.value=state.locator;}
}
function startNew(){if(session.reset())clearDraft();}
function format(value:PackageValue|undefined){return value===undefined?"未提供":value===null?"空值":typeof value==="object"?JSON.stringify(value,null,2):String(value);}
onUnmounted(()=>{session.dispose();clearDraft();});
</script>

<template>
  <section class="package-import" aria-label="多文件资料包导入">
    <h3>多文件资料包导入</h3>
    <p>当前身份：{{identityLabel}}。先核对已批准的资料包清单和原文件，再查看正式预检。</p>
    <p v-if="state.identityInvalid" role="alert">当前身份已失效，已清除本页资料与确认状态；请重新选择身份。</p>
    <fieldset v-if="enabledFamilies?.length" :disabled="inputLocked" aria-label="按获准对象准备资料包">
      <legend>从正式资料选择并填写</legend><p>按名称选择对象和需要修改的内容，引用自动携带。加入的是本页草稿；正式预检会重新核对身份、原值和关联影响，确认后才发布。</p>
      <label>资料范围<select v-model="selectedFamily" data-role="package-family"><option v-for="family in enabledFamilies" :key="family" :value="family">{{familyLabels[family]}}</option></select></label>
      <DirectoryWorkbench v-if="!inputLocked && enabledFamilies.includes(selectedFamily)" :key="`${identityEpoch}-${selectedFamily}-${inputKey}`" package-mode :identity-label="identityLabel" :api="formApis[selectedFamily]" :title="familyLabels[selectedFamily]" :education="selectedFamily==='education'" :batch="selectedFamily==='residence'" :residence-imports="selectedFamily==='residence'" @package-input="stageInput" @identity-invalid="expire" />
      <ol data-role="package-selected-draft"><li v-for="(draft,index) in selectedDrafts" :key="index">{{familyLabels[draft.family]}} · {{draft.label}}<button type="button" :disabled="inputLocked" @click="selectedDrafts.splice(index,1)">移出草稿</button></li></ol>
      <button type="button" data-action="package-selected-preflight" :disabled="inputLocked||!selectedDrafts.length" @click="preflightSelected">核对全部草稿与关联影响</button>
    </fieldset>
    <details><summary>导入已制作的标准文件或工程清单</summary>
    <fieldset :disabled="inputLocked">
      <legend>选择资料</legend>
      <label>已批准的资料包清单（JSON）<input :key="`manifest-${inputKey}`" data-role="package-manifest" type="file" accept=".json,application/json" @change="chooseManifest"></label>
      <label>来源文件（可多选 JSON、Excel 和制度正文）<input :key="`files-${inputKey}`" data-role="package-files" type="file" multiple accept=".json,.xlsx,.txt" @change="chooseFiles"></label>
      <p>来源最多 32 份，总计 10 MiB。清单中的学校、版本和映射来自已批准资料，页面不会替您补写。</p>
      <template v-if="manifest">
        <p>本包学校标识：{{manifest.school_id}}；核对版本：{{manifest.base_version}}。</p>
        <p data-role="package-mode">{{manifest.mode==='replace'?'完整替换明确选定的对象':'增量导入'}}。{{manifest.mode==='replace'?'选集中仍有效、但新输入未保留的对象将按预览结束。':'未提供的对象保持原状；文件齐全不代表学校登记完整。'}}</p>
        <label v-for="source in manifest.sources" :key="source.logical_source_code">{{source.logical_source_code}} · {{source.purpose==='policy_text'?'制度正文':'事实资料'}}（{{source.size_bytes}} 字节）
          <select v-model="slots[source.logical_source_code]" :data-slot="source.logical_source_code"><option value="">请选择对应文件</option><option v-for="(file,index) in files" :key="index" :value="String(index)">{{file.name}}（{{file.size}} 字节）</option></select>
          <small>对应事实：{{source.fact_kinds.join('、')||source.attached_policy_source_key}}；资料责任依据：{{source.responsibility_reference}}</small>
        </label>
        <section v-if="manifest.mode==='replace'" aria-label="显式完整替换选集">
          <h4>仅替换以下明确对象</h4>
          <details v-for="(scope,index) in manifest.replacement_scopes" :key="index"><summary>{{scope.fact_kind}} · {{scope.source_keys.length}} 个对象 · {{scope.effective_at}}</summary><pre>{{scope.source_keys.join('\n')}}</pre></details>
          <label><input v-model="replacementAcknowledged" type="checkbox">我已核对替换选集；不会把未列出的对象或未知资料当作应删除项。</label>
        </section>
      </template>
      <button data-action="package-preflight" type="button" :disabled="inputLocked||!manifest" @click="preflight">{{state.busy?'正在核对……':'上传并预检资料包'}}</button>
    </fieldset>
    </details>
    <p v-if="error||state.error" role="alert">{{error||state.error}}</p>
    <section v-if="state.plan" aria-label="资料包预检">
      <h4>{{state.plan.is_clean?'资料包预检通过':'资料包预检未通过'}}</h4>
      <p>原计划引用（可复制用于同登录会话核对结果）：{{state.locator}}</p>
      <p v-if="!state.canConfirm" role="status">当前预览尚未证明全部修改前后值与关联影响已完整核对，不能正式确认。请等待完整预览能力或使用现有领域维护入口。</p>
      <p v-if="state.plan.preview_issues?.length">预览核验事项：{{state.plan.preview_issues.join('、')}}</p>
      <section v-if="state.plan.change_preview?.length" aria-label="全部修改前后与关联影响">
        <h4>修改前后与关联影响（共 {{state.plan.change_preview.length}} 项）</h4>
        <article v-for="item in completePreviews" :key="`${item.fact_kind}:${item.source_key}`">
          <h5>{{item.fact_kind}} · {{item.source_key}} · {{item.operation}}</h5>
          <p>{{item.origin==='linked'?'关联修改':item.origin==='replacement_end'?'选集遗漏项的明确结束':'显式输入'}}；生效时点 {{item.effective_at}}；原版本 {{item.expected_version}}</p>
          <table><thead><tr><th>修改前</th><th>修改后</th></tr></thead><tbody><tr><td><pre>{{format(item.before)}}</pre></td><td><pre>{{format(item.after)}}</pre></td></tr></tbody></table>
          <section v-if="item.before_segments.length||item.after_segments.length"><h6>完整期间</h6><p>修改前</p><pre>{{format(item.before_segments)}}</pre><p>修改后</p><pre>{{format(item.after_segments)}}</pre></section>
          <section v-if="item.domain_preview"><h6>领域影响核对</h6><pre>{{format(item.domain_preview)}}</pre></section>
        </article>
      </section>
      <details v-if="state.plan.issues.length"><summary>查看 {{state.plan.issues.length}} 项预检问题</summary><ul><li v-for="(issue,index) in issues" :key="index">{{issue.source_slot}} / {{issue.sheet_code}} 第 {{issue.row_number}} 行 / {{issue.column_code}}：{{issue.error_code}}</li></ul><button type="button" :disabled="issuePage===0" @click="issuePage--">上一页</button><span>第 {{issuePage+1}} 页</span><button type="button" :disabled="(issuePage+1)*20>=state.plan.issues.length" @click="issuePage++">下一页</button></details>
      <h4>本次提出的修改（共 {{state.plan.changes.length}} 项）</h4>
      <article v-for="change in changes" :key="`${change.fact_kind}:${change.source_key}`">
        <h5>{{change.fact_kind}} · {{change.source_key}} · {{change.operation}}</h5><p>生效时点：{{change.effective_at}}；原版本：{{change.expected_version}}</p>
        <dl><template v-for="field in change.fields" :key="field.field_code"><dt>{{field.field_code}}</dt><dd><pre>{{format(field.new_value)}}</pre></dd></template></dl>
        <section v-for="(segment,index) in change.replacement_segments" :key="index"><p>调整后的期间：{{segment.effective_at}} 至 {{segment.valid_to||'未指定结束'}}</p><dl><template v-for="field in segment.fields" :key="field.field_code"><dt>{{field.field_code}}</dt><dd><pre>{{format(field.new_value)}}</pre></dd></template></dl></section>
      </article>
      <nav aria-label="修改分页"><button type="button" :disabled="page===0" @click="page--">上一页</button>第 {{page+1}} / {{Math.ceil(state.plan.changes.length/20)}} 页<button type="button" :disabled="(page+1)*20>=state.plan.changes.length" @click="page++">下一页</button></nav>
      <section v-if="state.plan.replacement_preview.length" aria-label="获准读取的选集原值"><h4>选集原值（不代表全部关联影响）</h4><article v-for="item in snapshots" :key="`${item.fact_kind}:${item.source_key}`"><h5>{{item.fact_kind}} · {{item.source_key}}</h5><p>原状态：{{item.state}}；本次动作：{{item.selection_action}}</p><pre>{{format(item.before)}}</pre></article><button type="button" :disabled="snapshotPage===0" @click="snapshotPage--">上一页</button><span>第 {{snapshotPage+1}} 页</span><button type="button" :disabled="(snapshotPage+1)*20>=state.plan.replacement_preview.length" @click="snapshotPage++">下一页</button></section>
      <template v-if="state.canConfirm&&!state.submitted"><label><input v-model="acknowledged" type="checkbox" :disabled="locked">我已核对当前身份、全部修改前后值、完整正文及关联影响。</label><button v-if="!state.ready" type="button" :disabled="locked||!acknowledged" @click="session.prepare">准备正式确认</button><button v-else data-action="package-confirm" type="button" :disabled="locked||!acknowledged" @click="session.confirm(acknowledged)">确认并正式发布本包</button></template>
    </section>
    <section aria-label="查询原资料包回执"><h4>核对原操作结果</h4><label>原计划引用<input v-model="locator" type="text" maxlength="128" :disabled="locked||state.submitted"></label><button type="button" :disabled="locked||!locator" @click="session.recover(locator.trim())">只查询原回执</button><p>同一登录会话内按原引用查询；换登录后的本人历史回执请使用现有回执入口。结果未查清时，不会重新提交。</p><p v-if="state.result">{{state.result.state==='applied'?'原操作已正式完成':'尚未查到已完成的原操作'}}<span v-if="state.result.state==='applied'">；版本 {{state.result.target_version}}；实际修改 {{state.result.changed_count}} 项</span></p></section>
    <button v-if="state.plan||state.result" type="button" :disabled="locked||(state.submitted&&state.result?.state!=='applied')" @click="startNew">清除此页，准备另一资料包</button>
  </section>
</template>
<style scoped>
.package-import{border-top:1px solid #bed1d0;margin-top:1.25rem;padding-top:1rem}label{display:block;margin:.75rem 0}input,select{display:block;max-width:100%;margin-top:.35rem}input[type=checkbox]{display:inline}small{display:block;color:#47635f}fieldset{border:1px solid #ccd9d7;border-radius:.5rem;padding:1rem}button{margin:.5rem .5rem .5rem 0}article{border-top:1px solid #dce5e3}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;max-height:30rem;overflow:auto}dd{margin-left:1rem}h5{font-size:1rem}nav{display:flex;align-items:center;gap:.5rem}
table{width:100%;table-layout:fixed;border-collapse:collapse}th,td{border:1px solid #dce5e3;padding:.5rem;vertical-align:top;text-align:left}
</style>
