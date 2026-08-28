import { useEffect, useState } from "react";
import { createModelProfile, listModelProfiles, listRoutingPolicies, verifyModelVersion } from "../api";
import type { ModelProfileRecord, RoutingPolicy } from "../types";
import AppToast from "../components/AppToast";

const protocolLabels={openai_compatible:"OpenAI 兼容",anthropic:"Anthropic",gemini:"Gemini"};
export default function ModelsPage({csrfToken}:{csrfToken:string}){
  const [profiles,setProfiles]=useState<ModelProfileRecord[]>([]),[policies,setPolicies]=useState<RoutingPolicy[]>([]);
  const [loading,setLoading]=useState(true),[error,setError]=useState(""),[busy,setBusy]=useState(""),[notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const [form,setForm]=useState({name:"",provider_protocol:"openai_compatible",provider_name:"",base_url:"",model_name:"",credential_env_ref:"",context_window:"32768",max_output_tokens:"4096",timeout_seconds:"60",max_attempts:"2",streaming:true,tool_calling:false,json_object:false});
  const load=()=>Promise.all([listModelProfiles(),listRoutingPolicies()]).then(([a,b])=>{setProfiles(a.profiles);setPolicies(b.policies);setError("");}).catch(e=>setError(e instanceof Error?e.message:"模型数据加载失败")).finally(()=>setLoading(false));
  useEffect(()=>{void load();},[]);
  async function verify(id:string){setBusy(id);try{await verifyModelVersion(id,csrfToken);await load();setNotice({message:"模型连接验证成功",tone:"success"});}catch(e){setNotice({message:e instanceof Error?e.message:"模型验证失败",tone:"error"});}finally{setBusy("");}}
  async function create(e:React.FormEvent){e.preventDefault();setBusy("create");try{await createModelProfile({...form,context_window:Number(form.context_window),max_output_tokens:Number(form.max_output_tokens),timeout_seconds:Number(form.timeout_seconds),max_attempts:Number(form.max_attempts),capabilities:{text:true,streaming:form.streaming,tool_calling:form.tool_calling,json_object:form.json_object}},csrfToken);setForm({...form,name:"",provider_name:"",base_url:"",model_name:"",credential_env_ref:""});await load();setNotice({message:"模型档案已注册。请配置环境变量后验证连接。",tone:"success"});}catch(e){setNotice({message:e instanceof Error?e.message:"模型注册失败",tone:"error"});}finally{setBusy("");}}
  const versions=profiles.flatMap(item=>item.versions);
  return <section className="control-page" aria-busy={loading}>
    <header className="control-hero"><div><span className="eyebrow">MODEL CONTROL</span><h2>模型控制台</h2><p>注册版本、检查凭据与能力，并查看角色路由。配置更新会创建不可变版本。</p></div><div className="control-hero-metrics"><strong>{profiles.length}</strong><span>模型档案</span><strong>{policies.length}</strong><span>路由策略</span></div></header>
    {error&&<div className="control-alert" role="alert">{error}<button className="button button-secondary" onClick={()=>void load()}>重试</button></div>}
    <section className="control-panel control-create"><div className="control-panel-head"><div><span className="eyebrow">REGISTER</span><h3>注册模型档案</h3></div></div><p className="control-helper">这里只保存凭据的环境变量名称，不会收集或保存 API Key。</p><form className="control-form" onSubmit={create}>
      <label>档案名称<input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label>
      <label>协议<select value={form.provider_protocol} onChange={e=>setForm({...form,provider_protocol:e.target.value})}><option value="openai_compatible">OpenAI 兼容</option><option value="anthropic">Anthropic</option><option value="gemini">Gemini</option></select></label>
      <label>供应商名称<input required value={form.provider_name} onChange={e=>setForm({...form,provider_name:e.target.value})}/></label>
      <label>接口地址<input required type="url" value={form.base_url} onChange={e=>setForm({...form,base_url:e.target.value})}/></label>
      <label>模型名称<input required value={form.model_name} onChange={e=>setForm({...form,model_name:e.target.value})}/></label>
      <label>凭据环境变量<input required pattern="[A-Za-z0-9_]+" value={form.credential_env_ref} onChange={e=>setForm({...form,credential_env_ref:e.target.value})}/></label>
      <label>上下文窗口<input required type="number" min="1" value={form.context_window} onChange={e=>setForm({...form,context_window:e.target.value})}/></label>
      <label>最大输出 Token<input required type="number" min="1" value={form.max_output_tokens} onChange={e=>setForm({...form,max_output_tokens:e.target.value})}/></label>
      <label>超时秒数<input required type="number" min="1" value={form.timeout_seconds} onChange={e=>setForm({...form,timeout_seconds:e.target.value})}/></label>
      <label>最大尝试次数<input required type="number" min="1" value={form.max_attempts} onChange={e=>setForm({...form,max_attempts:e.target.value})}/></label>
      <fieldset className="capability-field"><legend>模型能力</legend>{([ ["streaming","流式输出"],["tool_calling","工具调用"],["json_object","结构化 JSON"] ] as const).map(([key,label])=><label key={key}><input type="checkbox" checked={form[key]} onChange={e=>setForm({...form,[key]:e.target.checked})}/>{label}</label>)}</fieldset>
      <button className="button button-primary control-form-action" disabled={!csrfToken||busy==="create"}>{busy==="create"?"注册中…":"注册模型档案"}</button>
    </form></section>
    {!loading&&!error&&!profiles.length&&<div className="control-empty"><h3>还没有模型配置</h3><p>通过模型注册接口添加第一个供应商模型后，即可配置角色路由与真实评测。</p></div>}
    <div className="control-grid">
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">PROFILES</span><h3>模型版本</h3></div><span className="control-count">{versions.length}</span></div><div className="control-list">
        {versions.map(v=><article className="control-row" key={v.id}><div className="control-row-main"><div className="control-row-title"><strong>{v.profile_name}</strong><span className={`status-chip status-${v.status.toLowerCase()}`}>v{v.version} · {v.status==="ACTIVE"?"启用":"停用"}</span></div><p>{v.provider_name} · {v.model_name}</p><div className="chip-row"><span className="data-chip">{protocolLabels[v.provider_protocol]}</span>{Object.entries(v.capabilities).filter(([,x])=>x).map(([k])=><span className="data-chip" key={k}>{k}</span>)}</div></div><div className="control-row-side"><span className={v.credential_configured?"signal-good":"signal-warn"}>{v.credential_configured?"凭据已配置":"缺少凭据"}</span><span>{v.verification_status==="VERIFIED"?"连接已验证":v.verification_status==="FAILED"?"验证失败":"尚未验证"}</span><button className="button button-secondary" disabled={busy===v.id||v.status!=="ACTIVE"} onClick={()=>void verify(v.id)}>{busy===v.id?"验证中…":"验证连接"}</button></div></article>)}
      </div></section>
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">ROUTING</span><h3>角色路由</h3></div><span className="control-count">{policies.length}</span></div>{!policies.length?<div className="control-empty compact"><h3>尚未配置路由</h3><p>创建策略后，这里会显示每个 Agent 角色的主模型和显式 fallback。</p></div>:policies.map(p=><article className="policy-card" key={p.id}><strong>{p.name} · v{p.version}</strong>{Object.entries(p.roles).map(([role,route])=><div className="route-line" key={role}><span>{role}</span><code>{versions.find(v=>v.id===route.primary)?.model_name??route.primary.slice(-8)}</code><small>{route.fallback.length?`+ ${route.fallback.length} fallback`:"无 fallback"}</small></div>)}</article>)}</section>
    </div>{notice&&<AppToast {...notice} onDismiss={()=>setNotice(null)}/>}</section>;
}
