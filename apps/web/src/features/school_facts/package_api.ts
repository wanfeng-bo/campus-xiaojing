import type {DirectoryChange} from "./directory_contracts";
import { decodeSchoolFactSource, decodeSchoolFactConfirmation, decodeSchoolFactApply, decodeSchoolFactResult,
  type SchoolFactSource, type SchoolFactConfirmation, type SchoolFactApply, type SchoolFactResult } from "./school_fact_contracts";
import { decodePackagePlan, type PackageManifest, type PackagePlan } from "./package_contracts";
export type PackageFamily="directory"|"family"|"education"|"residence";
export interface PackageSelectedInput {family:PackageFamily;request:DirectoryChange}
export interface PackageApi {
  preflightSelected?(items:PackageSelectedInput[]):Promise<PackagePlan>;
  upload(file: File): Promise<SchoolFactSource>;
  discard(reference: string): Promise<void>;
  preflight(manifest: PackageManifest, sources: {source_slot:string;source_ref:string}[]): Promise<PackagePlan>;
  prepare(reference:string,key:string): Promise<SchoolFactConfirmation>;
  confirm(reference:string,credential:string,key:string): Promise<SchoolFactApply>;
  result(reference:string): Promise<SchoolFactResult>;
}
export class PackageApiError extends Error {
  constructor(readonly status: number) { super(status===401 || status===403 ? "当前身份或权限不可用，请重新核对。" : "资料包请求未能完成，请核对原操作结果或重新预检。"); }
}
export function readPackageFile(file:File):Promise<string> {
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new PackageApiError(0));reader.onload=()=>typeof reader.result==="string"?resolve(reader.result):reject(new PackageApiError(0));reader.readAsText(file);});
}
function base64(file:File):Promise<string> {
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new PackageApiError(0));reader.onload=()=>typeof reader.result==="string"?resolve(reader.result.slice(reader.result.indexOf(",")+1)):reject(new PackageApiError(0));reader.readAsDataURL(file);});
}
export function createPackageApi():PackageApi {
  const base="/api/v1/school-fact-product/imports";
  async function request<T>(path:string,method:string,decode:(v:unknown)=>T,body?:unknown,key?:string):Promise<T> {
    let response:Response;
    try {response=await fetch(base+path,{method,credentials:"same-origin",cache:"no-store",headers:{...(body===undefined?{}:{"Content-Type":"application/json"}),...(key?{"Idempotency-Key":key}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});}
    catch {throw new PackageApiError(0);}
    if(!response.ok)throw new PackageApiError(response.status);
    try {return decode(response.status===204?null:await response.json());}catch{throw new PackageApiError(200);}
  }
  const path=(ref:string)=>`/plans/${encodeURIComponent(ref)}`;
  return {
    upload:async file=>request("/sources","POST",decodeSchoolFactSource,{filename_summary:file.name,content_base64:await base64(file)}),
    discard:ref=>request(`/sources/${encodeURIComponent(ref)}`,"DELETE",()=>undefined),
    preflightSelected:items=>request("/plans/selected","POST",decodePackagePlan,{items}),
    preflight:(manifest,sources)=>request("/plans/package","POST",decodePackagePlan,{manifest,sources}),
    prepare:(ref,key)=>request(path(ref)+"/confirmation","POST",decodeSchoolFactConfirmation,undefined,key),
    confirm:(ref,credential,key)=>request(path(ref)+"/confirm","POST",decodeSchoolFactApply,{credential},key),
    result:ref=>request(path(ref)+"/result","GET",decodeSchoolFactResult),
  };
}
