import { useEffect, useMemo, useRef, useState } from "react";
import { archiveMemoryEntry, createMemoryEntry, decideMemoryProposal, deleteMemoryEpisode, getMemoryOverview, purgeMemoryEntry, restoreMemoryEntry, updateMemoryEntry, updateMemoryEpisode } from "../api";
import AppToast from "../components/AppToast";
import ConfirmDialog from "../components/ConfirmDialog";
import { localizedCode } from "../localization";
import type { MemoryEntry, MemoryEpisode, MemoryProposal } from "../types";

type Notice = { message: string; tone: "success" | "error" };

function evidenceLabel(proposal: MemoryProposal): string {
  if (proposal.evidence_label) return proposal.evidence_label;
  if (proposal.evidence_state === "INVALID") return "来源不可用";
  if (proposal.evidence_state === "LEGACY_UNVERIFIED") return "历史来源未核验";
  if ((proposal.independent_user_turn_count ?? 0) > 1) return "多次一致表达";
  if (proposal.independent_user_turn_count === 1) return "一次明确表达";
  if (proposal.evidence_state === "VERIFIED") return "来源已核验";
  return "来源待核验";
}

function evidenceText(item: NonNullable<MemoryProposal["evidence"]>[number]): string {
  return `${item.source_label}：${item.excerpt}`;
}

export default function MemoryPage({ csrfToken }: { csrfToken: string }) {
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [episodes, setEpisodes] = useState<MemoryEpisode[]>([]);
  const [proposals, setProposals] = useState<MemoryProposal[]>([]);
  const [draft, setDraft] = useState("");
  const [entryDrafts, setEntryDrafts] = useState<Record<string, string>>({});
  const [proposalDrafts, setProposalDrafts] = useState<Record<string, string>>({});
  const [editingEntryId, setEditingEntryId] = useState<string | null>(null);
  const [editingProposalId, setEditingProposalId] = useState<string | null>(null);
  const [entryToDelete, setEntryToDelete] = useState<MemoryEntry | null>(null);
  const [episodeToDelete, setEpisodeToDelete] = useState<MemoryEpisode | null>(null);
  const [lastCreated, setLastCreated] = useState<MemoryEntry | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const mutationKeys = useRef(new Map<string, { fingerprint: string; key: string }>());

  const activeEntries = useMemo(() => entries.filter(item => item.status === "ACTIVE"), [entries]);
  const archivedEntries = useMemo(() => entries.filter(item => item.status === "ARCHIVED"), [entries]);
  const pendingProposals = useMemo(() => proposals.filter(item => item.status === "PENDING"), [proposals]);

  function mutationKey(operation: string, payload: unknown): string {
    const fingerprint = JSON.stringify(payload);
    const previous = mutationKeys.current.get(operation);
    if (previous?.fingerprint === fingerprint) return previous.key;
    const key = crypto.randomUUID();
    mutationKeys.current.set(operation, { fingerprint, key });
    return key;
  }

  async function reload() {
    const data = await getMemoryOverview();
    setEntries(data.entries || []);
    setEpisodes(data.episodes || []);
    setProposals(data.proposals || []);
    setError("");
  }

  useEffect(() => { void reload().catch(caught => setError(caught instanceof Error ? caught.message : "加载失败")).finally(() => setLoading(false)); }, []);

  async function runAction(id: string, action: () => Promise<void>, success?: string) {
    setBusy(id);
    setError("");
    try {
      await action();
      try {
        await reload();
        if (success) setNotice({ message: success, tone: "success" });
      } catch (caught) {
        const message = caught instanceof Error ? caught.message : "列表刷新失败";
        setError(message);
        setNotice({ message: "操作已完成，但列表刷新失败，请刷新页面", tone: "error" });
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "操作失败";
      setNotice({ message, tone: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function addEntry() {
    const content = draft.trim();
    if (!content) return;
    setBusy("create");
    setError("");
    try {
      const operation = "create";
      const created = await createMemoryEntry({ kind: "preference", scope_type: "user", scope_id: "", content, idempotency_key: mutationKey(operation, { content }) }, csrfToken);
      mutationKeys.current.delete(operation);
      setDraft("");
      setLastCreated(created);
      try {
        await reload();
        setNotice({ message: "已加入长期记忆", tone: "success" });
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "列表刷新失败");
        setNotice({ message: "记忆已保存，但列表刷新失败，请刷新页面", tone: "error" });
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "保存失败";
      setNotice({ message, tone: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function undoCreatedEntry() {
    if (!lastCreated) return;
    const entry = lastCreated;
    await runAction("undo", async () => {
      const operation = `archive:${entry.id}`;
      await archiveMemoryEntry(entry.id, mutationKey(operation, {}), csrfToken);
      mutationKeys.current.delete(operation);
      setLastCreated(null);
    }, "已撤销，这条记忆不会再被使用");
  }

  async function decide(proposal: MemoryProposal, accept: boolean) {
    const content = (proposalDrafts[proposal.id] ?? proposal.original_content ?? proposal.content).trim();
    await runAction(`proposal:${proposal.id}`, async () => {
      const operation = `proposal:${proposal.id}`;
      const payload = { accept, content: accept ? content : "", version: proposal.version };
      await decideMemoryProposal(proposal.id, {
        accept,
        idempotency_key: mutationKey(operation, payload),
        ...(proposal.version === undefined ? {} : { expected_version: proposal.version }),
        ...(accept ? { accepted_content: content } : {}),
      }, csrfToken);
      mutationKeys.current.delete(operation);
      setEditingProposalId(null);
      setProposalDrafts(current => {
        const next = { ...current };
        delete next[proposal.id];
        return next;
      });
    }, accept ? "记忆建议已确认" : "记忆建议已忽略");
  }

  function beginEntryEdit(entry: MemoryEntry) {
    setEntryDrafts(current => ({ ...current, [entry.id]: entry.content }));
    setEditingEntryId(entry.id);
  }

  function cancelEntryEdit(entry: MemoryEntry) {
    setEntryDrafts(current => ({ ...current, [entry.id]: entry.content }));
    setEditingEntryId(null);
  }

  async function saveEntry(entry: MemoryEntry) {
    const content = (entryDrafts[entry.id] ?? entry.content).trim();
    if (!content) return;
    await runAction(`entry:${entry.id}`, async () => {
      const operation = `entry:${entry.id}`;
      await updateMemoryEntry(entry.id, content, entry.revision_id, mutationKey(operation, { content, revision: entry.revision_id }), csrfToken);
      mutationKeys.current.delete(operation);
      setEditingEntryId(null);
    }, "长期记忆已保存");
  }

  async function confirmDelete() {
    if (!entryToDelete) return;
    const entry = entryToDelete;
    await runAction(`delete:${entry.id}`, async () => {
      const operation = `delete:${entry.id}`;
      await purgeMemoryEntry(entry.id, mutationKey(operation, {}), csrfToken);
      mutationKeys.current.delete(operation);
      setEntryToDelete(null);
      if (lastCreated?.id === entry.id) setLastCreated(null);
    }, "长期记忆已永久删除");
  }

  async function confirmEpisodeDelete() {
    if (!episodeToDelete) return;
    const episode = episodeToDelete;
    await runAction(`episode-delete:${episode.id}`, async () => {
      const operation = `episode-delete:${episode.id}`;
      await deleteMemoryEpisode(episode.id, episode.version, mutationKey(operation, { version: episode.version }), csrfToken);
      mutationKeys.current.delete(operation);
    }, "经历摘要已删除").finally(() => setEpisodeToDelete(null));
  }

  return <div className="memory-v2 page-stack">
    {error && <p className="error-message" role="alert">{error}</p>}
    {lastCreated && <div className="memory-undo" role="status"><span>已记住“{lastCreated.content}”</span><button className="button button-quiet" disabled={busy === "undo"} type="button" onClick={() => void undoCreatedEntry()}>{busy === "undo" ? "正在撤销…" : "撤销"}</button></div>}

    <section className="memory-overview"><article><span>当前对话</span><strong>短期窗口</strong><p>最近完整消息自动维护，超出窗口后按完整轮次归档。</p></article><article><span>最近经历</span><strong>{episodes.length}</strong><p>可追溯的旧对话摘要，不会自动成为长期事实。</p></article><article><span>长期记忆</span><strong>{activeEntries.length}</strong><p>只有你确认的稳定信息会跨对话使用。</p></article></section>

    <section className="memory-section">
      <div className="section-heading"><div><span className="eyebrow">待确认变更</span><h3>待确认变更</h3></div></div>
      {loading ? <p className="muted" role="status" aria-busy="true">加载中…</p> : pendingProposals.length === 0 ? <p className="muted">暂无待确认建议</p> : pendingProposals.map(proposal => {
        const isEditing = editingProposalId === proposal.id;
        const proposalContent = proposal.original_content ?? proposal.content;
        const evidence = proposal.evidence ?? [];
        return <article className="memory-row memory-proposal" key={proposal.id}><div><span>{localizedCode(proposal.kind, proposal.kind)} · {localizedCode(proposal.scope_type, proposal.scope_type)}</span>{isEditing ? <textarea aria-label={`修改建议 ${proposal.id}`} value={proposalDrafts[proposal.id] ?? proposalContent} onChange={event => setProposalDrafts(current => ({ ...current, [proposal.id]: event.target.value }))}/> : <strong>{proposalContent}</strong>}<p>{proposal.reason || "根据对话中的用户表达提出"}</p><span className={`memory-evidence-state evidence-${(proposal.evidence_state ?? "unknown").toLowerCase()}`}>{evidenceLabel(proposal)}</span>{evidence.length > 0 && <details className="memory-evidence"><summary>查看来源依据</summary><ul>{evidence.map((item, index) => <li key={`${proposal.id}:evidence:${index}`}>{evidenceText(item)}</li>)}</ul></details>}</div><div className="button-row"><button className="button button-quiet" disabled={busy !== null} type="button" onClick={() => void decide(proposal, false)}>忽略</button>{isEditing ? <><button className="button button-secondary" disabled={busy !== null} type="button" onClick={() => setEditingProposalId(null)}>取消</button><button className="button button-primary" disabled={busy !== null || !(proposalDrafts[proposal.id] ?? proposalContent).trim()} type="button" onClick={() => void decide(proposal, true)}>确认修改</button></> : <><button className="button button-secondary" disabled={busy !== null} type="button" onClick={() => { setProposalDrafts(current => ({ ...current, [proposal.id]: proposalContent })); setEditingProposalId(proposal.id); }}>编辑后确认</button><button className="button button-primary" disabled={busy !== null} type="button" onClick={() => void decide(proposal, true)}>确认</button></>}</div></article>;
      })}
    </section>

    <section className="memory-section">
      <div className="section-heading"><div><span className="eyebrow">长期记忆</span><h3>已生效记忆</h3></div><form className="memory-add" onSubmit={event => { event.preventDefault(); void addEntry(); }}><input aria-label="新增长期记忆" placeholder="例如：喜欢简洁明确的回答" value={draft} onChange={event => setDraft(event.target.value)}/><button className="button button-primary" disabled={busy !== null || !draft.trim()} type="submit">{busy === "create" ? "正在保存…" : "记住"}</button></form></div>
      {loading ? <p className="muted" role="status" aria-busy="true">加载中…</p> : activeEntries.length === 0 && <p className="muted">暂无已生效的长期记忆</p>}
      {activeEntries.map(entry => { const isEditing = editingEntryId === entry.id; return <article className="memory-row" key={entry.id}><div><span>{localizedCode(entry.kind, entry.kind)} · {localizedCode(entry.scope_type, entry.scope_type)} · v{entry.revision_no}</span>{isEditing ? <textarea aria-label={`编辑 ${entry.id}`} value={entryDrafts[entry.id] ?? entry.content} onChange={event => setEntryDrafts(current => ({ ...current, [entry.id]: event.target.value }))}/> : <strong>{entry.content}</strong>}{entry.pinned && <p>优先使用</p>}</div><div className="button-row">{isEditing ? <><button className="button button-quiet" disabled={busy !== null} type="button" onClick={() => cancelEntryEdit(entry)}>取消</button><button className="button button-primary" disabled={busy !== null || !(entryDrafts[entry.id] ?? "").trim()} type="button" onClick={() => void saveEntry(entry)}>保存</button></> : <button className="button button-quiet" disabled={busy !== null} type="button" onClick={() => beginEntryEdit(entry)}>编辑</button>}<button className="button button-quiet" disabled={busy !== null} type="button" onClick={() => void runAction(`archive:${entry.id}`, async () => { const operation=`archive:${entry.id}`; await archiveMemoryEntry(entry.id, mutationKey(operation, {}), csrfToken); mutationKeys.current.delete(operation); if (lastCreated?.id === entry.id) setLastCreated(null); }, "长期记忆已停用")}>停用</button><button className="button button-danger" disabled={busy !== null} type="button" onClick={() => setEntryToDelete(entry)}>永久删除</button></div></article>; })}
    </section>

    <section className="memory-section memory-archived">
      <div className="section-heading"><div><span className="eyebrow">已停用</span><h3>已停用记忆</h3></div><span className="muted">{archivedEntries.length} 条</span></div>
      {loading ? <p className="muted" role="status" aria-busy="true">加载中…</p> : archivedEntries.length === 0 ? <p className="muted">暂无已停用记忆</p> : archivedEntries.map(entry => <article className="memory-row" key={entry.id}><div><span>{localizedCode(entry.kind, entry.kind)} · {localizedCode(entry.scope_type, entry.scope_type)}</span><strong>{entry.content}</strong></div><div className="button-row"><button className="button button-secondary" disabled={busy !== null} type="button" onClick={() => void runAction(`restore:${entry.id}`, async () => { const operation=`restore:${entry.id}`; await restoreMemoryEntry(entry.id, mutationKey(operation, {}), csrfToken); mutationKeys.current.delete(operation); }, "长期记忆已恢复")}>恢复</button><button className="button button-danger" disabled={busy !== null} type="button" onClick={() => setEntryToDelete(entry)}>永久删除</button></div></article>)}
    </section>

    <section className="memory-section"><div className="section-heading"><div><span className="eyebrow">经历摘要</span><h3>最近经历</h3></div></div>{loading ? <p className="muted" role="status" aria-busy="true">加载中…</p> : episodes.length === 0 ? <p className="muted">对话超出短期窗口后会在这里形成摘要。</p> : episodes.map(episode => <article className="memory-row" key={episode.id}><div><span>{episode.project_id || "当前对话"} · 消息 {episode.start_message_seq}–{episode.end_message_seq}</span><textarea defaultValue={episode.summary} aria-label={`编辑经历 ${episode.id}`} onBlur={event => { const summary=event.target.value; if (summary !== episode.summary) void runAction(`episode:${episode.id}`, async () => { const operation=`episode:${episode.id}`; await updateMemoryEpisode(episode.id, summary, episode.retrieval_policy, episode.version, mutationKey(operation,{summary,version:episode.version}), csrfToken); mutationKeys.current.delete(operation); }, "经历摘要已保存"); }}/><p>{localizedCode(episode.retrieval_policy, "按需使用")} · {localizedCode(episode.sensitivity, "普通")}</p></div><button className="button button-quiet" disabled={busy !== null} type="button" onClick={() => setEpisodeToDelete(episode)}>删除经历</button></article>)}</section>

    <ConfirmDialog open={Boolean(entryToDelete)} title="永久删除记忆？" description={`确定永久删除“${entryToDelete?.content ?? ""}”吗？正文和历史版本将无法恢复。`} confirmLabel="永久删除" busy={Boolean(entryToDelete && busy === `delete:${entryToDelete.id}`)} onCancel={() => setEntryToDelete(null)} onConfirm={() => void confirmDelete()}/>
    <ConfirmDialog open={Boolean(episodeToDelete)} title="删除经历摘要？" description={`确定删除“${episodeToDelete ? `${episodeToDelete.project_id || "当前对话"} · 消息 ${episodeToDelete.start_message_seq}–${episodeToDelete.end_message_seq}` : ""}”吗？删除后无法恢复。`} busy={Boolean(episodeToDelete && busy === `episode-delete:${episodeToDelete.id}`)} onCancel={() => setEpisodeToDelete(null)} onConfirm={() => void confirmEpisodeDelete()}/>
    {notice && <AppToast {...notice} onDismiss={() => setNotice(null)}/>}
  </div>;
}
