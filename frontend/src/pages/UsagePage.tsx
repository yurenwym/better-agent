import { useEffect, useState } from "react";
import { getCostSummary, getUsageSummary } from "../api";
import type { CostSummary, UsageGroup } from "../types";
import { localizedCode } from "../localization";

const money=(v:number)=>`$${(v/1_000_000).toFixed(6)}`;
const periodKey=(kind:string)=>kind==="MONTHLY"?new Date().toISOString().slice(0,7):kind==="DAILY"?new Date().toISOString().slice(0,10):"default";
const metric=(value:number|null,suffix="")=>value===null?"不可用":`${value.toFixed(2)}${suffix}`;

export default function UsagePage(_props:{csrfToken?:string}){
  const [budgets,setBudgets]=useState<Record<string,CostSummary|undefined>>({}),[groups,setGroups]=useState<UsageGroup[]>([]),[error,setError]=useState(""),[loading,setLoading]=useState(true);
  async function load(){const settled=await Promise.all(["DAILY","MONTHLY"].map(async kind=>{try{return[kind,await getCostSummary(kind,periodKey(kind))] as const}catch{return[kind,undefined] as const}}));const usage=await getUsageSummary();setBudgets(Object.fromEntries(settled));setGroups(usage.groups);}
  useEffect(()=>{load().catch(e=>setError(e instanceof Error?e.message:"用量加载失败")).finally(()=>setLoading(false));},[]);
  const unknown=groups.reduce((total,group)=>total+group.unknown_cost_attempts,0);
  return <section className="control-page" aria-busy={loading}><header className="control-hero"><div><span className="eyebrow">用量账本</span><h2>用量与费用</h2></div></header>{error&&<div className="control-alert" role="alert">{error}</div>}
  <div className="metric-grid"><article><span>今日估算费用（UTC）</span><strong>{budgets.DAILY?money(budgets.DAILY.charged_microusd):"暂无记录"}</strong></article><article><span>本月估算费用（UTC）</span><strong>{budgets.MONTHLY?money(budgets.MONTHLY.charged_microusd):"暂无记录"}</strong></article><article><span>累计已知费用</span><strong>{money(groups.reduce((total,group)=>total+group.cost_microusd,0))}</strong></article><article><span>费用未知调用</span><strong>{unknown}</strong></article></div>
  <section className="control-panel"><div className="control-panel-head"><div><span className="eyebrow">模型指标</span><h3>按角色、供应商与模型统计</h3></div><span className="control-count">{groups.length}</span></div>{loading?<p className="muted" role="status" aria-busy="true">正在加载用量…</p>:groups.length?<div className="usage-table" role="table"><div className="usage-table-row usage-table-head" role="row"><span>角色 / 模型</span><span>成功率</span><span>备用率</span><span>首字延迟</span><span>生成速度</span><span>P95 延迟</span><span>费用</span></div>{groups.map(g=><div className="usage-table-row" role="row" key={`${g.role}:${g.profile_version_id}`}><span><strong>{localizedCode(g.role, g.role)}</strong><small>{g.provider} · {g.profile_version_id.slice(-8)}</small></span><span>{(g.success_rate*100).toFixed(1)}%</span><span>{(g.fallback_rate*100).toFixed(1)}%</span><span>{metric(g.ttft_seconds,"s")}</span><span>{metric(g.tps)}</span><span>{metric(g.p95_latency_seconds,"s")}</span><span>{money(g.cost_microusd)}{g.unknown_cost_attempts>0&&<small>{g.unknown_cost_attempts} 次不可用</small>}</span></div>)}</div>:<div className="control-empty compact"><h3>暂无模型调用</h3><p>完成一次模型请求后，这里会显示权威指标。</p></div>}</section>
  </section>;
}
