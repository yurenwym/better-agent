import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ApprovalCard from "../components/ApprovalCard";

describe("goal activation approval", () => {
  it("shows the execution schedule and keeps document-only delivery available", () => {
    const grant = vi.fn();
    const reject = vi.fn();
    render(<ApprovalCard approvalId="approval-test" onGrant={grant} onReject={reject} details={{
      snapshot_hash: "sha256:test",
      preview: { objective_title: "旅行准备", start_date: "2026-09-21", end_date: "2026-09-22",
        timezone: "Asia/Shanghai", daily_minutes: 30,
        structure: { actions: [{ title: "确认出行安排", scheduled_date: "2026-09-21", estimated_minutes: 20 }] } },
    }} />);
    expect(screen.getByText(/确认出行安排/)).toBeTruthy();
    expect(screen.getByText(/只需要计划文档时可以拒绝/)).toBeTruthy();
    expect(screen.getByText(/不会因此开启主动提醒或自动复盘/)).toBeTruthy();
    expect(grant).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(reject).toHaveBeenCalledOnce();
    expect(grant).not.toHaveBeenCalled();
  });
});
