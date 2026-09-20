import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderApp } from "./renderApp";

const api = vi.hoisted(() => ({
  deleteThread: vi.fn(),
  getBootstrap: vi.fn(),
  listThreads: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  ...api,
}));

beforeEach(() => {
  api.getBootstrap.mockResolvedValue({ csrf_token: "csrf", human_mode: false });
  api.listThreads.mockResolvedValue({ threads: [{ id: "thread-1", title: "骑行计划", version: 1, active_turn_id: null, next_event_seq: 2, created_at: "2026-08-24T01:00:00Z", updated_at: "2026-08-24T01:00:00Z" }] });
  api.deleteThread.mockResolvedValue(undefined);
});

afterEach(() => { cleanup(); vi.clearAllMocks(); window.history.pushState({}, "", "/"); });

it("confirms conversation deletion in the app and reports failures without alert", async () => {
  api.deleteThread.mockRejectedValueOnce(new Error("busy"));
  renderApp();

  fireEvent.click(await screen.findByRole("button", { name: "删除会话：骑行计划" }));
  expect(screen.getByRole("dialog", { name: "删除会话？" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

  await waitFor(() => expect(api.deleteThread).toHaveBeenCalledWith("thread-1", "csrf"));
  expect((await screen.findByRole("alert")).textContent).toContain("删除会话失败");
});

it("confirms successful conversation deletion", async () => {
  renderApp();

  fireEvent.click(await screen.findByRole("button", { name: "删除会话：骑行计划" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

  expect(await screen.findByText("会话已删除")).toBeTruthy();
});
