import { useEffect, useState } from "react";
import { getEvents, getMessages, getStats, subscribeToEvents } from "../api";
import type { EventRecord, MessageRecord, Stats } from "../types";

const terminalEventTypes = new Set(["run.completed", "run.failed", "run.cancelled"]);
const liveResponseEventTypes = new Set(["model.response.delta", "model.response.reset"]);

export function hasTerminalEvent(events: EventRecord[]): boolean {
  return events.some((event) => terminalEventTypes.has(event.type));
}

export function appendTelemetryEvent(events: EventRecord[], event: EventRecord): EventRecord[] {
  if (events.some((item) => item.seq === event.seq)) return events;
  const messageId = event.data.message_id;
  const sameMessage = typeof messageId === "string" && messageId.length > 0;
  const filtered = sameMessage && (liveResponseEventTypes.has(event.type) || event.type === "model.response")
    ? events.filter((item) => {
      if (item.data.message_id !== messageId) return true;
      if (event.type === "model.response") return !liveResponseEventTypes.has(item.type);
      if (event.type === "model.response.delta") return item.type !== "model.response.delta";
      return item.type !== "model.response.delta" && item.type !== "model.response.reset";
    })
    : events;
  return [...filtered, event].sort((left, right) => left.seq - right.seq);
}

export function coalesceTelemetryEvents(events: EventRecord[]): EventRecord[] {
  return events.reduce(appendTelemetryEvent, []);
}

export function hydrateMessages(messages: MessageRecord[], events: EventRecord[]): MessageRecord[] {
  const streamedIds = new Set(
    events
      .filter((event) => event.type === "model.response.delta")
      .map((event) => event.data.message_id)
      .filter((messageId): messageId is string => typeof messageId === "string" && messageId.length > 0),
  );
  const completedIds = new Set(
    events
      .filter((event) => event.type === "model.response")
      .map((event) => event.data.message_id)
      .filter((messageId): messageId is string => typeof messageId === "string" && messageId.length > 0),
  );
  return messages.map((message) => streamedIds.has(message.id) && !completedIds.has(message.id)
    ? { ...message, streaming: true }
    : message);
}

export function applyModelEvent(messages: MessageRecord[], event: EventRecord): MessageRecord[] {
  if (event.type !== "model.response.delta" && event.type !== "model.response" && event.type !== "model.response.reset") return messages;
  const messageId = event.data.message_id;
  if (typeof messageId !== "string" || !messageId) return messages;
  const existing = messages.find((message) => message.id === messageId);
  if (event.type === "model.response.reset") {
    if (existing) {
      return messages.map((message) => message.id === messageId
        ? { ...message, content: "", streaming: true }
        : message);
    }
    return [...messages, {
      id: messageId,
      run_id: event.run_id,
      interaction_id: typeof event.data.interaction_id === "string" ? event.data.interaction_id : null,
      role: "assistant",
      content: "",
      created_at: event.occurred_at,
      streaming: true,
    }];
  }
  if (event.type === "model.response.delta") {
    const delta = event.data.delta;
    if (typeof delta !== "string" || !delta) return messages;
    if (existing) {
      const contentLength = event.data.content_length;
      if (typeof contentLength === "number" && Number.isFinite(contentLength)
        && Array.from(existing.content).length >= contentLength) {
        return messages.map((message) => message.id === messageId ? { ...message, streaming: true } : message);
      }
      return messages.map((message) => message.id === messageId
        ? { ...message, content: message.content + delta, streaming: true }
        : message);
    }
    return [...messages, {
      id: messageId,
      run_id: event.run_id,
      interaction_id: typeof event.data.interaction_id === "string" ? event.data.interaction_id : null,
      role: "assistant",
      content: delta,
      created_at: event.occurred_at,
      streaming: true,
    }];
  }
  const content = event.data.content;
  if (typeof content !== "string") return messages;
  if (existing) {
    return messages.map((message) => message.id === messageId
      ? { ...message, content, streaming: false }
      : message);
  }
  return [...messages, {
    id: messageId,
    run_id: event.run_id,
    interaction_id: typeof event.data.interaction_id === "string" ? event.data.interaction_id : null,
    role: "assistant",
    content,
    created_at: event.occurred_at,
    streaming: false,
  }];
}

export interface RunTelemetry {
  events: EventRecord[];
  messages: MessageRecord[];
  stats: Stats | null;
  loading: boolean;
  error: string;
}

export function useRunTelemetry(runId: string | null, runVersion = 0): RunTelemetry {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let close: () => void = () => undefined;

    if (!runId) {
      setEvents([]);
      setMessages([]);
      setStats(null);
      setLoading(false);
      setError("");
      return () => { active = false; };
    }

    const id = runId;
    setLoading(true);
    setError("");

    async function load() {
      try {
        const [eventResult, statsResult, messageResult] = await Promise.all([getEvents(id), getStats(id), getMessages(id)]);
        if (!active) return;
        setEvents(coalesceTelemetryEvents(eventResult.events));
        setStats(statsResult);
        setMessages(hydrateMessages(messageResult.messages, eventResult.events));
        const cursor = eventResult.events.at(-1)?.seq ?? 0;
        const terminalState = ["COMPLETED", "FAILED", "CANCELLED"].includes(statsResult.state ?? "");
        if (!hasTerminalEvent(eventResult.events) && !terminalState) {
          close = subscribeToEvents(id, cursor, (event) => {
            if (!active) return;
            setEvents((current) => appendTelemetryEvent(current, event));
            if (["model.response", "model.response.delta", "model.response.reset"].includes(event.type)) {
              setMessages((current) => applyModelEvent(current, event));
            }
            if (event.type !== "model.response.delta" && event.type !== "model.response.reset") {
              void getStats(id).then((nextStats) => {
                if (active) setStats(nextStats);
              }).catch(() => undefined);
            }
          });
        }
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "运行轨迹加载失败");
      } finally {
        if (active) setLoading(false);
      }
    }

    void load();
    return () => {
      active = false;
      close();
    };
  }, [runId, runVersion]);

  return { events, messages, stats, loading, error };
}
