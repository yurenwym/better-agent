import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ResearchPage from "../pages/ResearchPage";

const api = vi.hoisted(() => ({
  createResearch: vi.fn(),
  createThread: vi.fn(),
  cancelResearch: vi.fn(),
  deleteResearch: vi.fn(),
  getResearchJob: vi.fn(),
  getResearchJobs: vi.fn(),
  getResearchReport: vi.fn(),
  getResearchSources: vi.fn(),
  retryResearch: vi.fn(),
}));
vi.mock("../api", () => api);

const job = { id:"research-1",thread_id:"thread-1",source_turn_id:"turn-1",schedule_id:null,retry_of_job_id:null,trigger_kind:"manual",topic:"SQLite WAL",source_scopes:["web"],status:"COMPLETED" as const,phase:"completed",attempts:1,cancel_requested_at:null,created_at:"2026-08-23T00:00:00Z",updated_at:"2026-08-23T00:00:00Z",title:"SQLite WAL 研究",source_count:20,evidence_count:48,assistant_message_id:"message-1" };

beforeEach(() => {
  vi.resetAllMocks();
  api.getResearchJobs.mockResolvedValue({ jobs: [job] });
  api.getResearchJob.mockResolvedValue(job);
  api.getResearchReport.mockResolvedValue({ job_id:job.id,title:job.title,markdown:"# SQLite WAL 研究" });
  api.getResearchSources.mockResolvedValue({ sources: [] });
  api.deleteResearch.mockResolvedValue(undefined);
  api.createThread.mockResolvedValue({id:"thread-new",title:"研究",version:0,active_turn_id:null,next_event_seq:1,turns:[]});
  api.createResearch.mockResolvedValue({job_id:"research-new",status:"QUEUED",event_cursor:1});
  api.cancelResearch.mockResolvedValue({...job,status:"CANCELLED",phase:"cancelled"});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("starts an independent deep research job from a topic",async()=>{
  const queuedJob={...job,id:"research-new",thread_id:"thread-new",source_turn_id:"turn-new",topic:"调研 AI Agent 开发岗位",title:null,status:"QUEUED" as const,phase:"queued",attempts:0,source_count:0,evidence_count:0,assistant_message_id:null};
  api.getResearchJobs.mockResolvedValueOnce({jobs:[]}).mockResolvedValueOnce({jobs:[queuedJob]});
  api.getResearchJob.mockResolvedValue(queuedJob);
  const onSelectJob = vi.fn();
  render(<ResearchPage csrfToken="csrf" onSelectJob={onSelectJob}/>);
  fireEvent.change(await screen.findByLabelText("研究主题"),{target:{value:"调研 AI Agent 开发岗位"}});
  fireEvent.click(screen.getByRole("button",{name:"开始研究"}));
  await waitFor(()=>expect(api.createThread).toHaveBeenCalledWith({title:"调研 AI Agent 开发岗位"},"csrf"));
  expect(api.createResearch).toHaveBeenCalledWith("thread-new",expect.objectContaining({topic:"调研 AI Agent 开发岗位",source_scopes:["web"]}),"csrf");
  expect(await screen.findByLabelText("深度研究进度")).toBeTruthy();
  expect(onSelectJob).toHaveBeenCalledWith("research-new");
});

it("opens a report and deletes it after confirmation", async () => {
  const onSelectJob = vi.fn();
  render(<ResearchPage csrfToken="csrf" onSelectJob={onSelectJob} />);
  fireEvent.click(await screen.findByRole("button", { name:/SQLite WAL 研究/ }));
  expect(await screen.findByRole("heading", { name:"SQLite WAL 研究" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name:"删除研究" }));
  expect(api.deleteResearch).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name:"删除研究？" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name:"确认删除" }));
  await waitFor(() => expect(api.deleteResearch).toHaveBeenCalledWith(job.id,"csrf"));
  expect(screen.queryByRole("button", { name:/SQLite WAL 研究/ })).toBeNull();
  expect(screen.getByText("研究已删除")).toBeTruthy();
  expect(onSelectJob).toHaveBeenNthCalledWith(1, job.id);
  expect(onSelectJob).toHaveBeenLastCalledWith(null);
});

it("shows a deletion failure notice and keeps the research", async () => {
  api.deleteResearch.mockRejectedValueOnce(new Error("running"));
  render(<ResearchPage csrfToken="csrf" />);
  fireEvent.click(await screen.findByRole("button", { name:/SQLite WAL 研究/ }));
  fireEvent.click(await screen.findByRole("button", { name:"删除研究" }));
  fireEvent.click(screen.getByRole("button", { name:"确认删除" }));

  expect(await screen.findByText("研究删除失败。正在运行的研究需要先取消。")).toBeTruthy();
  expect(screen.getByRole("button", { name:/SQLite WAL 研究/ })).toBeTruthy();
});

it("loads a bookmarked report outside the recent history and opens sources only on demand", async () => {
  api.getResearchJobs.mockResolvedValue({jobs:[]});
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);

  expect(await screen.findByRole("heading", {name:job.title})).toBeTruthy();
  expect(api.getResearchJob).toHaveBeenCalledWith(job.id);
  expect(api.getResearchReport).toHaveBeenCalledWith(job.id);
  expect(screen.getByRole("button", {name:/SQLite WAL 研究/})).toBeTruthy();
  expect(api.getResearchSources).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("来源与证据"));
  await waitFor(() => expect(api.getResearchSources).toHaveBeenCalledWith(job.id));
});

it("notifies the parent when starting a new report or opening schedules", async () => {
  const onSelectJob = vi.fn(), onOpenSchedules = vi.fn();
  render(<ResearchPage csrfToken="csrf" jobId={job.id} onSelectJob={onSelectJob} onOpenSchedules={onOpenSchedules}/>);
  await screen.findByRole("heading", {name:job.title});
  fireEvent.click(screen.getByRole("button", {name:"新研究"}));
  expect(onSelectJob).toHaveBeenCalledWith(null);
  fireEvent.click(screen.getByRole("button", {name:"定时研究"}));
  expect(onOpenSchedules).toHaveBeenCalledOnce();
});

it("automatically loads the report when the selected running job completes", async () => {
  vi.useFakeTimers();
  api.getResearchJob.mockResolvedValueOnce({...job,status:"RUNNING",phase:"writing"}).mockResolvedValue(job);
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  await act(async () => {});
  expect(api.getResearchReport).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByRole("heading", {name:job.title})).toBeTruthy();
  expect(api.getResearchReport).toHaveBeenCalledTimes(1);
});

it("ignores a report response that arrives after the user selects another research", async () => {
  let finishFirst: (value: {markdown:string}) => void = () => {};
  const second = {...job,id:"research-2",title:"另一份研究"};
  api.getResearchJob.mockImplementation(async id => id === second.id ? second : job);
  api.getResearchReport.mockImplementation(id => id === second.id ? Promise.resolve({markdown:"# 新报告"}) : new Promise(resolve => {finishFirst=resolve;}));
  const page = render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  await waitFor(() => expect(api.getResearchReport).toHaveBeenCalledWith(job.id));
  page.rerender(<ResearchPage csrfToken="csrf" jobId={second.id}/>);
  expect(await screen.findByRole("heading", {name:"新报告"})).toBeTruthy();
  await act(async () => {finishFirst({markdown:"# 旧报告"});});
  expect(screen.queryByRole("heading", {name:"旧报告"})).toBeNull();
  expect(screen.getByRole("heading", {name:"新报告"})).toBeTruthy();
});

it("keeps partial result warnings next to the automatically opened report", async () => {
  api.getResearchJob.mockResolvedValue({...job,status:"PARTIAL",phase:"partial",missing_requirements:["最新投递截止时间"]});
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  expect(await screen.findByRole("heading", {name:job.title})).toBeTruthy();
  expect(screen.getAllByText("研究已部分完成").length).toBeGreaterThan(0);
  expect(screen.getAllByText("仍缺少：最新投递截止时间").length).toBeGreaterThan(0);
});

it("keeps a failed report retryable and updates the selected job", async () => {
  api.getResearchJob.mockResolvedValue({...job,status:"FAILED",phase:"failed",failure_reason_code:"search_timeout"});
  api.retryResearch.mockResolvedValue({job_id:"research-retry",status:"QUEUED"});
  const onSelectJob = vi.fn();
  render(<ResearchPage csrfToken="csrf" jobId={job.id} onSelectJob={onSelectJob}/>);
  fireEvent.click(await screen.findByRole("button", {name:"重新研究"}));
  await waitFor(() => expect(api.retryResearch).toHaveBeenCalledWith(job.id,"csrf"));
  expect(onSelectJob).toHaveBeenCalledWith("research-retry");
});

it("cancels an active research without clearing its details", async () => {
  api.getResearchJob.mockResolvedValue({...job,status:"RUNNING",phase:"retrieving"});
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  fireEvent.click(await screen.findByRole("button", {name:"取消研究"}));
  await waitFor(() => expect(api.cancelResearch).toHaveBeenCalledWith(job.id,"csrf"));
  expect(await screen.findByRole("button", {name:"删除研究"})).toBeTruthy();
  expect(screen.getByLabelText("深度研究进度")).toBeTruthy();
});

it("distinguishes unavailable research from an empty history and can retry", async () => {
  api.getResearchJobs.mockResolvedValue({jobs:[]});
  api.getResearchJob.mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue(job);
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  fireEvent.click(await screen.findByRole("button", {name:"重新加载研究"}));
  expect(await screen.findByRole("heading", {name:job.title})).toBeTruthy();
  expect(screen.queryByText("这份研究暂时无法加载，请重试或返回研究记录。")).toBeNull();
});

it("does not let history refresh clear a report failure and offers an independent retry", async () => {
  vi.useFakeTimers();
  api.getResearchReport.mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue({markdown:"# 已恢复报告"});
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  await act(async () => {});
  expect(screen.getByText("报告暂时无法加载，请稍后重试。")).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByText("报告暂时无法加载，请稍后重试。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name:"重新加载报告"}));
  await act(async () => {});
  expect(screen.getByRole("heading", {name:"已恢复报告"})).toBeTruthy();
});

it("keeps a report visible when the recent history request fails", async () => {
  api.getResearchJobs.mockRejectedValue(new Error("unavailable"));
  render(<ResearchPage csrfToken="csrf" jobId={job.id}/>);
  expect(await screen.findByRole("heading", {name:job.title})).toBeTruthy();
  expect(screen.getByText("暂时无法加载研究记录，请稍后重试。")).toBeTruthy();
  expect(screen.queryByText("还没有研究记录")).toBeNull();
});
