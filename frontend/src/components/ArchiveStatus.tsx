import { useEffect, useMemo, useState } from "react";
import { latestArchiveStatus } from "../trajectory";
import type { ThreadEvent } from "../types";

type ArchiveJob = { id: string; status: string; end_message_seq: number; last_error_code: string | null; updated_at: string };
type ArchiveState = { archived_through_seq: number; jobs: ArchiveJob[] };

export default function ArchiveStatus({ threadId, csrfToken, events = [] }: { threadId: string; csrfToken: string; events?: ThreadEvent[] }) {
  const [state, setState] = useState<ArchiveState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let disposed = false;
    setState(null);
    setError("");
    const load = async () => {
      try {
        const response = await fetch(`/api/threads/${threadId}/archive`);
        if (!response.ok) throw new Error("归档状态暂不可用");
        const value = await response.json() as ArchiveState;
        if (!disposed) { setState(value); setError(""); }
      } catch {
        if (!disposed) setError("归档状态暂不可用");
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [threadId, refresh]);
  const pending = state?.jobs.find(job => job.end_message_seq > state.archived_through_seq && ["QUEUED", "RUNNING", "RETRY_WAIT", "DEAD_LETTER"].includes(job.status));
  // The live hint wins over the polled job state: it is what the user is
  // actually waiting on, and it arrives within the stream's own latency rather
  // than up to a poll interval later.
  const hint = useMemo(() => latestArchiveStatus(events), [events]);
  async function retry() {
    if (!pending) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/threads/${threadId}/archive/${pending.id}/retry`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ expected_updated_at: pending.updated_at }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(response.status === 409 && typeof body?.detail === "string" ? body.detail : "归档重试失败");
      }
      setRefresh(value => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "归档重试失败"); }
    finally { setBusy(false); }
  }
  if (!pending && !error && !hint) return null;
  const dead = pending?.status === "DEAD_LETTER";
  const alert = hint?.tone === "warning" || dead;
  return <aside className={alert ? "error-message archive-status" : "archive-status"} aria-label="历史归档状态" aria-busy={busy}>
    {hint
      ? <span role={hint.tone === "warning" ? "alert" : "status"} title={hint.detail}>{hint.title}</span>
      : pending && <span role="status">{dead ? "历史归档失败，原始对话已保留" : "正在整理历史上下文"}</span>}
    {error && <span role="alert">{error}</span>}
    {pending && (dead || hint?.state === "timeout") && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void retry()}>{busy ? "正在提交…" : "重试归档"}</button>}
  </aside>;
}
