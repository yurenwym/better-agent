import type { GoalActivationApproval } from "../../types";

// 目标激活预览：run 级审批卡（ApprovalCard）与对话内工具审批卡（ChatToolApprovalCard）
// 原先各写一份，文案与排期列表重复。这里统一为唯一样式来源。
export function ActivationPreview({
  activation, heading, showProgramVersion = false,
}: {
  activation: GoalActivationApproval;
  heading?: string;
  showProgramVersion?: boolean;
}) {
  const preview = activation.preview;
  return (
    <div>
      <h4>{heading ?? preview?.objective_title ?? "执行安排"}</h4>
      <p>
        {preview?.start_date} 至 {preview?.end_date} · {preview?.timezone} · 每日 {preview?.daily_minutes} 分钟
        {showProgramVersion && activation.program_version !== undefined
          ? ` · 预览版本 v${String(activation.program_version)}`
          : ""}
      </p>
      <p>确认后将建立行动安排并记录执行进度。不会因此开启主动提醒或自动复盘；只需要计划文档时可以拒绝，已保存文档仍保留。</p>
      {preview?.structure?.actions?.length ? <ol className="approval-schedule">
        {preview.structure.actions.map((action, index) => (
          <li key={`${action.scheduled_date}-${index}`}>
            {action.scheduled_date}：{action.title}（{action.estimated_minutes} 分钟）
          </li>
        ))}
      </ol> : null}
    </div>
  );
}
