import { shallowReactive } from "vue";
import { createPackageApi, PackageApiError, type PackageApi, type PackageSelectedInput } from "./package_api";
import { hasCompletePackageReview, packageRef, parsePackageManifest, type PackageManifest, type PackagePlan } from "./package_contracts";
import type { SchoolFactConfirmation, SchoolFactResult } from "./school_fact_contracts";

export function createPackageSession(api:PackageApi=createPackageApi()) {
  const state=shallowReactive({plan:null as PackagePlan|null,result:null as SchoolFactResult|null,
    locator:"",busy:false,identityInvalid:false,submitted:false,ready:false,canConfirm:false,error:""});
  let epoch=0, credential:SchoolFactConfirmation|null=null, prepareKey:string|null=null;
  function invalidate() {
    epoch++; credential=null; prepareKey=null;
    Object.assign(state,{plan:null,result:null,locator:"",busy:false,identityInvalid:true,submitted:false,ready:false,canConfirm:false,error:""});
  }
  function current(token:number) {return token===epoch && !state.identityInvalid;}
  function available() {return !state.busy && !state.identityInvalid;}
  function failure(error:unknown,token:number) {
    if(!current(token))return;
    if(error instanceof PackageApiError && [401,403].includes(error.status)) {invalidate();return;}
    state.error=error instanceof PackageApiError?error.message:"资料包核验未完成，请核对清单、文件和当前权限。";
  }
  return {state,invalidate,dispose:invalidate,
    reset():boolean {
      if(!available() || state.submitted && state.result?.state!=="applied")return false;
      epoch++;credential=null;prepareKey=null;
      Object.assign(state,{plan:null,result:null,locator:"",submitted:false,ready:false,canConfirm:false,error:""});return true;
    },
    async preflight(input:PackageManifest,files:File[]):Promise<boolean> {
      if(!available() || state.submitted && state.result?.state!=="applied")return false;
      const token=epoch;state.busy=true;state.error="";credential=null;prepareKey=null;
      Object.assign(state,{plan:null,result:null,locator:"",ready:false,canConfirm:false,submitted:false});
      const uploaded:string[]=[];
      try {
        const manifest=parsePackageManifest(input);
        if(files.length!==manifest.sources.length || new Set(files).size!==files.length || files.some((f,i)=>f.size!==manifest.sources[i].size_bytes || !/\.(json|xlsx|txt)$/i.test(f.name)))throw new Error("source mismatch");
        const sources=[];
        for(let i=0;i<files.length;i++) {
          if(!current(token))return false;
          const source=await api.upload(files[i]);
          if(!current(token))return false;
          uploaded.push(source.source_ref);
          if(source.source_sha256!==manifest.sources[i].source_sha256)throw new Error("source digest mismatch");
          sources.push({source_slot:manifest.sources[i].logical_source_code,source_ref:source.source_ref});
        }
        const plan=await api.preflight(manifest,sources);
        if(!current(token))return false;
        state.plan=plan;state.locator=plan.plan_ref;
        state.canConfirm=plan.is_clean && hasCompletePackageReview(plan);
        return true;
      } catch(error) {
        failure(error,token);
        // Never send cleanup using a newly selected identity's cookies.
        for(const ref of uploaded) {
          if(!current(token))break;
          try {await api.discard(ref);}catch{/* The private source expires server-side. */}
        }
        return false;
      } finally {if(current(token))state.busy=false;}
    },
    async preflightSelected(items:PackageSelectedInput[]):Promise<boolean> {
      if(!available() || state.submitted && state.result?.state!=="applied")return false;
      const token=epoch;state.busy=true;state.error="";credential=null;prepareKey=null;
      Object.assign(state,{plan:null,result:null,locator:"",ready:false,canConfirm:false,submitted:false});
      try {
        if(!api.preflightSelected || !items.length || items.length>32)throw new Error("selected input unavailable");
        const plan=await api.preflightSelected(JSON.parse(JSON.stringify(items)));
        if(!current(token))return false;
        state.plan=plan;state.locator=plan.plan_ref;
        state.canConfirm=plan.is_clean&&hasCompletePackageReview(plan);return true;
      }catch(error){failure(error,token);return false;}
      finally{if(current(token))state.busy=false;}
    },
    async prepare():Promise<boolean> {
      const plan=state.plan;
      if(!available() || state.submitted || !state.canConfirm || !plan || !hasCompletePackageReview(plan))return false;
      const token=epoch;state.busy=true;state.error="";
      prepareKey??=crypto.randomUUID();
      try {
        const candidate=await api.prepare(plan.plan_ref,prepareKey);
        if(!current(token))return false;
        if(candidate.plan_ref!==plan.plan_ref || candidate.plan_sha256!==plan.plan_sha256 || Date.parse(candidate.expires_at)<=Date.now())throw new Error("confirmation mismatch");
        credential=candidate;state.ready=true;prepareKey=null;return true;
      } catch(error){failure(error,token);return false;}
      finally {if(current(token))state.busy=false;}
    },
    async confirm(acknowledged:boolean):Promise<boolean> {
      const plan=state.plan, selected=credential;
      if(!available() || !acknowledged || state.submitted || !state.canConfirm || !plan || !selected || !hasCompletePackageReview(plan))return false;
      if(Date.parse(selected.expires_at)<=Date.now()){credential=null;state.ready=false;state.error="确认已过期，请重新准备。";return false;}
      const token=epoch;credential=null;state.ready=false;state.submitted=true;state.busy=true;state.error="";
      try {
        const result=await api.confirm(plan.plan_ref,selected.credential,crypto.randomUUID());
        if(!current(token))return false;
        if(result.plan_ref!==plan.plan_ref)throw new Error("result mismatch");
        state.result={...result,state:"applied"};return true;
      } catch(error){failure(error,token);if(current(token))state.error="提交结果尚未确认，请只查询原操作回执，不要重新提交。";return false;}
      finally {if(current(token))state.busy=false;}
    },
    async recover(reference:string=state.locator):Promise<boolean> {
      if(!available() || !packageRef(reference) || state.submitted && state.locator!==reference)return false;
      const token=epoch;state.busy=true;state.submitted=true;state.ready=false;credential=null;state.locator=reference;state.error="";
      try {
        const result=await api.result(reference);
        if(!current(token))return false;
        if(result.plan_ref!==reference)throw new Error("result mismatch");
        state.result=result;return true;
      } catch(error){failure(error,token);return false;}
      finally {if(current(token))state.busy=false;}
    },
  };
}
