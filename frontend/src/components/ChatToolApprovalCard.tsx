import type { ChatToolCall } from "../types";
import { toolApprovalContent } from "./approval/toolDetails";

interface ChatToolApprovalCardProps {
  toolCall: ChatToolCall;
  busy?: boolean;
  error?: string;
  onApprove: () => void;
  onReject: () => void;
  onRefresh: () => void;
}

// 卡片外壳只负责布局与按钮；每种工具的文案和字段由 approval/toolDetails 按 tool_name 分派。
export default function ChatToolApprovalCard({
  toolCall, busy = false, error = "", onApprove, onReject, onRefresh,
}: ChatToolApprovalCardProps) {
  const { title, body } = toolApprovalContent(toolCall);

  return (
    <article className="approval-card" aria-label={title}>
      <div>
        <span className="eyebrow risk-label">{title}</span>
        {body}
        <details className="approval-reference">
          <summary>查看审批引用</summary>
          <code>{toolCall.approval_id ?? toolCall.id}</code>
        </details>
        {error && <p className="error-message" role="alert">{error}</p>}
      </div>
      <div className="button-row">
        <button className="button button-quiet" disabled={busy} onClick={onRefresh} type="button">刷新状态</button>
        <button className="button button-danger" disabled={busy} onClick={onReject} type="button">拒绝</button>
        <button className="button button-primary" disabled={busy} onClick={onApprove} type="button">
          {busy ? "处理中…" : "批准"}
        </button>
      </div>
    </article>
  );
}
