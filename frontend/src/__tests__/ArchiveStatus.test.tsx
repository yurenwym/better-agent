import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ArchiveStatus from "../components/ArchiveStatus";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows a failed archive and submits an explicit scoped retry", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ archived_through_seq: 0, jobs: [{ id: "job", status: "DEAD_LETTER", end_message_seq: 5, updated_at: "version-one" }] }) })
    .mockResolvedValueOnce({ ok: true }).mockResolvedValue({ ok: true, json: async () => ({ archived_through_seq: 0, jobs: [{ id: "job", status: "QUEUED", end_message_seq: 5 }] }) });
  vi.stubGlobal("fetch", fetcher);
  render(<ArchiveStatus threadId="thread" csrfToken="token" />);
  fireEvent.click(await screen.findByRole("button", { name: "重试归档" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/threads/thread/archive/job/retry", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_updated_at: "version-one" }) })));
  expect(await screen.findByText("正在整理历史上下文")).toBeTruthy();
});

it("does not show obsolete failures already covered by a committed archive", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ archived_through_seq: 5, jobs: [{ id: "job", status: "DEAD_LETTER", end_message_seq: 5 }] }) }));
  render(<ArchiveStatus threadId="thread" csrfToken="token" />);
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "重试归档" })).toBeNull();
});

function liveEvent(type: string, data: Record<string, unknown>, seq: number) {
  return {
    schema_version: 1, event_id: `e-${seq}`, seq, thread_id: "thread", turn_id: "turn",
    type, occurred_at: "2026-09-19T00:00:00Z", actor: "worker", data,
  } as never;
}

it("shows the live wait status from the event stream before the poll resolves", async () => {
  // The poll never resolves to a pending job; only the stream knows.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ archived_through_seq: 99, jobs: [] }) }));
  render(<ArchiveStatus
    threadId="thread"
    csrfToken="token"
    events={[liveEvent("context.archiving", { state: "waiting", waited_ms: 300 }, 1)]}
  />);
  expect(await screen.findByRole("status")).toBeTruthy();
  expect(screen.getByText("正在整理历史上下文")).toBeTruthy();
});

it("reports a timed-out wait as an alert and offers the retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ archived_through_seq: 0, jobs: [{ id: "job", status: "RUNNING", end_message_seq: 5, updated_at: "v1" }] }) }));
  render(<ArchiveStatus
    threadId="thread"
    csrfToken="token"
    events={[liveEvent("context.archive_wait_timeout", { message: "稍后可以直接继续" }, 1)]}
  />);
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getByText("历史整理超时，本轮未生成回答")).toBeTruthy();
  expect(await screen.findByRole("button", { name: "重试归档" })).toBeTruthy();
});
