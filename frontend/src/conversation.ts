import type { MessageRecord } from "./types";

export interface MessagePresentation {
  summary: string;
  detail: string | null;
  bullets: string[];
}

function stringValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function parseJson(content: string): Record<string, unknown> | null {
  let candidate = content.trim();
  if (candidate.startsWith("```")) {
    candidate = candidate.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
  }
  try {
    const value: unknown = JSON.parse(candidate);
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function parseEmbeddedJson(content: string): Record<string, unknown> | null {
  const start = content.indexOf("{");
  if (start < 0) return null;
  const candidate = content.slice(start).replace(/\s*```$/, "").trim();
  try {
    const value: unknown = JSON.parse(candidate);
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function looksLikeStructuredPayload(content: string): boolean {
  return /"(?:needs_clarification|steps|summary|action|tool_call|candidates)"\s*:/.test(content);
}

function safeStructuredFallback(): MessagePresentation {
  return { summary: "妯″瀷杩斿洖浜嗕竴鏉＄粨鏋勫寲缁撴灉銆?", detail: null, bullets: [] };
}

function rawPresentation(parsed: Record<string, unknown>): MessagePresentation {
  const steps = Array.isArray(parsed.steps)
    ? parsed.steps.map((step) => {
      if (!step || typeof step !== "object") return "";
      const value = step as Record<string, unknown>;
      const title = stringValue(value.title);
      const description = stringValue(value.description);
      return title && description ? `${title}：${description}` : title;
    }).filter(Boolean)
    : [];
  if (typeof parsed.needs_clarification === "boolean") {
    return {
      summary: parsed.needs_clarification ? "我还需要更多信息，才能继续推进这个目标。" : "目标信息已经足够，我会继续进入下一步。",
      detail: parsed.needs_clarification ? "请在下方补充背景、范围或完成标准。" : "",
      bullets: [],
    };
  }
  if (steps.length > 0 || (parsed.summary && !parsed.action)) {
    return {
      summary: stringValue(parsed.summary) || "我已经生成了一版执行计划。",
      detail: steps.length > 0 ? `共 ${steps.length} 个步骤，批准后开始执行。` : null,
      bullets: steps,
    };
  }
  if (typeof parsed.action === "string") {
    const actionLabels: Record<string, string> = {
      complete_step: "这一步已完成。",
      continue: "我会继续推进当前步骤。",
      await_outcome: "我会先等待外部结果。",
      tool_call: "我准备调用一个工具。",
      blocked: "我暂时无法安全继续。",
    };
    const tool = parsed.tool_call && typeof parsed.tool_call === "object"
      ? stringValue((parsed.tool_call as Record<string, unknown>).name)
      : "";
    const visibleResult = stringValue(parsed.output) || stringValue(parsed.summary);
    return {
      summary: visibleResult || actionLabels[parsed.action] || "我已经更新了当前步骤。",
      detail: tool ? `工具：${tool}` : stringValue(parsed.observation) || actionLabels[parsed.action] || null,
      bullets: [],
    };
  }
  if (Array.isArray(parsed.candidates)) {
    return {
      summary: parsed.candidates.length > 0 ? `复盘完成，发现 ${parsed.candidates.length} 条记忆候选。` : "复盘完成，没有新增记忆候选。",
      detail: "你可以在记忆面板中确认或拒绝候选。",
      bullets: [],
    };
  }
  return { summary: "模型返回了一条结构化结果。", detail: null, bullets: [] };
}

function streamedValue(content: string, key: string): string {
  const match = content.match(new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`));
  if (!match) return "";
  try {
    return JSON.parse(`"${match[1]}"`);
  } catch {
    return "";
  }
}

function streamingPresentation(content: string): MessagePresentation {
  const summary = streamedValue(content, "summary") || streamedValue(content, "output");
  if (summary) {
    return { summary, detail: "模型正在继续生成回答…", bullets: [] };
  }
  const trimmed = content.trim();
  if (trimmed && !trimmed.startsWith("{") && !trimmed.startsWith("[") && !trimmed.startsWith("```")) {
    return { summary: trimmed, detail: "模型正在继续生成回答…", bullets: [] };
  }
  return { summary: "模型正在生成回答…", detail: "正在接收模型输出", bullets: [] };
}

export function presentMessage(message: MessageRecord): MessagePresentation {
  if (message.role === "user") {
    return { summary: message.content, detail: null, bullets: [] };
  }
  if (message.streaming) return streamingPresentation(message.content);
  const parsed = parseJson(message.content) ?? parseEmbeddedJson(message.content);
  if (parsed) return rawPresentation(parsed);
  return looksLikeStructuredPayload(message.content)
    ? safeStructuredFallback()
    : { summary: message.content, detail: null, bullets: [] };
}
