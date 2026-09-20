import { useState } from "react";
import type { GoalActivationApproval } from "../types";
import { ActivationPreview } from "./approval/ActivationPreview";

interface ApprovalCardProps {
  approvalId: string;
  details?: GoalActivationApproval;
  onGrant: () => Promise<void> | void;
  onReject: () => Promise<void> | void;
}

export default function ApprovalCard({ approvalId, details, onGrant, onReject }: ApprovalCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(action: () => Promise<void> | void) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch {
      setError("审批操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="approval-card">
      <div>
        <span className="eyebrow risk-label">写入操作 · 需要审批</span>
        <h3>需要你的决策</h3>
        {details
          ? <ActivationPreview activation={details} heading={`开启执行管理：${details.preview?.objective_title ?? "执行安排"}`} />
          : <p>一次写入操作已暂停，批准后才会修改相应内容。</p>}
        <details className="approval-reference">
          <summary>查看审批引用</summary>
          <code>{approvalId}</code>
        </details>
        {error && <p className="error-message" role="alert">{error}</p>}
      </div>
      <div className="button-row">
        <button className="button button-danger" disabled={busy} onClick={() => void act(onReject)} type="button">拒绝</button>
        <button className="button button-primary" disabled={busy} onClick={() => void act(onGrant)} type="button">批准</button>
      </div>
    </article>
  );
}
