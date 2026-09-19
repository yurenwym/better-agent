import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

beforeEach(() => {
  vi.clearAllMocks();
  api.getSchedules.mockResolvedValue({ schedules: [schedule] });
  api.getNotificationChannels.mockResolvedValue({ channels: [] });
  api.createSchedule.mockResolvedValue(schedule);
});

function fillBase() {
  fireEvent.change(screen.getByLabelText("名称"), { target: { value: "晚间研究" } });
  fireEvent.change(screen.getByLabelText("研究主题"), { target: { value: "向量数据库选型" } });
}

describe("SchedulesPage", () => {
  it("creates a daily schedule by default", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    fillBase();
    fireEvent.click(screen.getByRole("button", { name: "每天 09:00 创建" }));

    await waitFor(() => expect(api.createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ trigger_type: "daily", trigger_time: "09:00", trigger_weekday: null, interval_hours: null, timezone: "Asia/Shanghai" }),
      "csrf",
    ));
  });

  it("creates a weekly schedule with the chosen weekday", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    fillBase();
    fireEvent.change(screen.getByLabelText("触发方式"), { target: { value: "weekly" } });
    fireEvent.change(screen.getByLabelText("星期"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "每周三 09:00 创建" }));

    await waitFor(() => expect(api.createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ trigger_type: "weekly", trigger_time: "09:00", trigger_weekday: 2, interval_hours: null }),
      "csrf",
    ));
  });

  it("creates an interval schedule without a fixed time of day", async () => {
    render(<SchedulesPage csrfToken="csrf" />);
    fillBase();
    fireEvent.change(screen.getByLabelText("触发方式"), { target: { value: "interval_hours" } });
    fireEvent.change(screen.getByLabelText("间隔小时"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "每 6 小时创建" }));

    await waitFor(() => expect(api.createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ trigger_type: "interval_hours", trigger_time: null, trigger_weekday: null, interval_hours: 6 }),
      "csrf",
    ));
  });

  it("renders the trigger in Chinese instead of the raw code", async () => {
    render(<SchedulesPage csrfToken="csrf" />);

    expect(await screen.findByText(/每周一 09:00/)).toBeTruthy();
    expect(screen.queryByText(/weekly ·/)).toBeNull();
  });
});
