import type { EventRecord } from "./types";

export type TrajectoryStage =
  | "interaction"
  | "context"
  | "model"
  | "plan"
  | "react"
  | "tool"
  | "memory"
  | "checkpoint"
  | "state"
  | "run";

export type TrajectoryTone = "accent" | "info" | "success" | "warning" | "danger" | "neutral";

export interface TrajectoryItem {
  event: EventRecord;
  seq: number;
  occurredAt: string;
  stage: TrajectoryStage;
  stageLabel: string;
  tone: TrajectoryTone;
  title: string;
  detail: string;
}

export interface TrajectoryGroup {
  id: string;
  label: string;
  events: TrajectoryItem[];
}

const stateLabels: Record<string, string> = {
  RECEIVED: "收到目标",
  CLARIFYING: "澄清阶段",
  PLANNING: "规划阶段",
  AWAITING_APPROVAL: "审批阶段",
  EXECUTING: "执行阶段",
  AWAITING_OUTCOME: "等待结果阶段",
  REFLECTING: "复盘阶段",
  COMPLETED: "完成",
  BLOCKED: "已阻塞",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const stageMeta: Record<TrajectoryStage, { label: string; tone: TrajectoryTone }> = {
  interaction: { label: "交互", tone: "accent" },
  context: { label: "上下文", tone: "info" },
  model: { label: "模型", tone: "info" },
  plan: { label: "计划", tone: "success" },
  react: { label: "ReAct", tone: "warning" },
  tool: { label: "工具与审批", tone: "warning" },
  memory: { label: "长期记忆", tone: "accent" },
  checkpoint: { label: "检查点", tone: "neutral" },
  state: { label: "状态", tone: "accent" },
  run: { label: "Run", tone: "neutral" },
};

function text(data: Record<string, unknown>, key: string, fallback = ""): string {
  const value = data[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function stateLabel(value: string): string {
  return stateLabels[value] ?? value.toLowerCase().replaceAll("_", " ");
}

function make(event: EventRecord, stage: TrajectoryStage, title: string, detail: string): TrajectoryItem {
  const meta = stageMeta[stage];
  return {
    event,
    seq: event.seq,
    occurredAt: event.occurred_at,
    stage,
    stageLabel: meta.label,
    tone: meta.tone,
    title,
    detail,
  };
}

export function describeEvent(event: EventRecord): TrajectoryItem {
  const data = event.data;

  switch (event.type) {
    case "run.created":
      return make(event, "run", "Run 已创建", "目标已进入本地运行时");
    case "run.completed":
      return make(event, "run", "目标已完成", "运行结果已保存，可以查看计划、轨迹和记忆");
    case "run.failed":
      return make(event, "run", "Run 失败", text(data, "reason", "运行时遇到未完成的请求"));
    case "run.blocked":
      return make(event, "run", "Run 暂停等待处理", text(data, "reason", "需要补充预算、审批或结果"));
    case "run.cancelled":
      return make(event, "run", "Run 已取消", "已停止后续执行，之前的轨迹仍然保留");
    case "run.resumed":
      return make(event, "run", "Run 已恢复", "从最近的检查点继续执行");
    case "interaction.started":
      return make(event, "interaction", "开始理解目标", "正在把这次输入整理成可执行的上下文");
    case "interaction.ended":
      return make(event, "interaction", "本轮交互已收束", "输入已写入运行轨迹");
    case "context.snapshot_created":
      return make(event, "context", "上下文快照已就绪", `已整理 ${text(data, "crop_count", "0")} 个上下文裁剪片段`);
    case "model.invocation_started": {
      const kind = text(data, "kind");
      const label = kind === "clarification" ? "正在理解目标" : kind === "planning" ? "正在生成计划" : kind === "react" ? "正在选择下一步" : kind === "reflection" ? "正在复盘结果" : "模型正在思考";
      return make(event, "model", label, "模型请求已开始");
    }
    case "model.invocation_finished":
      return make(event, "model", text(data, "status") === "failed" ? "模型阶段失败" : "模型阶段完成", text(data, "status") === "failed" ? "请查看失败原因并重试" : "模型结果已交给运行时处理");
    case "model.attempt_started":
      return make(event, "model", `模型请求第 ${text(data, "attempt", "1")} 次尝试`, "正在等待模型响应");
    case "model.attempt_finished": {
      const failed = text(data, "status") === "failed";
      const errorKind = text(data, "error_kind");
      const detail = !failed ? "模型响应已收到" : errorKind === "request" ? "模型端点拒绝了请求格式" : errorKind === "authentication" ? "模型凭证未通过验证" : errorKind === "timeout" ? "模型响应超时，可以稍后重试" : "模型请求未完成，请检查运行状态";
      return make(event, "model", failed ? "模型请求失败" : "模型请求完成", detail);
    }
    case "model.first_token":
      return make(event, "model", "模型开始输出", `首 token 延迟 ${text(data, "ttft_seconds", "不可用")} 秒`);
    case "model.usage_updated":
      return make(event, "model", "模型用量已记录", "Token、缓存和生成耗时已写入统计");
    case "model.response.delta":
      return make(event, "model", "正在接收模型回答", "模型回答正在实时加入对话");
    case "model.response.reset":
      return make(event, "model", "正在重新生成模型回答", "上一轮输出未通过结构校验，正在替换为新的回答");
    case "model.response": {
      const kind = text(data, "kind");
      const label = kind === "clarification" ? "澄清回复已到达" : kind === "planning" ? "计划回复已到达" : kind === "react" ? "执行判断已到达" : kind === "reflection" ? "复盘回复已到达" : "模型回复已到达";
      return make(event, "model", label, "完整回复已加入对话，可以直接据此继续下一步");
    }
    case "plan.version_created":
      return make(event, "plan", "新的计划版本已生成", `当前版本 v${text(data, "version", "1")}`);
    case "plan.approved":
      return make(event, "plan", "计划已获批准", `v${text(data, "version", "当前版本")} 可以开始执行`);
    case "plan.step_started":
      return make(event, "plan", "计划步骤开始", text(data, "title", "正在执行当前步骤"));
    case "plan.step_finished":
      return make(event, "plan", "计划步骤完成", text(data, "title", "当前步骤已完成"));
    case "plan.step_cancelled":
      return make(event, "plan", "计划步骤已取消", text(data, "title", "该步骤不会继续执行"));
    case "react.iteration_started":
      return make(event, "react", `ReAct 第 ${text(data, "iteration", "1")} 轮开始`, "运行时正在观察并决定下一步");
    case "react.iteration_finished":
      return make(event, "react", `ReAct 第 ${text(data, "iteration", "1")} 轮完成`, "本轮观察结果已保存");
    case "tool.proposed":
      return make(event, "tool", "准备调用工具", `工具：${text(data, "name", "未命名工具")}`);
    case "tool.started":
    case "tool.execution.started":
      return make(event, "tool", "工具开始执行", `工具：${text(data, "name", "未命名工具")}`);
    case "tool.finished":
    case "tool.execution":
    case "tool.execution.finished":
      return make(event, "tool", text(data, "ok") === "false" ? "工具执行未完成" : "工具执行完成", `工具：${text(data, "name", "当前工具")}`);
    case "approval.requested":
      return make(event, "tool", "需要你的审批", "写入类操作已暂停，批准后才会产生副作用");
    case "approval.granted":
      return make(event, "tool", "审批已通过", "已允许对应的受控操作继续");
    case "approval.rejected":
      return make(event, "tool", "审批已拒绝", "对应操作不会执行");
    case "memory.candidate_created":
      return make(event, "memory", "发现一条记忆候选", "等待你在记忆面板确认是否长期保留");
    case "memory.confirmed":
      return make(event, "memory", "长期记忆已确认", "已写入 Markdown 记忆并可在面板中回滚");
    case "memory.applied":
      return make(event, "memory", "记忆已应用到上下文", "这条已确认记忆参与了本轮运行");
    case "memory.rejected":
      return make(event, "memory", "记忆候选已拒绝", "不会写入长期记忆");
    case "memory.disabled":
      return make(event, "memory", "长期记忆已停用", "后续上下文不会再使用它");
    case "checkpoint.saved":
      return make(event, "checkpoint", "检查点已保存", "运行可以从这里恢复，不会重复已完成的副作用");
    case "budget.warning":
      return make(event, "state", "预算即将耗尽", `已追加 ${text(data, "added", "0")} 轮预算`);
    case "budget.exhausted":
      return make(event, "state", "执行预算已耗尽", "可以追加预算后从检查点恢复");
    case "state.transitioned": {
      const target = text(data, "to", text(data, "target", "UNKNOWN"));
      return make(event, "state", `已进入${stateLabel(target)}`, `${stateLabel(text(data, "from", "上一状态"))} → ${stateLabel(target)}`);
    }
    default:
      return make(event, "state", "运行记录已更新", "新的轨迹事件已写入本地日志");
  }
}

export function groupEvents(events: EventRecord[]): TrajectoryGroup[] {
  const sorted = [...events].sort((left, right) => left.seq - right.seq);
  const groups: TrajectoryGroup[] = [];
  let active: TrajectoryGroup | null = null;
  let interactionNumber = 0;
  let overviewNumber = 0;

  for (const event of sorted) {
    if (event.type === "interaction.started") {
      interactionNumber += 1;
      active = { id: text(event.data, "interaction_id", `interaction-${interactionNumber}`), label: `交互 ${interactionNumber}`, events: [] };
      groups.push(active);
    }
    if (!active) {
      overviewNumber += 1;
      active = { id: `run-${overviewNumber}`, label: overviewNumber === 1 ? "Run 总览" : `Run 后续 ${overviewNumber}`, events: [] };
      groups.push(active);
    }
    active.events.push(describeEvent(event));
    if (event.type === "interaction.ended") active = null;
  }

  return groups;
}

export function stageLabel(stage: TrajectoryStage): string {
  return stageMeta[stage].label;
}
