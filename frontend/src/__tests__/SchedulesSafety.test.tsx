import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SchedulesPage from "../pages/SchedulesPage";

const api = vi.hoisted(() => ({
  createNotificationChannel: vi.fn(),
  createSchedule: vi.fn(),
  deleteNotificationChannel: vi.fn(),
  deleteSchedule: vi.fn(),
  getNotificationChannels: vi.fn(),
  getSchedules: vi.fn(),
  runSchedule: vi.fn(),
  updateSchedule: vi.fn(),
}));
vi.mock("../api", () => api);
afterEach(cleanup);

const schedule = {
  id: "schedule-1", name: "晨间研究", thread_id: "thread-1", topic: "AI Agent 岗位", source_scopes: ["web"],
  trigger_type: "weekly" as const, trigger_time: "09:00", trigger_weekday: 0, interval_hours: null,
  timezone: "Asia/Shanghai", enabled: true, notify_enabled: true,
  next_run_at: null, last_run_at: null, last_job_id: null,
  created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z",
};
const channel = { id: "channel-1", name: "飞书通知", channel_type: "webhook" as const, secret_env_name: "FEISHU_WEBHOOK", enabled: true, configured: true };

beforeEach(() => {
  vi.clearAllMocks();
  api.getSchedules.mockResolvedValue({ schedules: [schedule] });
  api.getNotificationChannels.mockResolvedValue({ channels: [channel] });
  api.deleteSchedule.mockResolvedValue(undefined);
  api.deleteNotificationChannel.mockResolvedValue(undefined);
});

describe("SchedulesPage destructive safety", () => {
  it("opens a confirmation dialog before deleting a schedule and cancels without deleting", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    expect(await screen.findByText("晨间研究")).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(api.deleteSchedule).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "删除定时任务？" });
    expect(dialog.textContent).toContain("晨间研究");

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.deleteSchedule).not.toHaveBeenCalled();
  });

  it("deletes the schedule only after the default confirmation", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    fireEvent.click((await screen.findAllByRole("button", { name: "删除" }))[0]);

    fireEvent.click(within(screen.getByRole("dialog", { name: "删除定时任务？" })).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deleteSchedule).toHaveBeenCalledWith("schedule-1", "csrf"));
    await waitFor(() => expect(api.getSchedules).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens its own confirmation dialog before deleting a notification channel", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    const deleteButtons = await screen.findAllByRole("button", { name: "删除" });
    fireEvent.click(deleteButtons[1]);
    expect(api.deleteNotificationChannel).not.toHaveBeenCalled();

    fireEvent.click(within(screen.getByRole("dialog", { name: "删除通知渠道？" })).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deleteNotificationChannel).toHaveBeenCalledWith("channel-1", "csrf"));
  });

  it("shows a visible error and closes the dialog when the delete fails", async () => {
    api.deleteSchedule.mockRejectedValueOnce(new Error("后端不可用"));
    render(<SchedulesPage csrfToken="csrf" />);
    fireEvent.click((await screen.findAllByRole("button", { name: "删除" }))[0]);
    fireEvent.click(within(screen.getByRole("dialog", { name: "删除定时任务？" })).getByRole("button", { name: "确认删除" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("后端不可用");
    expect(api.deleteSchedule).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
