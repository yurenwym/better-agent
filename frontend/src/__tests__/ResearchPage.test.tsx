import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import ResearchPage from "../pages/ResearchPage";

const api = vi.hoisted(() => ({
  deleteResearch: vi.fn(),
  getResearchJobs: vi.fn(),
  getResearchReport: vi.fn(),
  retryResearch: vi.fn(),
}));
vi.mock("../api", () => api);

const job = { id:"research-1",thread_id:"thread-1",source_turn_id:"turn-1",schedule_id:null,retry_of_job_id:null,trigger_kind:"manual",topic:"SQLite WAL",source_scopes:["web"],status:"COMPLETED" as const,phase:"completed",attempts:1,cancel_requested_at:null,created_at:"2026-08-23T00:00:00Z",updated_at:"2026-08-23T00:00:00Z",title:"SQLite WAL 研究",source_count:20,evidence_count:48,assistant_message_id:"message-1" };

beforeEach(() => {
  api.getResearchJobs.mockResolvedValue({ jobs: [job] });
  api.getResearchReport.mockResolvedValue({ job_id:job.id,title:job.title,markdown:"# SQLite WAL 研究" });
  api.deleteResearch.mockResolvedValue(undefined);
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

it("opens a report and deletes it after confirmation", async () => {
  render(<ResearchPage csrfToken="csrf" />);
  fireEvent.click(await screen.findByRole("button", { name:/SQLite WAL 研究/ }));
  expect(await screen.findByRole("heading", { name:"SQLite WAL 研究" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name:"删除研究" }));
  await waitFor(() => expect(api.deleteResearch).toHaveBeenCalledWith(job.id,"csrf"));
  expect(window.confirm).toHaveBeenCalled();
  expect(screen.queryByRole("button", { name:/SQLite WAL 研究/ })).toBeNull();
});
