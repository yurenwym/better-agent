import type { ResearchJob } from "../types";

const labels: Record<string, string> = { queued:"等待开始",planning:"正在规划",retrieving:"正在检索来源",distilling:"正在提炼证据",reflecting:"正在检查缺口",curating:"正在整理大纲",writing:"正在撰写报告",summarizing:"正在生成摘要",finalizing:"正在校验引用",completed:"研究完成",failed:"研究失败",cancelled:"已取消" };
const failureMessages: Record<string, string> = {
  search_auth_failed: "搜索服务认证失败。请在设置中检查搜索服务密钥后重新研究。",
  search_rate_limited: "搜索服务当前请求过多。系统已自动重试；仍失败时请稍后重新研究。",
  search_timeout: "搜索服务响应超时。系统已自动重试；请检查网络或稍后重新研究。",
  search_provider_unavailable: "搜索服务暂时不可用。请检查搜索服务配置或稍后重试。",
  search_request_failed: "搜索请求未成功。请检查搜索服务配置后重新研究。",
  search_no_results: "搜索服务没有返回结果。请换一个更具体的研究主题后重试。",
  search_results_rejected: "搜索结果未通过安全校验，无法作为研究来源。请调整主题后重试。",
  search_results_filtered: "搜索结果内容过短或重复，无法形成可靠证据。请换一个更具体的研究主题后重试。",
  unknowncitation: "报告的引用校验未通过，已有检索结果不会作为错误报告发布。",
  insufficientevidence: "目前找到的有效证据不足，暂时无法生成可靠报告。",
  topiccoverageerror: "报告未完整回答研究题目，系统已阻止发布这份不完整结果。请重新研究。",
  max_attempts_exhausted: "研究多次执行仍未完成，请稍后重新研究。",
  timeout: "研究服务响应超时，请重新研究。",
  rate_limit: "研究服务当前请求较多，请稍后重新研究。",
  server: "研究服务暂时不可用，请稍后重新研究。",
};

export default function ResearchProgressCard({ job, onCancel, onRetry, onOpen, onDelete, deleteBusy = false }: { job: ResearchJob; onCancel?:()=>void; onRetry?:()=>void; onOpen?:()=>void; onDelete?:()=>void; deleteBusy?:boolean }) {
  const active = job.status === "QUEUED" || job.status === "RUNNING";
  const progress=Math.max(8,["queued","planning","retrieving","distilling","reflecting","curating","writing","summarizing","finalizing","completed"].indexOf(job.phase)*11);
  return <section className={`research-progress research-${job.status.toLowerCase()}`} aria-label="深度研究进度">{active&&<div className="research-mode-notice" role="status" aria-label="深度研究已启动"><span className="research-mode-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M10.5 18a7.5 7.5 0 1 1 5.3-2.2L21 21"/><path d="M8 9h5M8 12h3"/></svg></span><div><strong>已进入深度研究模式</strong><p>我会在后台检索并核对来源，完成后生成带引用的报告。你可以继续浏览当前对话。</p></div></div>}<div className="research-progress-head"><div><span className="eyebrow">DEEP RESEARCH</span><strong>{job.title || job.topic}</strong></div><span className="research-status" role="status" aria-atomic="true">{labels[job.phase] || job.phase}</span></div><div className="research-meter" role="progressbar" aria-label="研究进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div><div className="research-progress-meta"><span>{job.source_count} 个来源</span><span>{job.evidence_count} 条证据</span><span>第 {job.attempts || 1} 次执行</span></div>{job.status === "FAILED"&&<div className="research-failure" role="alert"><strong>这次研究没有完成</strong><p>{failureMessages[job.failure_reason_code || ""] || "研究过程中出现异常，请重新研究。"}</p></div>}<div className="button-row">{active && onCancel && <button className="button button-quiet" onClick={onCancel} type="button">取消研究</button>}{job.status === "FAILED" && onRetry && <button className="button button-secondary" onClick={onRetry} type="button">重新研究</button>}{job.status === "COMPLETED" && onOpen && <button className="button button-primary" onClick={onOpen} type="button">查看报告</button>}{!active && onDelete && <button className="button button-danger" disabled={deleteBusy} onClick={onDelete} type="button">{deleteBusy?"正在删除…":"删除研究"}</button>}</div></section>;
}
