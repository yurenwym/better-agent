import { useEffect,useState } from "react";
import { createNotificationChannel,createSchedule,deleteNotificationChannel,deleteSchedule,getNotificationChannels,getSchedules,runSchedule,updateSchedule } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import type { NotificationChannel,ResearchSchedule } from "../types";

// 后端的 trigger_weekday 与 datetime.weekday() 对齐：0 是周一。
const weekdayLabels=["周一","周二","周三","周四","周五","周六","周日"];
const triggerLabels:Record<string,string>={daily:"每天",weekly:"每周",interval_hours:"固定间隔"};

function triggerSummary(item:ResearchSchedule):string{
  if(item.trigger_type==="interval_hours")return `每 ${item.interval_hours??"—"} 小时`;
  const time=item.trigger_time??"";
  if(item.trigger_type==="weekly")return `每${weekdayLabels[item.trigger_weekday??0]??"周一"} ${time}`.trim();
  return `${triggerLabels[item.trigger_type]??item.trigger_type} ${time}`.trim();
}

type PendingDelete={kind:"schedule"|"channel";id:string;name:string};

export default function SchedulesPage({csrfToken}:{csrfToken:string}){
  const [items,setItems]=useState<ResearchSchedule[]>([]),[channels,setChannels]=useState<NotificationChannel[]>([]),[name,setName]=useState(""),[topic,setTopic]=useState(""),[channelName,setChannelName]=useState(""),[env,setEnv]=useState("");
  const [triggerType,setTriggerType]=useState<ResearchSchedule["trigger_type"]>("daily"),[triggerTime,setTriggerTime]=useState("09:00"),[triggerWeekday,setTriggerWeekday]=useState("0"),[intervalHours,setIntervalHours]=useState("6"),[timezone,setTimezone]=useState("Asia/Shanghai");
  const [loading,setLoading]=useState(true),[error,setError]=useState(""),[busy,setBusy]=useState(""),[pendingDelete,setPendingDelete]=useState<PendingDelete|null>(null);
  async function load(){try{const [scheduleData,channelData]=await Promise.all([getSchedules(),getNotificationChannels()]);setItems(scheduleData.schedules);setChannels(channelData.channels);setError("")}catch(caught){setError(caught instanceof Error?caught.message:"定时任务加载失败，请稍后重试。")}finally{setLoading(false)}}
  useEffect(()=>{void load()},[]);
  async function runOperation(key:string,action:()=>Promise<unknown>){if(busy)return;setBusy(key);setError("");try{await action();await load()}catch(caught){setError(caught instanceof Error?caught.message:"操作失败，请稍后重试。")}finally{setBusy("")}}
  async function confirmDelete(){if(!pendingDelete)return;const target=pendingDelete;setBusy("delete");setError("");try{if(target.kind==="schedule")await deleteSchedule(target.id,csrfToken);else await deleteNotificationChannel(target.id,csrfToken);await load();setPendingDelete(null)}catch(caught){setError(caught instanceof Error?caught.message:"删除失败，请稍后重试。");setPendingDelete(null)}finally{setBusy("")}}
  const draft={name,topic,source_scopes:["web"],trigger_type:triggerType,trigger_time:triggerType==="interval_hours"?null:triggerTime,trigger_weekday:triggerType==="weekly"?Number(triggerWeekday):null,interval_hours:triggerType==="interval_hours"?Number(intervalHours):null,timezone};
  const createLabel=triggerType==="interval_hours"?`每 ${intervalHours||"—"} 小时创建`:triggerType==="weekly"?`每${weekdayLabels[Number(triggerWeekday)]??"周一"} ${triggerTime} 创建`:`每天 ${triggerTime} 创建`;
  return <div className="page-stack">
    {error&&<p className="error-message" role="alert">{error}</p>}
    <section className="schedule-page">
      <div className="section-heading"><div><span className="eyebrow">AUTOMATIONS</span><h3>定时研究</h3><p className="muted">每日、每周或固定小时自动创建研究任务。</p></div></div>
      <form className="schedule-create" onSubmit={e=>{e.preventDefault();void runOperation("create",async()=>{await createSchedule(draft,csrfToken);setName("");setTopic("")})}}>
        <label>名称<input required value={name} onChange={e=>setName(e.target.value)}/></label>
        <label>研究主题<input required value={topic} onChange={e=>setTopic(e.target.value)}/></label>
        <label>触发方式<select aria-label="触发方式" value={triggerType} onChange={e=>setTriggerType(e.target.value as ResearchSchedule["trigger_type"])}><option value="daily">每天</option><option value="weekly">每周</option><option value="interval_hours">固定间隔</option></select></label>
        {triggerType!=="interval_hours"&&<label>触发时间<input aria-label="触发时间" required type="time" value={triggerTime} onChange={e=>setTriggerTime(e.target.value)}/></label>}
        {triggerType==="weekly"&&<label>星期<select aria-label="星期" value={triggerWeekday} onChange={e=>setTriggerWeekday(e.target.value)}>{weekdayLabels.map((label,index)=><option key={label} value={String(index)}>{label}</option>)}</select></label>}
        {triggerType==="interval_hours"&&<label>间隔小时<input aria-label="间隔小时" required min="1" max="720" type="number" value={intervalHours} onChange={e=>setIntervalHours(e.target.value)}/></label>}
        <label>时区<input aria-label="时区" required value={timezone} onChange={e=>setTimezone(e.target.value)}/></label>
        <button className="button button-primary" disabled={Boolean(busy)}>{busy==="create"?"正在…":createLabel}</button>
      </form>
      {loading?<p className="muted" role="status" aria-busy="true">正在加载定时任务…</p>:items.length===0?<div className="empty-state"><strong>暂无定时任务</strong><p>创建第一个定时研究后，它会出现在这里。</p></div>:items.map(item=><article className="schedule-row" key={item.id}><div><strong>{item.name}</strong><span>{item.topic}</span><small>{triggerSummary(item)} · {item.timezone} · 下次 {item.next_run_at?new Date(item.next_run_at).toLocaleString("zh-CN"):"已停用"}{item.last_run_at?` · 上次 ${new Date(item.last_run_at).toLocaleString("zh-CN")}`:""}{item.notify_enabled?"":" · 未开启通知"}</small></div><div className="button-row"><button className="button button-quiet" disabled={Boolean(busy)} onClick={()=>void runOperation(`toggle:${item.id}`,()=>updateSchedule(item.id,{enabled:!item.enabled},csrfToken))}>{busy===`toggle:${item.id}`?"正在…":item.enabled?"停用":"启用"}</button><button className="button button-secondary" disabled={Boolean(busy)} onClick={()=>void runOperation(`run:${item.id}`,()=>runSchedule(item.id,crypto.randomUUID(),csrfToken))}>{busy===`run:${item.id}`?"正在…":"立即运行"}</button><button className="button button-danger" disabled={Boolean(busy)} onClick={()=>setPendingDelete({kind:"schedule",id:item.id,name:item.name})}>删除</button></div></article>)}
    </section>
    <section className="schedule-page">
      <div className="section-heading"><div><span className="eyebrow">NOTIFICATIONS</span><h3>完成通知</h3><p className="muted">只保存环境变量名，密钥不会进入数据库。</p></div></div>
      <form className="schedule-create" onSubmit={e=>{e.preventDefault();void runOperation("channel-create",async()=>{await createNotificationChannel({name:channelName,channel_type:"webhook",secret_env_name:env},csrfToken);setChannelName("");setEnv("")})}}><label>渠道名称<input required value={channelName} onChange={e=>setChannelName(e.target.value)}/></label><label>密钥环境变量<input required pattern="[A-Z][A-Z0-9_]{2,63}" value={env} onChange={e=>setEnv(e.target.value)}/></label><button className="button button-primary" disabled={Boolean(busy)}>{busy==="channel-create"?"正在…":"添加 Webhook"}</button></form>
      {loading?<p className="muted" role="status" aria-busy="true">正在加载通知渠道…</p>:channels.length===0?<div className="empty-state"><strong>暂无通知渠道</strong><p>添加 Webhook 后，研究完成时会发送通知。</p></div>:channels.map(item=><article className="schedule-row" key={item.id}><div><strong>{item.name}</strong><span>{item.channel_type} · {item.secret_env_name}</span><small>{item.configured?"环境变量已配置":"等待环境变量"}</small></div><button className="button button-danger" disabled={Boolean(busy)} onClick={()=>setPendingDelete({kind:"channel",id:item.id,name:item.name})}>删除</button></article>)}
    </section>
    <ConfirmDialog open={Boolean(pendingDelete)} title={pendingDelete?.kind==="channel"?"删除通知渠道？":"删除定时任务？"} description={pendingDelete?pendingDelete.kind==="channel"?`确定删除通知渠道“${pendingDelete.name}”吗？删除后将不再发送完成通知。`:`确定删除定时任务“${pendingDelete.name}”吗？删除后不会再自动创建研究。`:""} busy={busy==="delete"} onCancel={()=>setPendingDelete(null)} onConfirm={()=>void confirmDelete()}/>
  </div>;
}
