import { useEffect, useState } from "react";
import { getEvents, getMessages, getStats, subscribeToEvents } from "../api";
import type { EventRecord, MessageRecord, Stats } from "../types";

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
        setEvents(eventResult.events);
        setStats(statsResult);
        setMessages(messageResult.messages);
        const cursor = eventResult.events.at(-1)?.seq ?? 0;
        close = subscribeToEvents(id, cursor, (event) => {
          if (!active) return;
          setEvents((current) => current.some((item) => item.seq === event.seq) ? current : [...current, event].sort((left, right) => left.seq - right.seq));
          if (event.type === "model.response") {
            const content = event.data.content;
            const messageId = event.data.message_id;
            if (typeof content === "string" && typeof messageId === "string") {
              setMessages((current) => current.some((item) => item.id === messageId) ? current : [...current, {
                id: messageId,
                run_id: event.run_id,
                interaction_id: typeof event.data.interaction_id === "string" ? event.data.interaction_id : null,
                role: "assistant",
                content,
                created_at: event.occurred_at,
              }]);
            }
          }
          void getStats(id).then((nextStats) => {
            if (active) setStats(nextStats);
          }).catch(() => undefined);
        });
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
