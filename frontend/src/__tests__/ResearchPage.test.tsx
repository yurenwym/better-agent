import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import ResearchPage from "../pages/ResearchPage";

const api = vi.hoisted(() => ({
  createResearch: vi.fn(),
  createThread: vi.fn(),
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
  api.createThread.mockResolvedValue({id:"thread-new",title:"研究",version:0,active_turn_id:null,next_event_seq:1,turns:[]});
  api.createResearch.mockResolvedValue({job_id:"research-new",status:"QUEUED",event_cursor:1});
});

it("starts an independent deep research job from a topic",async()=>{
  const queuedJob={...job,id:"research-new",thread_id:"thread-new",source_turn_id:"turn-new",topic:"调研 AI Agent 开发岗位",title:null,status:"QUEUED" as const,phase:"queued",attempts:0,source_count:0,evidence_count:0,assistant_message_id:null};
  api.getResearchJobs.mockResolvedValueOnce({jobs:[]}).mockResolvedValueOnce({jobs:[queuedJob]});
  render(<ResearchPage csrfToken="csrf"/>);
  fireEvent.change(await screen.findByLabelText("研究主题"),{target:{value:"调研 AI Agent 开发岗位"}});
  fireEvent.click(screen.getByRole("button",{name:"开始研究"}));
  await waitFor(()=>expect(api.createThread).toHaveBeenCalledWith({title:"调研 AI Agent 开发岗位"},"csrf"));
  expect(api.createResearch).toHaveBeenCalledWith("thread-new",expect.objectContaining({topic:"调研 AI Agent 开发岗位",source_scopes:["web"]}),"csrf");
  expect(await screen.findByLabelText("深度研究进度")).toBeTruthy();
});

it("opens a report and deletes it after confirmation", async () => {
  render(<ResearchPage csrfToken="csrf" />);
  fireEvent.click(await screen.findByRole("button", { name:/SQLite WAL 研究/ }));
  expect(await screen.findByRole("heading", { name:"SQLite WAL 研究" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name:"删除研究" }));
  expect(api.deleteResearch).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name:"删除研究？" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name:"确认删除" }));
  await waitFor(() => expect(api.deleteResearch).toHaveBeenCalledWith(job.id,"csrf"));
  expect(screen.queryByRole("button", { name:/SQLite WAL 研究/ })).toBeNull();
});
