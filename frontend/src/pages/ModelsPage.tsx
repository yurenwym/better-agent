import { useEffect, useRef, useState } from "react";
import { createModelProfile, createRoutingPolicy, listModelProfiles, listRoutingPolicies, resolveModelCapacity, verifyModelVersion } from "../api";
import type { ModelCapacityResolution, ModelProfileRecord, RoutingPolicy } from "../types";
import AppToast from "../components/AppToast";
import { localizedCode } from "../localization";

const protocolLabels={openai_compatible:"OpenAI 兼容",anthropic:"Anthropic",gemini:"Gemini"};
const routeRoles=["conversation","ask","planner","executor","reflector","researcher","expert","coordinator","judge_quality","judge_safety"] as const;
const capacityStatusLabels:Record<string,string>={verified:"容量已核实",unverified:"容量未核实",manual:"手动窗口",legacy:"旧版配置"};
const counterModeLabels:Record<string,string>={verified:"精确计数",estimate:"保守估算"};

export default function ModelsPage({csrfToken}:{csrfToken:string}){
  const [profiles,setProfiles]=useState<ModelProfileRecord[]>([]),[policies,setPolicies]=useState<RoutingPolicy[]>([]);
  const [loading,setLoading]=useState(true),[error,setError]=useState(""),[busy,setBusy]=useState(""),[notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const [form,setForm]=useState({name:"",provider_protocol:"openai_compatible",provider_name:"",base_url:"",model_name:"",credential_env_ref:"",working_window_mode:"auto" as "auto"|"manual",context_window:"",max_output_tokens:"8192",timeout_seconds:"60",max_attempts:"2",streaming:true,tool_calling:false,json_object:false});
  const [capacity,setCapacity]=useState<ModelCapacityResolution|null>(null),[capacityBusy,setCapacityBusy]=useState(false);
  const [policyName,setPolicyName]=useState(""),[routes,setRoutes]=useState<Record<string,{primary:string;fallback:string}>>(()=>Object.fromEntries(routeRoles.map(role=>[role,{primary:"",fallback:""}])));
  const load=()=>Promise.all([listModelProfiles(),listRoutingPolicies()]).then(([a,b])=>{setProfiles(a.profiles);setPolicies(b.policies);setError("");}).catch(e=>setError(e instanceof Error?e.message:"模型数据加载失败")).finally(()=>setLoading(false));
  useEffect(()=>{void load();},[]);
  async function verify(id:string){setBusy(id);try{await verifyModelVersion(id,csrfToken);await load();setNotice({message:"模型连接验证成功",tone:"success"});}catch(e){setNotice({message:e instanceof Error?e.message:"模型验证失败",tone:"error"});}finally{setBusy("");}}
  const capacityKey=JSON.stringify([form.base_url,form.provider_protocol,form.model_name]);
  const currentCapacityKey=useRef(capacityKey);currentCapacityKey.current=capacityKey;
  const querySequence=useRef(0);
  useEffect(()=>{querySequence.current++;setCapacity(null);setCapacityBusy(false);},[capacityKey]);
  async function lookupCapacity(){
    if(!form.base_url||!form.model_name){setCapacity(null);return;}
    const key=capacityKey,seq=++querySequence.current;setCapacityBusy(true);
    try{const result=await resolveModelCapacity(form.base_url,form.provider_protocol,form.model_name);if(seq===querySequence.current&&key===currentCapacityKey.current)setCapacity(result);}
    catch{if(seq===querySequence.current&&key===currentCapacityKey.current)setCapacity(null);}
    finally{if(seq===querySequence.current&&key===currentCapacityKey.current)setCapacityBusy(false);}
  }
  async function create(e:React.FormEvent){
    e.preventDefault();setBusy("create");
    try{
      const common={name:form.name,provider_protocol:form.provider_protocol,provider_name:form.provider_name,base_url:form.base_url,model_name:form.model_name,credential_env_ref:form.credential_env_ref,max_output_tokens:Number(form.max_output_tokens),timeout_seconds:Number(form.timeout_seconds),max_attempts:Number(form.max_attempts),capabilities:{text:true,streaming:form.streaming,tool_calling:form.tool_calling,json_object:form.json_object}};
      let payload:Record<string,unknown>;
      if(form.working_window_mode==="manual"){
        const window=Number(form.context_window);
        if(!Number.isFinite(window)||window<=0)throw new Error("手动模式需要填写正数工作窗口");
        payload={...common,context_window:window,soft_context_limit:window,working_window_mode:"manual"};
      }else{
        payload={...common,working_window_mode:"auto"};
      }
      await createModelProfile(payload,csrfToken);
      setForm({...form,name:"",provider_name:"",base_url:"",model_name:"",credential_env_ref:""});setCapacity(null);
      await load();setNotice({message:"模型档案已注册。请配置环境变量后验证连接。",tone:"success"});
    }catch(e){setNotice({message:e instanceof Error?e.message:"模型注册失败",tone:"error"});}finally{setBusy("");}
  }
  const versions=profiles.flatMap(item=>item.versions);
  async function createPolicy(e:React.FormEvent){e.preventDefault();setBusy("policy");try{const roles=Object.fromEntries(routeRoles.map(role=>[role,{primary:routes[role].primary,fallback:routes[role].fallback.split(",").map(x=>x.trim()).filter(Boolean)}]));await createRoutingPolicy({name:policyName,roles},csrfToken);setPolicyName("");await load();setNotice({message:"角色路由策略已保存为不可变版本",tone:"success"});}catch(e){setNotice({message:e instanceof Error?e.message:"路由策略保存失败",tone:"error"});}finally{setBusy("");}}
  return <section className="control-page" aria-busy={loading}>
    <header className="control-hero"><div><span className="eyebrow">模型管理</span><h2>模型控制台</h2><p>注册版本、检查凭据与能力，并查看角色路由。配置更新会创建不可变版本。</p></div><div className="control-hero-metrics"><strong>{profiles.length}</strong><span>模型档案</span><strong>{policies.length}</strong><span>路由策略</span></div></header>
    {error&&<div className="control-alert" role="alert">{error}<button className="button button-secondary" onClick={()=>void load()}>重试</button></div>}
    {!loading&&!error&&!profiles.length&&<div className="control-empty"><h3>还没有模型配置</h3><p>添加第一个供应商模型后，即可配置角色路由与真实评测。</p></div>}
    <div className="control-grid">
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">模型档案</span><h3>模型版本</h3></div><span className="control-count">{versions.length}</span></div><div className="control-list">
        {versions.map(v=><article className="control-row" key={v.id}><div className="control-row-main"><div className="control-row-title"><strong>{v.profile_name}</strong><span className={`status-chip status-${v.status.toLowerCase()}`}>v{v.version} · {v.status==="ACTIVE"?"启用":"停用"}</span></div><p>{v.provider_name} · {v.model_name}</p><div className="chip-row"><span className="data-chip">{protocolLabels[v.provider_protocol]}</span>{Object.entries(v.capabilities).filter(([,x])=>x).map(([k])=><span className="data-chip" key={k}>{k}</span>)}{v.working_window_mode&&<span className="data-chip">{v.working_window_mode==="auto"?"自动跟随":"手动窗口"} · {v.context_window}</span>}{v.model_context_limit?<span className="data-chip">官方容量 {v.model_context_limit}</span>:null}{v.capacity_status&&<span className="data-chip">{capacityStatusLabels[v.capacity_status]}</span>}{v.counter_mode&&<span className="data-chip">{counterModeLabels[v.counter_mode]}</span>}</div></div><div className="control-row-side"><span className={v.credential_configured?"signal-good":"signal-warn"}>{v.credential_configured?"凭据已配置":"缺少凭据"}</span><span>{v.verification_status==="VERIFIED"?"连接已验证":v.verification_status==="FAILED"?"验证失败":"尚未验证"}</span><button className="button button-secondary" disabled={busy===v.id||v.status!=="ACTIVE"} onClick={()=>void verify(v.id)}>{busy===v.id?"验证中…":"验证连接"}</button></div></article>)}
      </div></section>
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">角色路由</span><h3>角色路由</h3></div><span className="control-count">{policies.length}</span></div>{!policies.length?<div className="control-empty compact"><h3>尚未配置路由</h3><p>创建策略后，这里会显示每个 Agent 角色的主模型和显式备用模型。</p></div>:policies.map(p=><article className="policy-card" key={p.id}><strong>{p.name} · v{p.version}</strong>{Object.entries(p.roles).map(([role,route])=><div className="route-line" key={role}><span>{localizedCode(role, role)}</span><code>{versions.find(v=>v.id===route.primary)?.model_name??route.primary.slice(-8)}</code><small>{route.fallback.length?`+ ${route.fallback.length} 个备用模型`:"无备用模型"}</small></div>)}</article>)}</section>
    </div>
    <details className="control-disclosure control-create" open={!loading&&!error&&!profiles.length}><summary>注册模型档案</summary><p className="control-helper">这里只保存凭据的环境变量名称，不会收集或保存 API Key。工作窗口默认自动跟随已核实的模型容量，也可以手动限制为更小窗口。</p><form className="control-form" onSubmit={create}>
      <label>档案名称<input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label>
      <label>协议<select value={form.provider_protocol} onChange={e=>setForm({...form,provider_protocol:e.target.value})}><option value="openai_compatible">OpenAI 兼容</option><option value="anthropic">Anthropic</option><option value="gemini">Gemini</option></select></label>
      <label>供应商名称<input required value={form.provider_name} onChange={e=>setForm({...form,provider_name:e.target.value})}/></label>
      <label>接口地址<input required type="url" value={form.base_url} onChange={e=>setForm({...form,base_url:e.target.value})}/></label>
      <label>模型名称<input required value={form.model_name} onChange={e=>setForm({...form,model_name:e.target.value})}/></label>
      <label>凭据环境变量<input required pattern="[A-Za-z0-9_]+" value={form.credential_env_ref} onChange={e=>setForm({...form,credential_env_ref:e.target.value})}/></label>
      <label>工作窗口模式<select value={form.working_window_mode} onChange={e=>setForm({...form,working_window_mode:e.target.value as "auto"|"manual"})}><option value="auto">自动跟随已核实容量</option><option value="manual">手动上限</option></select></label>
      {form.working_window_mode==="manual"&&<label>手动工作窗口<input required type="number" min="1" value={form.context_window} onChange={e=>setForm({...form,context_window:e.target.value})}/></label>}
      <div className="control-helper"><button type="button" className="button button-secondary" onClick={()=>void lookupCapacity()} disabled={capacityBusy||!form.base_url||!form.model_name}>{capacityBusy?"查询中…":"查询容量"}</button>{" "}{capacity?<>官方容量：{capacity.entry?.context_limit??"未核实"} · 实际工作窗口：{capacity.capacity.effective_context_limit??"未确定"} · 核验：{capacityStatusLabels[capacity.capacity.status??""]??"未知"} · 计数：{counterModeLabels[capacity.capacity.counter_mode??""]??"未知"}</>:<span>尚未查询容量；保存时会自动按端点与模型解析。</span>}</div>
      <label>本次输出预算（Token）<input required type="number" min="1" value={form.max_output_tokens} onChange={e=>setForm({...form,max_output_tokens:e.target.value})}/></label>
      <label>超时秒数<input required type="number" min="1" value={form.timeout_seconds} onChange={e=>setForm({...form,timeout_seconds:e.target.value})}/></label>
      <label>最大尝试次数<input required type="number" min="1" value={form.max_attempts} onChange={e=>setForm({...form,max_attempts:e.target.value})}/></label>
      <fieldset className="capability-field"><legend>模型能力</legend>{([ ["streaming","流式输出"],["tool_calling","工具调用"],["json_object","结构化 JSON"] ] as const).map(([key,label])=><label key={key}><input type="checkbox" checked={form[key]} onChange={e=>setForm({...form,[key]:e.target.checked})}/>{label}</label>)}</fieldset>
      <button className="button button-primary control-form-action" disabled={!csrfToken||busy==="create"}>{busy==="create"?"注册中…":"注册模型档案"}</button>
    </form></details>
    <details className="control-disclosure control-create"><summary>创建角色路由</summary><p className="control-helper">每个角色必须选择满足其硬能力的主模型。备用模型使用模型版本 ID，以英文逗号分隔；仅在超时、限流或供应商不可用且尚未输出时启用。</p><form className="control-form route-policy-form" onSubmit={createPolicy}><label>策略名称<input required value={policyName} onChange={e=>setPolicyName(e.target.value)}/></label>{routeRoles.map(role=><fieldset className="route-field" key={role}><legend>{localizedCode(role, role)}</legend><label>主模型<select aria-label={`${localizedCode(role, role)} 主模型`} required value={routes[role].primary} onChange={e=>setRoutes({...routes,[role]:{...routes[role],primary:e.target.value}})}><option value="">请选择</option>{versions.filter(v=>v.status==="ACTIVE").map(v=><option key={v.id} value={v.id}>{v.profile_name} · {v.model_name}</option>)}</select></label><label>备用模型版本 ID<input aria-label={`${localizedCode(role, role)} 备用模型`} value={routes[role].fallback} onChange={e=>setRoutes({...routes,[role]:{...routes[role],fallback:e.target.value}})} placeholder="可留空，多个 ID 用逗号分隔"/></label></fieldset>)}<button className="button button-primary control-form-action" disabled={!csrfToken||busy==="policy"}>{busy==="policy"?"保存中…":"保存路由策略"}</button></form></details>
    {notice&&<AppToast {...notice} onDismiss={()=>setNotice(null)}/>}</section>;
}
