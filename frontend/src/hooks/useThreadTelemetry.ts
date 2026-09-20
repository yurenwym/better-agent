import { useEffect, useState } from "react";
import { getThread, getThreadEvents, getThreadMessages, subscribeToThreadEvents } from "../api";
import type { AskQuestion, MessageRecord, PendingAsk, Thread, ThreadEvent, ThreadMessage, Turn } from "../types";

function codePointLength(value: string): number {
  return Array.from(value).length;
}

export function hydrateThreadMessages(messages: ThreadMessage[]): MessageRecord[] {
  return messages.filter((message) => message.status !== "interrupted").map((message) => ({
    id: message.id,
    run_id: message.thread_id,
    turn_id: message.turn_id,
    interaction_id: null,
    role: message.role,
    content: message.content,
    created_at: message.created_at,
    streaming: message.status === "streaming",
    generation: message.generation,
    status: message.status,
    plan_document_version_id: message.plan_document_version_id,
    presentation: message.presentation ?? "standard",
    origin: "history",
    research_job_id: message.research_job_id,
    total_ms: message.total_ms,
  }));
}

export function appendThreadEvent(events: ThreadEvent[], event: ThreadEvent): ThreadEvent[] {
  if (events.some((item) => item.seq === event.seq || item.event_id === event.event_id)) return events;
  return [...events, event].sort((left, right) => left.seq - right.seq);
}

export function needsEventRecovery(events: ThreadEvent[], event: ThreadEvent): boolean {
  const cursor = events.at(-1)?.seq ?? 0;
  return event.seq > cursor + 1;
}

function messageId(event: ThreadEvent): string | null {
  return typeof event.data.message_id === "string" && event.data.message_id
    ? event.data.message_id
    : null;
}

function generationOf(event: ThreadEvent, fallback = 1): number {
  return typeof event.data.generation === "number" ? event.data.generation : fallback;
}

export function needsMessageSnapshot(messages: MessageRecord[], event: ThreadEvent): boolean {
  if (event.type !== "message.delta") return false;
  const id = messageId(event);
  const current = id ? messages.find((message) => message.id === id) : undefined;
  if (!current) return true;
  const offset = event.data.offset;
  return generationOf(event, current.generation ?? 1) !== (current.generation ?? 1)
    || typeof offset !== "number"
    || offset !== codePointLength(current.content);
}

function pendingAskFromRequest(event: ThreadEvent): PendingAsk | null {
  const askId = event.data.ask_id;
  const rawQuestions = event.data.questions;
  if (typeof askId !== "string" || !askId || !Array.isArray(rawQuestions)) return null;
  const questions: AskQuestion[] = [];
  for (const raw of rawQuestions) {
    if (!raw || typeof raw !== "object") return null;
    const candidate = raw as Record<string, unknown>;
    if (
      typeof candidate.id !== "string"
      || typeof candidate.header !== "string"
      || typeof candidate.question !== "string"
      || !Array.isArray(candidate.options)
      || typeof candidate.multi_select !== "boolean"
      || typeof candidate.allow_free_text !== "boolean"
    ) return null;
    const options = candidate.options.filter((option): option is Record<string, unknown> => Boolean(option) && typeof option === "object")
      .filter((option) => typeof option.label === "string" && typeof option.description === "string")
      .map((option) => ({ label: option.label as string, description: option.description as string }));
    if (options.length !== candidate.options.length) return null;
    questions.push({
      id: candidate.id,
      header: candidate.header,
      question: candidate.question,
      options,
      multi_select: candidate.multi_select,
      allow_free_text: candidate.allow_free_text,
    });
  }
  if (questions.length === 0) return null;
  return {
    id: askId,
    turn_id: event.turn_id,
    questions,
    status: "PENDING",
    continuation_turn_id: null,
    created_at: event.occurred_at,
    answered_at: null,
  };
}

export function applyAskEvent(current: PendingAsk | null, event: ThreadEvent): PendingAsk | null {
  if (event.type === "ask.requested") {
    const next = pendingAskFromRequest(event);
    if (!next) return current;
    return current?.id === next.id ? current : next;
  }
  if (event.type === "ask.answered" || event.type === "ask.cancelled") {
    return current && event.data.ask_id === current.id ? null : current;
  }
  return current;
}

export function pendingAskFromEvents(events: ThreadEvent[]): PendingAsk | null {
  return [...events]
    .sort((left, right) => left.seq - right.seq)
    .reduce<PendingAsk | null>(applyAskEvent, null);
}

export function applyThreadEvent(messages: MessageRecord[], event: ThreadEvent): MessageRecord[] {
  const id = messageId(event);
  if (event.type === "message.started" && id) {
    if (messages.some((message) => message.id === id)) return messages;
    return [...messages, {
      id,
      run_id: event.thread_id,
      turn_id: event.turn_id,
      interaction_id: null,
      role: "assistant",
      content: "",
      created_at: event.occurred_at,
      streaming: true,
      generation: generationOf(event),
      status: "streaming",
      presentation: event.data.presentation === "human_bubbles" ? "human_bubbles" : "standard",
      origin: "live",
    }];
  }
  if (event.type === "message.snapshot" && id) {
    const content = typeof event.data.content === "string" ? event.data.content : "";
    const next = {
      id,
      run_id: event.thread_id,
      turn_id: event.turn_id,
      interaction_id: null,
      role: "assistant" as const,
      content,
      created_at: event.occurred_at,
      streaming: event.data.status === "streaming",
      generation: generationOf(event),
      status: typeof event.data.status === "string" ? event.data.status : "streaming",
    };
    return messages.some((message) => message.id === id)
      ? messages.map((message) => message.id === id ? { ...message, ...next } : message)
      : [...messages, next];
  }
  if (event.type === "message.delta" && id) {
    const current = messages.find((message) => message.id === id);
    if (!current || needsMessageSnapshot(messages, event)) return messages;
    const delta = typeof event.data.delta === "string" ? event.data.delta : "";
    return delta ? messages.map((message) => message.id === id
      ? {
        ...message,
        content: message.content + delta,
        streaming: true,
        generation: generationOf(event, message.generation ?? 1),
        status: "streaming",
      }
      : message) : messages;
  }
  if (event.type === "message.completed" && id) {
    if (event.data.finish_reason === "retry" || event.data.finish_reason === "interrupted") {
      return messages.filter((message) => message.id !== id);
    }
    return messages.map((message) => message.id === id
      ? {
        ...message,
        streaming: false,
        status: event.data.finish_reason === "cancelled" ? "cancelled" : event.data.finish_reason === "failed" ? "failed" : "ready",
      }
      : message);
  }
  return messages;
}

export function shouldRefreshThreadMessages(event: ThreadEvent): boolean {
  return event.type === "ask.answered" || event.type === "turn.accepted" || event.type === "research.queued"
    || event.type === "message.completed" || event.type === "turn.metrics.updated"
    || event.type === "expert.run.queued" || event.type === "expert.run.completed";
}

export interface ThreadTelemetry {
  thread: Thread | null;
  activeTurn: Turn | null;
  events: ThreadEvent[];
  messages: MessageRecord[];
  pendingAsk: PendingAsk | null;
  loading: boolean;
  error: string;
}

export function useThreadTelemetry(
  threadId: string | null,
  onMaterialized?: (runId: string) => void,
): ThreadTelemetry {
  const [thread, setThread] = useState<Thread | null>(null);
  const [events, setEvents] = useState<ThreadEvent[]>([]);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let close: () => void = () => undefined;
    setThread(null);
    setEvents([]);
    setMessages([]);
    setPendingAsk(null);
    setError("");
    if (!threadId) {
      setLoading(false);
      return () => { active = false; };
    }
    const id = threadId;
    setLoading(true);

    async function refreshMessages() {
      const result = await getThreadMessages(id);
      if (active) setMessages(hydrateThreadMessages(result.messages));
    }

    async function load() {
      try {
        const [threadResult, eventResult, messageResult] = await Promise.all([
          getThread(id),
          getThreadEvents(id),
          getThreadMessages(id),
        ]);
        if (!active) return;
        setThread(threadResult);
        setEvents(eventResult.events);
        setMessages(hydrateThreadMessages(messageResult.messages));
        setPendingAsk(pendingAskFromEvents(eventResult.events));
        const cursor = eventResult.events.at(-1)?.seq ?? 0;
        let eventCursor = cursor;
        close = subscribeToThreadEvents(id, cursor, (event) => {
          if (!active) return;
          if (event.seq > eventCursor + 1) {
            void getThreadEvents(id, eventCursor).then((result) => {
              if (!active) return;
              setEvents((current) => result.events.reduce(appendThreadEvent, current));
              setPendingAsk((current) => result.events.reduce(applyAskEvent, current));
              void refreshMessages().catch(() => undefined);
            }).catch(() => undefined);
          } else {
            setEvents((current) => appendThreadEvent(current, event));
            setPendingAsk((current) => applyAskEvent(current, event));
          }
          eventCursor = Math.max(eventCursor, event.seq);
          setMessages((current) => {
            if (needsMessageSnapshot(current, event)) {
              void refreshMessages().catch(() => undefined);
              return current;
            }
            return applyThreadEvent(current, event);
          });
          if (shouldRefreshThreadMessages(event)) void refreshMessages().catch(() => undefined);
          if (event.type === "execution.materialized" && typeof event.data.run_id === "string") {
            onMaterialized?.(event.data.run_id);
          }
          if (["turn.accepted", "turn.started", "turn.policy_decided", "turn.awaiting_input", "turn.awaiting_direction", "turn.awaiting_tool_approval", "turn.direction_selected", "turn.completed", "turn.failed", "turn.cancelled", "ask.requested", "ask.answered", "ask.cancelled", "chat_tool.approval_requested", "chat_tool.approved", "chat_tool.rejected", "chat_tool.cancelled", "chat_tool.resumed", "execution.materialized"].includes(event.type)) {
            void getThread(id).then((next) => { if (active) setThread(next); }).catch(() => undefined);
          }
        });
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "对话加载失败");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
      close();
    };
  }, [threadId, onMaterialized]);

  const activeTurn = thread?.turns?.find((turn) => turn.id === thread.active_turn_id) ?? null;
  return { thread, activeTurn, events, messages, pendingAsk, loading, error };
}
