import type { ChatToolCall, GoalActivationApproval } from "../types";

interface ChatToolApprovalCardProps {
  toolCall: ChatToolCall;
  busy?: boolean;
  error?: string;
  onApprove: () => void;
  onReject: () => void;
  onRefresh: () => void;
}

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const clearedFieldLabels: Record<string, string> = {
  actual_minutes: "实际时长",
  difficulty: "难度",
  note: "备注",
  completed_work: "已完成",
  remaining_work: "剩余",
  output: "产出",
  reason_code: "原因",
};

function stringParam(params: Record<string, unknown>, name: string): string | null {
  const value = params[name];
  return typeof value === "string" && value.trim() ? value : null;
}

function numberParam(params: Record<string, unknown>, name: string): number | null {
  const value = params[name];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringArrayParam(params: Record<string, unknown>, name: string): string[] {
  const value = params[name];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function numberArrayParam(params: Record<string, unknown>, name: string): number[] {
  const value = params[name];
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

function activationPreview(toolCall: ChatToolCall): GoalActivationApproval | undefined {
  const activation = toolCall.binding?.goal_activation;
  return activation && typeof activation === "object" ? activation : undefined;
}

function FeedbackDetails({ params }: { params: Record<string, unknown> }) {
  const actualMinutes = numberParam(params, "actual_minutes");
  const difficulty = numberParam(params, "difficulty");
  const cleared = stringArrayParam(params, "cleared_fields");
  const fields: [string, string | null][] = [
    ["行动", stringParam(params, "action_id")],
    ["方式", stringParam(params, "kind") === "correction" ? "更正已有记录" : "记录部分进展"],
    ["日期", stringParam(params, "actual_date")],
    ["实际用时", actualMinutes === null ? null : `${actualMinutes} 分钟`],
    ["难度", difficulty === null ? null : `${difficulty}/5`],
    ["已完成", stringParam(params, "completed_work")],
    ["剩余", stringParam(params, "remaining_work")],
    ["产出", stringParam(params, "output")],
    ["备注", stringParam(params, "note")],
    ["清除字段", cleared.length ? cleared.map((name) => clearedFieldLabels[name] ?? name).join("、") : null],
  ];
  return <dl className="approval-facts">
    {fields.filter(([, value]) => value).map(([label, value]) => (
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
    ))}
  </dl>;
}

function PreviewDetails({ params }: { params: Record<string, unknown> }) {
  const constraints = params.constraints;
  const constraintRecord = constraints && typeof constraints === "object"
    ? constraints as Record<string, unknown>
    : {};
  const weekdays = numberArrayParam(constraintRecord, "available_weekdays");
  const excluded = stringArrayParam(constraintRecord, "excluded_dates");
  const weekdayLabels = weekdays.length && weekdays.length < 7
    ? weekdays.map((day) => WEEKDAY_LABELS[day] ?? String(day)).join("、")
    : null;
  const fields: [string, string | null][] = [
    ["计划文档", stringParam(params, "document_id")],
    ["文档版本", stringParam(params, "expected_document_version_id")],
    ["开始日期", stringParam(params, "start_date")],
    ["结束日期", stringParam(params, "end_date")],
    ["时区", stringParam(params, "timezone")],
    ["每日预算", numberParam(params, "daily_minutes") === null ? null : `${numberParam(params, "daily_minutes")} 分钟`],
    ["可用日", weekdayLabels],
    ["休息日", excluded.length ? excluded.join("、") : null],
  ];
  return <dl className="approval-facts">
    {fields.filter(([, value]) => value).map(([label, value]) => (
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
    ))}
  </dl>;
}

function DeferDetails({ params }: { params: Record<string, unknown> }) {
  const fields: [string, string | null][] = [
    ["行动", stringParam(params, "action_id")],
    ["新日期", stringParam(params, "scheduled_date")],
  ];
  return <dl className="approval-facts">
    {fields.filter(([, value]) => value).map(([label, value]) => (
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
    ))}
  </dl>;
}

export default function ChatToolApprovalCard({
  toolCall, busy = false, error = "", onApprove, onReject, onRefresh,
}: ChatToolApprovalCardProps) {
  const params = toolCall.params ?? {};
  const activation = activationPreview(toolCall);
  const preview = activation?.preview;
  const isActivation = toolCall.tool_name === "activate_goal_plan" && Boolean(activation);
  const isCompilePreview = toolCall.tool_name === "activate_goal_plan" && !activation;
  const isDraft = toolCall.tool_name === "create_plan_draft";
  const draftMarkdown = stringParam(params, "markdown_content");
  const draftTitle = stringParam(params, "title");

  let title = "写入操作 · 需要确认";
  let body = <p>一次写入操作已暂停，批准后才会修改相应内容。</p>;

  if (isDraft) {
    title = "保存计划文档 · 需要确认";
    body = <div>
      <h4>{draftTitle ?? "计划文档"}</h4>
      <p>批准后会保存为计划文档，你可以在计划页面查看和修改；不会创建执行目标，也不会开启提醒或每日复盘。</p>
      {draftMarkdown && <details className="approval-reference">
        <summary>查看将保存的内容</summary>
        <pre className="approval-content">{draftMarkdown}</pre>
      </details>}
    </div>;
  } else if (isActivation) {
    title = "开启执行管理 · 需要确认";
    body = <div>
      <h4>{preview?.objective_title ?? "执行安排"}</h4>
      <p>
        {preview?.start_date} 至 {preview?.end_date} · {preview?.timezone} · 每日 {preview?.daily_minutes} 分钟
        {activation?.program_version !== undefined ? ` · 预览版本 v${String(activation.program_version)}` : ""}
      </p>
      <p>确认后将建立行动安排并记录执行进度。不会因此开启主动提醒或自动复盘；只需要计划文档时可以拒绝，已保存文档仍保留。</p>
      {preview?.structure?.actions?.length ? <ol className="approval-schedule">
        {preview.structure.actions.map((action, index) => (
          <li key={`${action.scheduled_date}-${index}`}>
            {action.scheduled_date}：{action.title}（{action.estimated_minutes} 分钟）
          </li>
        ))}
      </ol> : null}
    </div>;
  } else if (toolCall.tool_name === "modify_plan_document") {
    title = "修改计划文档 · 需要确认";
    body = <div>
      <h4>{draftTitle ?? "计划文档"}</h4>
      <p>
        批准后会写入新版本（文档 {stringParam(params, "document_id")}，基于版本 {stringParam(params, "expected_version_id")}）。
        已激活的执行安排继续使用原来源版本，不会自动重编译，也不会开启提醒或复盘。
      </p>
      {draftMarkdown && <details className="approval-reference">
        <summary>查看修改后的内容</summary>
        <pre className="approval-content">{draftMarkdown}</pre>
      </details>}
    </div>;
  } else if (isCompilePreview) {
    title = "生成执行预览 · 需要确认";
    body = <div>
      <h4>按以下设置编译执行预览</h4>
      <p>批准后会编译为待确认的执行预览（草稿目标）；仍需你再次确认具体安排后才会激活。不会开启提醒或每日复盘。</p>
      <PreviewDetails params={params} />
    </div>;
  } else if (toolCall.tool_name === "defer_action") {
    title = "延期行动 · 需要确认";
    body = <div>
      <h4>把行动改到 {stringParam(params, "scheduled_date") ?? "指定日期"}</h4>
      <p>批准后会保留原行动并创建替代行动；不会自动开启提醒。</p>
      <DeferDetails params={params} />
    </div>;
  } else if (toolCall.tool_name === "record_action_feedback") {
    title = "记录行动进展 · 需要确认";
    body = <div>
      <h4>记录你亲自陈述的进展</h4>
      <p>只记录你明确提供的信息，不会把行动标记为完成。</p>
      <FeedbackDetails params={params} />
    </div>;
  }

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
