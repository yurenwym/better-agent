import { useState } from "react";

interface ApprovalCardProps {
  approvalId: string;
  onGrant: () => Promise<void> | void;
  onReject: () => Promise<void> | void;
}

export default function ApprovalCard({ approvalId, onGrant, onReject }: ApprovalCardProps) {
  const [busy, setBusy] = useState(false);

  async function act(action: () => Promise<void> | void) {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="approval-card">
      <div>
        <span className="eyebrow risk-label">WRITE / APPROVAL REQUIRED</span>
        <h3>待处理副作用</h3>
        <code>{approvalId}</code>
      </div>
      <div className="button-row">
        <button className="button button-danger" disabled={busy} onClick={() => void act(onReject)} type="button">拒绝</button>
        <button className="button button-primary" disabled={busy} onClick={() => void act(onGrant)} type="button">批准</button>
      </div>
    </article>
  );
}
