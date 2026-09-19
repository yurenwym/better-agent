import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";

const api = vi.hoisted(() => ({
  deleteThread: vi.fn(),
  getBootstrap: vi.fn(),
  getLatestExpertRun: vi.fn(),
  listThreads: vi.fn(),
  setHumanMode: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  ...api,
}));

beforeEach(() => {
  api.getBootstrap.mockResolvedValue({ csrf_token: "csrf", human_mode: false, api_key_configured: true });
  api.listThreads.mockResolvedValue({ threads: [] });
  api.getLatestExpertRun.mockResolvedValue(null);
  api.setHumanMode.mockResolvedValue({ human_mode: true });
});

afterEach(() => { cleanup(); vi.clearAllMocks(); window.history.pushState({}, "", "/"); });

it("reports human mode toggle failures through the error toast", async () => {
  api.setHumanMode.mockRejectedValueOnce(new Error("backend offline"));
  render(<App />);
  await screen.findByText("模型已连接");

  fireEvent.click(screen.getByText("设置"));
  fireEvent.click(screen.getByLabelText("真人对话模式"));

  await waitFor(() => expect(api.setHumanMode).toHaveBeenCalledWith(true, "csrf"));
  expect((await screen.findByRole("alert")).textContent).toContain("真人对话模式设置失败");
});

it("keeps the human mode switch retryable after a failure", async () => {
  api.setHumanMode.mockRejectedValueOnce(new Error("backend offline"));
  render(<App />);
  await screen.findByText("模型已连接");

  fireEvent.click(screen.getByText("设置"));
  fireEvent.click(screen.getByLabelText("真人对话模式"));
  await waitFor(() => expect(api.setHumanMode).toHaveBeenCalledWith(true, "csrf"));
  expect((screen.getByLabelText("真人对话模式") as HTMLInputElement).checked).toBe(false);

  api.setHumanMode.mockResolvedValueOnce({ human_mode: true });
  fireEvent.click(screen.getByLabelText("真人对话模式"));
  await waitFor(() => expect(api.setHumanMode).toHaveBeenCalledTimes(2));
  expect((screen.getByLabelText("真人对话模式") as HTMLInputElement).checked).toBe(true);
});

it("shows a backend connection warning near the local mark when bootstrap fails", async () => {
  api.getBootstrap.mockRejectedValueOnce(new Error("backend down"));
  render(<App />);

  fireEvent.click(screen.getByText("控制台"));
  fireEvent.click(screen.getByRole("link", { name: "运行轨迹" }));

  expect(await screen.findByText("后端未连接，操作可能不可用")).toBeTruthy();
  expect(document.querySelector(".workspace-topbar .topbar-meta .backend-offline")).toBeTruthy();
});

it("keeps backend failures visible on the conversation shell too", async () => {
  api.getBootstrap.mockRejectedValueOnce(new Error("backend down"));
  render(<App />);

  await screen.findByRole("region", { name: "当前目标对话" });
  expect(await screen.findByText("后端未连接，操作可能不可用")).toBeTruthy();
});
