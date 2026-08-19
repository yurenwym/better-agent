import { useEffect, useState } from "react";
import { getThread, getThreadEvents, getThreadMessages, subscribeToThreadEvents } from "../api";
import type { MessageRecord, Thread, ThreadEvent, ThreadMessage, Turn } from "../types";

function codePointLength(value: string): number {
  return Array.from(value).length;
}

export function hydrateThreadMessages(messages: ThreadMessage[]): MessageRecord[] {
  return messages.filter((message) => message.status !== "interrupted").map((message) => ({
    id: message.id,
    run_id: message.thread_id,
    interaction_id: null,
    role: message.role,
    content: message.content,
    created_at: message.created_at,
    streaming: message.status === "streaming",
    generation: message.generation,
    status: message.status,
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

export function applyThreadEvent(messages: MessageRecord[], event: ThreadEvent): MessageRecord[] {
  const id = messageId(event);
  if (event.type === "message.started" && id) {
    if (messages.some((message) => message.id === id)) return messages;
    return [...messages, {
      id,
      run_id: event.thread_id,
      interaction_id: null,
      role: "assistant",
      content: "",
      created_at: event.occurred_at,
      streaming: true,
      generation: generationOf(event),
      status: "streaming",
    }];
  }
  if (event.type === "message.snapshot" && id) {
    const content = typeof event.data.content === "string" ? event.data.content : "";
    const next = {
      id,
      run_id: event.thread_id,
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
        status: event.data.finish_reason === "cancelled" ? "cancelled" : "ready",
      }
      : message);
  }
  return messages;
}

export interface ThreadTelemetry {
  thread: Thread | null;
  activeTurn: Turn | null;
  events: ThreadEvent[];
  messages: MessageRecord[];
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let close: () => void = () => undefined;
    if (!threadId) {
      setThread(null);
      setEvents([]);
      setMessages([]);
      setLoading(false);
      setError("");
      return () => { active = false; };
    }
    const id = threadId;
    setLoading(true);
    setError("");

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
        const cursor = eventResult.events.at(-1)?.seq ?? 0;
        let eventCursor = cursor;
        close = subscribeToThreadEvents(id, cursor, (event) => {
          if (!active) return;
          if (event.seq > eventCursor + 1) {
            void getThreadEvents(id, eventCursor).then((result) => {
              if (!active) return;
              setEvents((current) => result.events.reduce(appendThreadEvent, current));
              void refreshMessages().catch(() => undefined);
            }).catch(() => undefined);
          } else {
            setEvents((current) => appendThreadEvent(current, event));
          }
          eventCursor = Math.max(eventCursor, event.seq);
          setMessages((current) => {
            if (needsMessageSnapshot(current, event)) {
              void refreshMessages().catch(() => undefined);
              return current;
            }
            return applyThreadEvent(current, event);
          });
          if (event.type === "execution.materialized" && typeof event.data.run_id === "string") {
            onMaterialized?.(event.data.run_id);
          }
          if (["turn.accepted", "turn.started", "turn.policy_decided", "turn.awaiting_direction", "turn.direction_selected", "turn.completed", "turn.failed", "turn.cancelled", "execution.materialized"].includes(event.type)) {
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
  return { thread, activeTurn, events, messages, loading, error };
}
