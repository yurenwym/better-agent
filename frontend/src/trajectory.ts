import type { EventRecord, ThreadEvent } from "./types";

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
  event: EventRecord | ThreadEvent;
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

export type TrajectoryView = "summary" | "debug";

const summaryRunEvents = new Set([
  "interaction.started", "context.snapshot_created", "model.invocation_started", "model.output.started", "model.first_token",
  "model.response", "model.fallback.selected", "model.response.reset", "approval.requested",
  "approval.rejected", "tool.execution.started", "tool.execution.finished", "tool.started", "tool.finished",
  "plan.approved", "plan.step_started", "plan.step_finished", "run.blocked", "run.failed",
  "run.cancelled", "run.completed", "state.transitioned", "budget.warning", "budget.exhausted",
]);

const summaryThreadEvents = new Set([
  "turn.started", "plan.context_loaded", "model.output.started",
  // R2-04: the user must be able to see that the app is *整理历史* rather than
  // thinking the model has started answering, and must be told when a wait ran
  // out or when history was dropped. These are context-stage status events, so
  // they can never be read as first-token progress.
  "context.archiving", "context.archive_wait_timeout", "context.incomplete",
  "memory.retrieval.completed", "message.completed",
  "ask.requested", "turn.awaiting_input", "ask.answered", "turn.awaiting_direction",
  "turn.direction_selected", "turn.failed", "turn.cancel_requested", "turn.cancelled",
  "execution.materialized", "expert.requested", "research.requested", "plan.document_ready",
  "plan.document_failed", "plan.document_conflict", "plan.execution_projection_failed",
]);

export function isKeyTrajectoryEvent(event: EventRecord | ThreadEvent, mode: "run" | "thread"): boolean {
  if (mode === "thread") {
    if (["turn.metrics.updated", "message.delta", "message.snapshot"].includes(event.type)) return false;
    return summaryThreadEvents.has(event.type)
      || /failed|cancel|fallback|blocked|retry/.test(event.type);
  }
  if (event.type.startsWith("cost.") || event.type === "model.response.delta") return false;
  if (event.type === "model.attempt.finished" && String(event.data.status) === "succeeded") return false;
  return summaryRunEvents.has(event.type)
    || /failed|cancel|fallback|blocked|retry/.test(event.type);
}

export function keyTrajectoryEvents<T extends EventRecord | ThreadEvent>(
  events: T[], mode: "run" | "thread",
): T[] {
  return events.filter((event) => isKeyTrajectoryEvent(event, mode));
}

export function formatDuration(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "--";
  const seconds = value / 1000;
  const decimals = seconds < 10 ? 2 : seconds < 100 ? 1 : 0;
  return `${seconds.toFixed(decimals).replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1")}s`;
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

const runtimeReasonLabels: Record<string, string> = {
  STEP_WALL_TIME_EXHAUSTED: "当前步骤执行超时",
  REACT_ITERATION_BUDGET_EXHAUSTED: "本步骤的执行轮次已用完",
  IDENTICAL_ACTION_BUDGET_EXHAUSTED: "模型重复执行了相同操作，任务已暂停",
  CONSECUTIVE_TOOL_ERRORS_EXHAUSTED: "工具连续执行失败，任务已暂停",
  INVALID_MODEL_ACTION: "模型返回了无法识别的操作",
  MODEL_REQUESTED_BLOCK: "模型请求暂停当前任务",
  TOOL_AUTHORIZATION_DENIED: "工具调用未通过安全授权",
  MODEL_AUTHENTICATION: "模型凭证未通过验证",
  MODEL_RATE_LIMIT: "模型服务当前请求过多，请稍后重试",
  MODEL_REQUEST: "模型服务拒绝了当前请求格式",
  MODEL_SERVER: "模型服务暂时不可用",
  MODEL_TIMEOUT: "模型响应超时",
};

const legacyRuntimeReasonLabels: Record<string, string> = {
  "step wall time exhausted": "当前步骤执行超时",
  "react iteration budget exhausted": "本步骤的执行轮次已用完",
  "identical action budget exhausted": "模型重复执行了相同操作，任务已暂停",
  "consecutive tool errors exhausted": "工具连续执行失败，任务已暂停",
  "invalid model action": "模型返回了无法识别的操作",
  "model requested block": "模型请求暂停当前任务",
};

function text(data: Record<string, unknown>, key: string, fallback = ""): string {
  const value = data[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function publicRuntimeMessage(data: Record<string, unknown>, fallback: string): string {
  const code = text(data, "reason_code");
  const message = text(data, "message");
  const legacyReason = text(data, "reason");
  if (message && /[\u3400-\u9fff]/.test(message)) return message;
  if (runtimeReasonLabels[code]) return runtimeReasonLabels[code];
  if (legacyRuntimeReasonLabels[legacyReason]) return legacyRuntimeReasonLabels[legacyReason];
  if (legacyReason && /[\u3400-\u9fff]/.test(legacyReason)) return legacyReason;
  return fallback;
}

function planVersion(data: Record<string, unknown>): string {
  return text(data, "version", text(data, "document_version", "?"));
}

function stateLabel(value: string): string {
  return stateLabels[value] ?? value.toLowerCase().replaceAll("_", " ");
}

function make(
  event: EventRecord | ThreadEvent,
  stage: TrajectoryStage,
  title: string,
  detail: string,
  toneOverride?: TrajectoryTone,
): TrajectoryItem {
  const meta = stageMeta[stage];
  return {
    event,
    seq: event.seq,
    occurredAt: event.occurred_at,
    stage,
    stageLabel: meta.label,
    tone: toneOverride ?? meta.tone,
    title,
    detail,
  };
}

export function describeEvent(event: EventRecord): TrajectoryItem {
  const data = event.data;

  switch (event.type) {
    case "model.invocation.created":
      return make(event, "model", "模型任务已创建", `角色：${text(data, "role", "conversation")}`);
    case "model.attempt.started":
      return make(event, "model", `模型请求第 ${text(data, "attempt", "1")} 次尝试`, text(data, "reason") === "fallback" ? "正在使用显式备用模型" : "正在等待模型响应");
    case "model.output.started":
      return make(event, "model", "模型开始输出", "首段内容已到达，后续不会跨模型切换");
    case "model.fallback.selected":
      return make(event, "model", "已切换到备用模型", `主模型因 ${text(data, "error_kind", "临时故障")} 未完成，正在使用策略中明确配置的 fallback`);
    case "model.attempt.finished":
      return make(event, "model", text(data, "status") === "succeeded" ? "模型尝试完成" : "模型尝试未完成", text(data, "error_kind", "用量与状态已写入账本"));
    case "model.invocation.finished":
      return make(event, "model", text(data, "status") === "succeeded" ? "模型阶段完成" : "模型阶段结束", `状态：${text(data, "status", "unknown")}`);
    case "run.created":
      return make(event, "run", "执行任务已创建", "目标已进入本地运行时");
    case "run.completed":
      return make(event, "run", "目标已完成", "运行结果已保存，可以查看计划、轨迹和记忆");
    case "run.failed":
      return make(event, "run", "运行失败", publicRuntimeMessage(data, "运行时遇到未完成的请求"));
    case "run.blocked":
      return make(event, "run", "任务暂停等待处理", publicRuntimeMessage(data, "需要补充预算、审批或结果"));
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
      return make(event, "state", "执行预算已耗尽", publicRuntimeMessage(data, "可以追加预算后从检查点恢复"));
    case "state.transitioned": {
      const target = text(data, "to", text(data, "target", "UNKNOWN"));
      return make(event, "state", `已进入${stateLabel(target)}`, `${stateLabel(text(data, "from", "上一状态"))} → ${stateLabel(target)}`);
    }
    default:
      return make(event, "state", "运行记录已更新", "新的轨迹事件已写入本地日志");
  }
}

export function describeThreadEvent(event: ThreadEvent): TrajectoryItem {
  const data = event.data;

  switch (event.type) {
    case "turn.accepted":
      return make(event, "interaction", "已收到你的消息", "正在准备本轮回答");
    case "turn.started":
      return make(event, "interaction", "本轮对话已开始", `排队 ${formatDuration(data.queue_wait_ms)}，正在准备上下文`);
    case "memory.retrieval.completed": {
      const mode = text(data, "retrieval_mode");
      if (mode === "semantic") {
        return make(event, "context", "已完成语义记忆检索", "已按当前对话范围选择相关长期记忆");
      }
      const fallbackLabels: Record<string, string> = {
        embedding_not_configured: "向量模型未配置，已自动降级",
        no_semantic_candidates: "没有语义候选，已自动降级",
        timeout: "向量服务超时，已自动降级",
        rate_limit: "向量服务繁忙，已自动降级",
      };
      const reason = fallbackLabels[text(data, "fallback_reason")] ?? "语义检索不可用，已自动降级";
      return make(event, "context", "已使用关键词记忆检索", reason);
    }
    case "context.archiving": {
      // A status hint about the *context* stage. Deliberately rendered with the
      // context tone and never as model output: 整理历史 is not 首字.
      const state = text(data, "state");
      const waited = formatDuration(data.waited_ms);
      if (state === "waiting") {
        return make(event, "context", "正在整理历史上下文", `已等待 ${waited}，整理完成后会立即开始回答`);
      }
      if (state === "ready") {
        return make(event, "context", "历史上下文已整理完成", `等待 ${waited}，正在准备回答`);
      }
      if (state === "cancelled") {
        return make(event, "context", "已停止等待历史整理", "本轮已取消；已整理好的部分会保留");
      }
      return make(event, "context", "历史整理未在时限内完成", `已等待 ${waited}，本轮未生成回答`);
    }
    case "context.counted":
      return make(event, "context", "本轮上下文已选定", `本地选择耗时 ${formatDuration(data.counting_ms)}，保留 ${text(data, "kept_turns", "?")} 轮`);
    case "context.archive_wait_timeout":
      return make(event, "context", "历史整理超时，本轮未生成回答", text(data, "message", "你的输入已保存，稍后可以直接继续，不需要重新输入。"), "warning");
    case "context.incomplete":
      return make(event, "context", "本轮回答未包含完整历史", text(data, "message", "历史整理不可用，本轮基于有限上下文回答"), "warning");
    case "context.window_degraded":
      return make(event, "context", "最近对话超出窗口上限", text(data, "reason", "已按上限缩小最近对话范围，摘要仍然保留"));
    case "turn.policy_decided":
      return make(event, "model", "已确定回答方式", "模型已完成本轮路由判断");
    case "plan.document_prepared":
      return make(event, "plan", "正在准备保存计划", `已校验计划内容，准备写入本地版本 v${planVersion(data)}`);
    case "plan.document_version_created":
      return make(event, "plan", "计划版本已创建", `本地账本已记录计划 v${planVersion(data)}`);
    case "plan.document_ready":
      return make(event, "plan", "计划已保存", `已保存到计划 · v${planVersion(data)}，可以打开计划页面查看`);
    case "plan.document_failed":
      return make(event, "plan", "计划保存失败", publicRuntimeMessage(data, "计划文件写入未完成，可以重试"), "danger");
    case "plan.document_conflict":
      return make(event, "plan", "计划文件存在冲突", "检测到计划页面或本地文件有并发修改，请比较后再保存", "danger");
    case "plan.context_loaded":
      return make(event, "context", `已加载计划 v${planVersion(data)}`, text(data, "cropped") === "true" || data.cropped === true
        ? "本轮使用了计划的确定性裁剪上下文"
        : "本轮回答已固定读取当前已提交版本");
    case "plan.execution_projection_started":
      return make(event, "plan", "正在生成执行预览", `正在基于计划 v${planVersion(data)} 整理可执行步骤`);
    case "plan.execution_projection_created":
      return make(event, "plan", "执行预览已生成", `已基于计划 v${planVersion(data)} 创建预览，等待你的确认`);
    case "plan.execution_projection_failed":
      return make(event, "plan", "执行预览生成失败", publicRuntimeMessage(data, "计划无法编译为可执行步骤"), "danger");
    case "message.started":
      return make(event, "model", "正在生成回答", "模型回答会实时显示在左侧对话框");
    case "message.delta":
      return make(event, "model", "回答正在实时生成", "新的内容已加入对话");
    case "message.snapshot":
      return make(event, "model", "回答内容已更新", "已同步当前可用回答");
    case "message.completed":
      if (text(data, "finish_reason") === "retry") {
        return make(event, "model", "正在重试模型回答", "上一代输出未完成，正在准备新的回答");
      }
      if (text(data, "finish_reason") === "cancelled") {
        return make(event, "model", "回答已停止", "本轮生成已按你的要求停止");
      }
      if (text(data, "finish_reason") === "error") {
        return make(event, "model", "回答生成遇到问题", "已保留当前可用内容，可以重新发送消息");
      }
      return make(event, "model", "回答生成完成", "回答已加入对话，可以继续输入下一步");
    case "ask.requested":
      return make(event, "interaction", "问题已准备好", "模型需要你补充少量信息后继续");
    case "turn.awaiting_input":
      return make(event, "interaction", "等待你的回答", "请在对话框中选择选项或补充信息，提交后会继续当前目标");
    case "ask.answered":
      return make(event, "interaction", "已收到你的回答", "正在用补充信息继续当前目标");
    case "ask.cancelled":
      return make(event, "interaction", "已停止询问", "当前对话没有继续执行新的步骤");
    case "turn.awaiting_direction":
      return make(event, "interaction", "等待你的选择", "你可以选择继续执行或修改方案");
    case "turn.direction_selected":
      return make(event, "interaction", "已记录你的选择", text(data, "action", "正在应用下一步操作"));
    case "turn.completed":
      return make(event, "interaction", "本轮对话完成", "回答已加入对话，可以继续输入下一步");
    case "turn.metrics.updated":
      return make(event, "model", "本轮耗时已更新", `排队 ${formatDuration(data.queue_wait_ms)} · 上下文 ${formatDuration(data.context_ms)} · TTFT ${formatDuration(data.model_ttft_ms)} · 输出 ${formatDuration(data.stream_ms)}`);
    case "turn.failed":
      return make(event, "state", "本轮对话未完成", "可以重新发送消息，再次尝试生成回答");
    case "turn.cancel_requested":
      return make(event, "state", "正在停止本轮对话", "已收到停止请求");
    case "turn.cancelled":
      return make(event, "state", "本轮对话已停止", "后续生成已取消，之前内容仍会保留");
    case "execution.materialized":
      return make(event, "run", "已创建执行任务", "已切换到 Run 轨迹，可以继续查看执行进度");
    default:
      return make(event, "state", "对话进展已更新", "新的线程事件已记录");
  }
}

export type ArchiveStatusHint = {
  state: "waiting" | "timeout" | "cancelled" | "incomplete";
  title: string;
  detail: string;
  tone: TrajectoryTone;
};

/**
 * The live archival status for a thread, derived from the event stream.
 *
 * Polling the archive-jobs endpoint cannot meet a one-second status target, so
 * the status is taken from the newest `context.archiving` event instead: the
 * backend emits it the moment the wait starts, and it arrives on the same
 * stream as everything else. `ready` is a terminal state that clears the hint.
 *
 * Only the newest event matters, so the state machine needs no turn identity:
 * each wait emits `waiting` and then exactly one of `ready`/`cancelled`/`timeout`.
 */
export function latestArchiveStatus(events: ThreadEvent[]): ArchiveStatusHint | null {
  const relevant = ["context.archiving", "context.archive_wait_timeout", "context.incomplete"];
  const newest = [...events].reverse().find((event) => relevant.includes(event.type));
  if (!newest) return null;
  if (newest.type === "context.archive_wait_timeout") {
    return {
      state: "timeout",
      title: "历史整理超时，本轮未生成回答",
      detail: text(newest.data, "message", "你的输入已保存，稍后可以直接继续，不需要重新输入。"),
      tone: "warning",
    };
  }
  if (newest.type === "context.incomplete") {
    return {
      state: "incomplete",
      title: "本轮回答未包含完整历史",
      detail: text(newest.data, "message", "历史整理不可用，本轮基于有限上下文回答"),
      tone: "warning",
    };
  }
  const state = text(newest.data, "state");
  if (state === "waiting") {
    return {
      state: "waiting",
      title: "正在整理历史上下文",
      detail: "整理完成后会立即开始回答；这一步不是模型输出。",
      tone: "info",
    };
  }
  if (state === "cancelled") {
    return {
      state: "cancelled",
      title: "已停止等待历史整理",
      detail: "已整理好的部分会保留，可以重新发送消息继续。",
      tone: "neutral",
    };
  }
  // "ready" (and anything unrecognised) clears the hint.
  return null;
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

export function groupThreadEvents(events: ThreadEvent[]): TrajectoryGroup[] {
  const sorted = [...events].sort((left, right) => left.seq - right.seq);
  const groups: TrajectoryGroup[] = [];
  let active: TrajectoryGroup | null = null;
  let activeTurnId: string | null = null;
  let turnNumber = 0;

  for (const event of sorted) {
    if (!active || activeTurnId !== event.turn_id) {
      turnNumber += 1;
      active = { id: `turn-${event.turn_id}`, label: `第 ${turnNumber} 轮对话`, events: [] };
      activeTurnId = event.turn_id;
      groups.push(active);
    }
    active.events.push(describeThreadEvent(event));
  }

  return groups;
}

export function stageLabel(stage: TrajectoryStage): string {
  return stageMeta[stage].label;
}
