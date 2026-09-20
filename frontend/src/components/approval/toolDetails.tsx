import type { ReactNode } from "react";
import type { ChatToolCall, GoalActivationApproval } from "../../types";
import { ActivationPreview } from "./ActivationPreview";

// 对话内写操作的确认卡片：按 tool_name 分派到各自的渲染器，
// 避免把每种工具的文案与字段塞进同一个 if-else 链里继续膨胀。

export function stringParam(params: Record<string, unknown>, name: string): string | null {
  const value = params[name];
  return typeof value === "string" && value.trim() ? value : null;
}

export function numberParam(params: Record<string, unknown>, name: string): number | null {
  const value = params[name];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function stringArrayParam(params: Record<string, unknown>, name: string): string[] {
  const value = params[name];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function numberArrayParam(params: Record<string, unknown>, name: string): number[] {
  const value = params[name];
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

export function FactList({ fields }: { fields: [string, string | null][] }) {
  return <dl className="approval-facts">
    {fields.filter(([, value]) => value).map(([label, value]) => (
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
    ))}
  </dl>;
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

export function activationPreview(toolCall: ChatToolCall): GoalActivationApproval | undefined {
  const activation = toolCall.binding?.goal_activation;
  return activation && typeof activation === "object" ? activation : undefined;
}

function FeedbackDetails({ params }: { params: Record<string, unknown> }) {
  const actualMinutes = numberParam(params, "actual_minutes");
  const difficulty = numberParam(params, "difficulty");
  const cleared = stringArrayParam(params, "cleared_fields");
  return <FactList fields={[
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
  ]} />;
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
  const dailyMinutes = numberParam(params, "daily_minutes");
  return <FactList fields={[
    ["计划文档", stringParam(params, "document_id")],
    ["文档版本", stringParam(params, "expected_document_version_id")],
    ["开始日期", stringParam(params, "start_date")],
    ["结束日期", stringParam(params, "end_date")],
    ["时区", stringParam(params, "timezone")],
    ["每日预算", dailyMinutes === null ? null : `${dailyMinutes} 分钟`],
    ["可用日", weekdayLabels],
    ["休息日", excluded.length ? excluded.join("、") : null],
  ]} />;
}

function DeferDetails({ params }: { params: Record<string, unknown> }) {
  return <FactList fields={[
    ["行动", stringParam(params, "action_id")],
    ["新日期", stringParam(params, "scheduled_date")],
  ]} />;
}

export interface ToolApprovalContent { title: string; body: ReactNode }

type ToolRenderer = (toolCall: ChatToolCall) => ToolApprovalContent;

export function renderPlanDraft(toolCall: ChatToolCall): ToolApprovalContent {
  const params = toolCall.params ?? {};
  const draftMarkdown = stringParam(params, "markdown_content");
  const draftTitle = stringParam(params, "title");
  return {
    title: "保存计划文档 · 需要确认",
    body: <div>
      <h4>{draftTitle ?? "计划文档"}</h4>
      <p>批准后会保存为计划文档，你可以在计划页面查看和修改；不会创建执行目标，也不会开启提醒或每日复盘。</p>
      {draftMarkdown && <details className="approval-reference">
        <summary>查看将保存的内容</summary>
        <pre className="approval-content">{draftMarkdown}</pre>
      </details>}
    </div>,
  };
}

export function renderGoalActivation(toolCall: ChatToolCall): ToolApprovalContent {
  const activation = activationPreview(toolCall);
  const preview = activation?.preview;
  if (!activation) {
    return {
      title: "生成执行预览 · 需要确认",
      body: <div>
        <h4>按以下设置编译执行预览</h4>
        <p>批准后会编译为待确认的执行预览（草稿目标）；仍需你再次确认具体安排后才会激活。不会开启提醒或每日复盘。</p>
        <PreviewDetails params={toolCall.params ?? {}} />
      </div>,
    };
  }
  return {
    title: "开启执行管理 · 需要确认",
    body: <ActivationPreview activation={activation} showProgramVersion />,
  };
}

export function renderModifyDocument(toolCall: ChatToolCall): ToolApprovalContent {
  const params = toolCall.params ?? {};
  const draftMarkdown = stringParam(params, "markdown_content");
  const draftTitle = stringParam(params, "title");
  return {
    title: "修改计划文档 · 需要确认",
    body: <div>
      <h4>{draftTitle ?? "计划文档"}</h4>
      <p>
        批准后会写入新版本（文档 {stringParam(params, "document_id")}，基于版本 {stringParam(params, "expected_version_id")}）。
        已激活的执行安排继续使用原来源版本，不会自动重编译，也不会开启提醒或复盘。
      </p>
      {draftMarkdown && <details className="approval-reference">
        <summary>查看修改后的内容</summary>
        <pre className="approval-content">{draftMarkdown}</pre>
      </details>}
    </div>,
  };
}

export function renderDeferAction(toolCall: ChatToolCall): ToolApprovalContent {
  const params = toolCall.params ?? {};
  return {
    title: "延期行动 · 需要确认",
    body: <div>
      <h4>把行动改到 {stringParam(params, "scheduled_date") ?? "指定日期"}</h4>
      <p>批准后会保留原行动并创建替代行动；不会自动开启提醒。</p>
      <DeferDetails params={params} />
    </div>,
  };
}

export function renderRecordFeedback(toolCall: ChatToolCall): ToolApprovalContent {
  return {
    title: "记录行动进展 · 需要确认",
    body: <div>
      <h4>记录你亲自陈述的进展</h4>
      <p>只记录你明确提供的信息，不会把行动标记为完成。</p>
      <FeedbackDetails params={toolCall.params ?? {}} />
    </div>,
  };
}

// 新增工具只需在这里注册一个渲染器；未注册的工具回落到通用文案。
const toolRenderers: Record<string, ToolRenderer> = {
  create_plan_draft: renderPlanDraft,
  modify_plan_document: renderModifyDocument,
  activate_goal_plan: renderGoalActivation,
  defer_action: renderDeferAction,
  record_action_feedback: renderRecordFeedback,
};

export function toolApprovalContent(toolCall: ChatToolCall): ToolApprovalContent {
  const renderer = toolRenderers[toolCall.tool_name];
  if (renderer) return renderer(toolCall);
  return {
    title: "写入操作 · 需要确认",
    body: <p>一次写入操作已暂停，批准后才会修改相应内容。</p>,
  };
}

