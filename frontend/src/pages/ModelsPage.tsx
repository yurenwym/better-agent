import { useEffect, useState } from "react";
import { listModelProfiles, listRoutingPolicies, verifyModelVersion } from "../api";
import type { ModelProfileRecord, RoutingPolicy } from "../types";
import AppToast from "../components/AppToast";

const protocolLabels={openai_compatible:"OpenAI 兼容",anthropic:"Anthropic",gemini:"Gemini"};
export default function ModelsPage({csrfToken}:{csrfToken:string}){
  const [profiles,setProfiles]=useState<ModelProfileRecord[]>([]),[policies,setPolicies]=useState<RoutingPolicy[]>([]);
  const [loading,setLoading]=useState(true),[error,setError]=useState(""),[busy,setBusy]=useState(""),[notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const load=()=>Promise.all([listModelProfiles(),listRoutingPolicies()]).then(([a,b])=>{setProfiles(a.profiles);setPolicies(b.policies);setError("");}).catch(e=>setError(e instanceof Error?e.message:"模型数据加载失败")).finally(()=>setLoading(false));
  useEffect(()=>{void load();},[]);
  async function verify(id:string){setBusy(id);try{await verifyModelVersion(id,csrfToken);await load();setNotice({message:"模型连接验证成功",tone:"success"});}catch(e){setNotice({message:e instanceof Error?e.message:"模型验证失败",tone:"error"});}finally{setBusy("");}}
  const versions=profiles.flatMap(item=>item.versions);
  return <section className="control-page" aria-busy={loading}>
    <header className="control-hero"><div><span className="eyebrow">MODEL CONTROL</span><h2>模型控制台</h2><p>注册版本、检查凭据与能力，并查看角色路由。配置更新会创建不可变版本。</p></div><div className="control-hero-metrics"><strong>{profiles.length}</strong><span>模型档案</span><strong>{policies.length}</strong><span>路由策略</span></div></header>
    {error&&<div className="control-alert" role="alert">{error}<button className="button button-secondary" onClick={()=>void load()}>重试</button></div>}
    {!loading&&!error&&!profiles.length&&<div className="control-empty"><h3>还没有模型配置</h3><p>通过模型注册接口添加第一个供应商模型后，即可配置角色路由与真实评测。</p></div>}
    <div className="control-grid">
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">PROFILES</span><h3>模型版本</h3></div><span className="control-count">{versions.length}</span></div><div className="control-list">
        {versions.map(v=><article className="control-row" key={v.id}><div className="control-row-main"><div className="control-row-title"><strong>{v.profile_name}</strong><span className={`status-chip status-${v.status.toLowerCase()}`}>v{v.version} · {v.status==="ACTIVE"?"启用":"停用"}</span></div><p>{v.provider_name} · {v.model_name}</p><div className="chip-row"><span className="data-chip">{protocolLabels[v.provider_protocol]}</span>{Object.entries(v.capabilities).filter(([,x])=>x).map(([k])=><span className="data-chip" key={k}>{k}</span>)}</div></div><div className="control-row-side"><span className={v.credential_configured?"signal-good":"signal-warn"}>{v.credential_configured?"凭据已配置":"缺少凭据"}</span><span>{v.verification_status==="VERIFIED"?"连接已验证":v.verification_status==="FAILED"?"验证失败":"尚未验证"}</span><button className="button button-secondary" disabled={busy===v.id||v.status!=="ACTIVE"} onClick={()=>void verify(v.id)}>{busy===v.id?"验证中…":"验证连接"}</button></div></article>)}
      </div></section>
      <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">ROUTING</span><h3>角色路由</h3></div><span className="control-count">{policies.length}</span></div>{!policies.length?<div className="control-empty compact"><h3>尚未配置路由</h3><p>创建策略后，这里会显示每个 Agent 角色的主模型和显式 fallback。</p></div>:policies.map(p=><article className="policy-card" key={p.id}><strong>{p.name} · v{p.version}</strong>{Object.entries(p.roles).map(([role,route])=><div className="route-line" key={role}><span>{role}</span><code>{versions.find(v=>v.id===route.primary)?.model_name??route.primary.slice(-8)}</code><small>{route.fallback.length?`+ ${route.fallback.length} fallback`:"无 fallback"}</small></div>)}</article>)}</section>
    </div>{notice&&<AppToast {...notice} onDismiss={()=>setNotice(null)}/>}</section>;
}
