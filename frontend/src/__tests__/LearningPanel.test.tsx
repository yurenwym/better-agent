import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LearningPanel from "../components/LearningPanel";

const api = vi.hoisted(() => ({ getLearningPolicy: vi.fn(), getLearningHistory: vi.fn(), saveLearningPolicy: vi.fn(), suspendLearningJob: vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

it("saves explicit policy settings and distinguishes adoption from effect", async () => {
  api.getLearningPolicy.mockResolvedValue({version: 2, paused: true, config: {allowed_assets: ["memory"]}});
  api.getLearningHistory.mockResolvedValue({items: [{id: "job", version: 1, status: "APPLIED", created_at: "2026-09-10T00:00:00Z", provenance: "production", reason: "",
    changes: [{asset_type: "memory", adoption: "ACTIVE", effect: "UNKNOWN", after: "revision"}]}]});
  api.saveLearningPolicy.mockImplementation(async policy => ({...policy, version: 3}));
  render(<LearningPanel csrfToken="csrf" />);
  expect(await screen.findByText(/效果待验证/)).toBeTruthy();
  fireEvent.click(screen.getByLabelText("启用自主学习"));
  fireEvent.click(screen.getByRole("button", {name: "保存学习政策"}));
  await waitFor(() => expect(api.saveLearningPolicy).toHaveBeenCalledWith(expect.objectContaining({version: 2, paused: false}), "csrf"));
  expect(await screen.findByText(/学习政策已保存/)).toBeTruthy();
});
