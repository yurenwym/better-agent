import { useCallback, useEffect, useRef, useState } from "react";
import { getTrace } from "../api";
import { isTerminalTrace, mergeTraceEvents } from "../trajectory";
import type { TraceCompleteness, TraceEvent, TraceScope } from "../types";

export const TRACE_EMPTY_PAGE_BUDGET = 3;
export const TRACE_PAGE_BUDGET = 8;
export const TRACE_POLL_LIMIT = 4;
const DEFAULT_POLL_MS = 4000;

export interface TraceTelemetry {
  events: TraceEvent[];
  completeness: TraceCompleteness | null;
  legacyScanTruncated: boolean;
  cursor: string | null;
  loading: boolean;
  error: string;
  refresh: () => void;
  loadMore: () => void;
}

function scopeKey(scope: TraceScope | null): string {
  if (!scope) return "";
  return scope.kind === "task" ? `task:${scope.taskKind}:${scope.id}` : `${scope.kind}:${scope.id}`;
}

function aborted(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

async function collect(
  scope: TraceScope,
  start: string | null,
  signal: AbortSignal,
): Promise<{ events: TraceEvent[]; cursor: string | null; completeness: TraceCompleteness; legacyScanTruncated: boolean }> {
  let cursor = start;
  let events: TraceEvent[] = [];
  let emptyStreak = 0;
  let completeness: TraceCompleteness = { status: "exact", reasons: [] };
  let legacyScanTruncated = false;
  for (let page = 0; page < TRACE_PAGE_BUDGET; page += 1) {
    const body = await getTrace(scope, cursor, fetch, signal);
    completeness = body.completeness;
    legacyScanTruncated = legacyScanTruncated || Boolean(body.legacy_scan_truncated);
    events = mergeTraceEvents(events, body.events);
    if (!body.next_cursor) return { events, cursor: null, completeness, legacyScanTruncated };
    if (body.events.length === 0) {
      emptyStreak += 1;
      if (emptyStreak >= TRACE_EMPTY_PAGE_BUDGET) {
        return { events, cursor: body.next_cursor, completeness, legacyScanTruncated };
      }
    } else {
      emptyStreak = 0;
    }
    cursor = body.next_cursor;
  }
  return { events, cursor, completeness, legacyScanTruncated };
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("aborted", "AbortError"));
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    }, { once: true });
  });
}

export function useTraceTelemetry(scope: TraceScope | null, options?: { pollMs?: number }): TraceTelemetry {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [completeness, setCompleteness] = useState<TraceCompleteness | null>(null);
  const [legacyScanTruncated, setLegacyScanTruncated] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [resume, setResume] = useState(0);
  const cursorRef = useRef<string | null>(null);
  const scopeRef = useRef(scope);
  const resumeAbortRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  scopeRef.current = scope;
  cursorRef.current = cursor;
  const pollMs = options?.pollMs ?? DEFAULT_POLL_MS;
  const key = scopeKey(scope);
  const requestIdentity = `${key}\0${generation}`;
  const requestIdentityRef = useRef(requestIdentity);
  if (requestIdentityRef.current !== requestIdentity) {
    requestIdentityRef.current = requestIdentity;
    requestRef.current += 1;
  }

  const refresh = useCallback(() => setGeneration((value) => value + 1), []);
  const loadMore = useCallback(() => {
    if (cursorRef.current) setResume((value) => value + 1);
  }, []);

  useEffect(() => {
    const requestId = requestRef.current;
    const current = scopeRef.current;
    const currentKey = scopeKey(current);
    resumeAbortRef.current?.abort();
    const live = () => requestRef.current === requestId && scopeKey(scopeRef.current) === currentKey;
    if (!current) {
      setEvents([]);
      setCompleteness(null);
      setLegacyScanTruncated(false);
      setCursor(null);
      setError("");
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setEvents([]);
    setCompleteness(null);
    setLegacyScanTruncated(false);
    setCursor(null);
    setError("");
    setLoading(true);

    async function watch(target: TraceScope) {
      let polls = 0;
      try {
        while (live()) {
          const walk = await collect(target, null, controller.signal);
          if (!live()) return;
          setEvents(walk.events);
          setCompleteness(walk.completeness);
          setLegacyScanTruncated(walk.legacyScanTruncated);
          setCursor(walk.cursor);
          setLoading(false);
          if (walk.cursor || isTerminalTrace(walk.events, target) || polls >= TRACE_POLL_LIMIT) return;
          const known = new Set(walk.events.map((event) => event.event_id));
          await delay(pollMs, controller.signal);
          if (!live()) return;
          const again = await collect(target, null, controller.signal);
          if (!live()) return;
          const added = again.events.some((event) => !known.has(event.event_id));
          setEvents(again.events);
          setCompleteness(again.completeness);
          setLegacyScanTruncated(again.legacyScanTruncated);
          setCursor(again.cursor);
          polls += 1;
          if (!added || again.cursor || isTerminalTrace(again.events, target) || polls >= TRACE_POLL_LIMIT) return;
        }
      } catch (caught) {
        if (!live() || aborted(caught)) return;
        setError(caught instanceof Error ? caught.message : "轨迹加载失败");
        setLoading(false);
      }
    }

    void watch(current);
    return () => {
      requestRef.current += 1;
      controller.abort();
      resumeAbortRef.current?.abort();
    };
  }, [key, generation, pollMs]);

  useEffect(() => {
    const current = scopeRef.current;
    const nextCursor = cursorRef.current;
    if (!resume || !current || !nextCursor) return undefined;
    const requestId = requestRef.current;
    const currentKey = scopeKey(current);
    const controller = new AbortController();
    resumeAbortRef.current = controller;
    const live = () => requestRef.current === requestId && scopeKey(scopeRef.current) === currentKey;
    setLoading(true);
    setError("");
    void collect(current, nextCursor, controller.signal).then((walk) => {
      if (!live()) return;
      setEvents((existing) => mergeTraceEvents(existing, walk.events));
      setCompleteness(walk.completeness);
      setLegacyScanTruncated((value) => value || walk.legacyScanTruncated);
      setCursor(walk.cursor);
      setLoading(false);
    }).catch((caught) => {
      if (!live() || aborted(caught)) return;
      setError(caught instanceof Error ? caught.message : "轨迹加载失败");
      setLoading(false);
    });
    return () => {
      controller.abort();
      if (resumeAbortRef.current === controller) resumeAbortRef.current = null;
    };
  }, [resume]);

  return { events, completeness, legacyScanTruncated, cursor, loading, error, refresh, loadMore };
}
