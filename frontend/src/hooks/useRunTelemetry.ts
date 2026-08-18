import { useEffect, useState } from "react";
import { getEvents, getStats, subscribeToEvents } from "../api";
import type { EventRecord, Stats } from "../types";

export interface RunTelemetry {
  events: EventRecord[];
  stats: Stats | null;
  loading: boolean;
  error: string;
}

export function useRunTelemetry(runId: string | null): RunTelemetry {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let close: () => void = () => undefined;

    if (!runId) {
      setEvents([]);
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
        const [eventResult, statsResult] = await Promise.all([getEvents(id), getStats(id)]);
        if (!active) return;
        setEvents(eventResult.events);
        setStats(statsResult);
        const cursor = eventResult.events.at(-1)?.seq ?? 0;
        close = subscribeToEvents(id, cursor, (event) => {
          if (!active) return;
          setEvents((current) => current.some((item) => item.seq === event.seq) ? current : [...current, event].sort((left, right) => left.seq - right.seq));
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
  }, [runId]);

  return { events, stats, loading, error };
}
