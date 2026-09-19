import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ApprovalCard from "../components/ApprovalCard";

afterEach(cleanup);

describe("ApprovalCard failure feedback", () => {
  it("shows an inline alert when granting fails and keeps the action retryable", async () => {
    const onGrant = vi.fn().mockRejectedValueOnce(new Error("network down"));
    const onReject = vi.fn().mockResolvedValue(undefined);
    render(<ApprovalCard approvalId="approval-1" onGrant={onGrant} onReject={onReject} />);

    fireEvent.click(screen.getByRole("button", { name: "批准" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("审批操作失败");
    expect(screen.getByRole("button", { name: "批准" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "拒绝" }).hasAttribute("disabled")).toBe(false);

    onGrant.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => expect(onGrant).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a rejection failure instead of swallowing the promise", async () => {
    const onGrant = vi.fn().mockResolvedValue(undefined);
    const onReject = vi.fn().mockRejectedValue(new Error("backend busy"));
    render(<ApprovalCard approvalId="approval-2" onGrant={onGrant} onReject={onReject} />);

    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("审批操作失败");
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "拒绝" }).hasAttribute("disabled")).toBe(false);

    onReject.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(onReject).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
