import { useEffect, useState } from "react";
import { confirmMemory, disableMemory, editMemory, getMemories, rejectMemory, rollbackMemory } from "../api";
import type { MemoryRecord } from "../types";

interface MemoryPageProps { csrfToken: string; }

export default function MemoryPage({ csrfToken }: MemoryPageProps) {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [rollback, setRollback] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  async function reload() { try { setMemories((await getMemories()).memories); } catch (caught) { setError(caught instanceof Error ? caught.message : "记忆加载失败"); } }
  useEffect(() => { void reload(); }, []);

  async function update(action: () => Promise<MemoryRecord>) { try { await action(); await reload(); } catch (caught) { setError(caught instanceof Error ? caught.message : "记忆操作失败"); } }

 return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">MEMORY / MARKDOWN</span><h2>长期记忆</h2><p>候选记忆先由你确认，确认后才会写入对应 Markdown 文件并进入上下文。</p></div><span className="version-badge">{memories.length} 条记录</span></section>{error && <p className="error-message" role="alert">{error}</p>}{memories.length === 0 ? <section className="empty-panel"><h3>暂无候选记忆</h3><p>完成一次 Run 的复盘后，偏好和习惯候选会出现在这里。</p></section> : <section className="memory-list">{memories.map((memory) => { const value = editing[memory.id] ?? memory.content; return <article className={`memory-card memory-${memory.status}`} key={memory.id}><div className="memory-heading"><div><span className="eyebrow">{memory.kind} · {memory.scope}</span><h3>{memory.status}</h3></div><span className="confidence">{Math.round(memory.confidence * 100)}%</span></div><textarea aria-label={`编辑记忆 ${memory.id}`} disabled={memory.status !== "confirmed"} value={value} onChange={(event) => setEditing({ ...editing, [memory.id]: event.target.value })} rows={3} /><div className="memory-meta"><code>{memory.path}</code><span>v{memory.version ?? "—"}</span></div><div className="memory-evidence">证据：{memory.evidence_event_ids.length ? memory.evidence_event_ids.join(", ") : "无"}</div><div className="button-row">{memory.status === "proposed" && <><button className="button button-primary" type="button" onClick={() => void update(() => confirmMemory(memory.id, csrfToken, value))}>确认</button><button className="button button-danger" type="button" onClick={() => void update(() => rejectMemory(memory.id, csrfToken))}>拒绝</button></>}{memory.status === "confirmed" && <><button className="button button-primary" type="button" onClick={() => void update(() => editMemory(memory.id, value, csrfToken))}>保存编辑</button><button className="button button-quiet" type="button" onClick={() => void update(() => disableMemory(memory.id, csrfToken))}>停用</button><input className="rollback-input" aria-label={`回滚记忆 ${memory.id}`} inputMode="numeric" placeholder="版本" value={rollback[memory.id] ?? ""} onChange={(event) => setRollback({ ...rollback, [memory.id]: event.target.value })} /><button className="button button-quiet" type="button" disabled={!rollback[memory.id]} onClick={() => void update(() => rollbackMemory(memory.id, Number(rollback[memory.id]), csrfToken))}>回滚</button></>}</div></article>; })}</section>}</div>;
}
