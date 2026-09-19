import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import MemoryPage from "../pages/MemoryPage";

const api = vi.hoisted(() => ({
  archiveMemoryEntry: vi.fn(),
  createMemoryEntry: vi.fn(),
  decideMemoryProposal: vi.fn(),
  deleteMemoryEpisode: vi.fn(),
  getMemoryOverview: vi.fn(),
  purgeMemoryEntry: vi.fn(),
  restoreMemoryEntry: vi.fn(),
  updateMemoryEntry: vi.fn(),
  updateMemoryEpisode: vi.fn(),
}));
vi.mock("../api", () => api);
afterEach(cleanup);

const active = { id:"memory-1",kind:"preference",scope_type:"user",scope_id:"",status:"ACTIVE",content:"喜欢简洁回答",revision_id:"revision-1",revision_no:1,pinned:false,importance:.5,sensitivity:"normal",created_at:"",updated_at:"" } as const;
const archived = { ...active,id:"memory-2",status:"ARCHIVED",content:"使用中文回答" } as const;
const proposal = { id:"proposal-1",version:3,operation:"ADD",target_entry_id:null,base_revision_id:null,kind:"preference",scope_type:"user",scope_id:"",content:"偏好简洁回答",model_confidence:.91,evidence_state:"VERIFIED",independent_user_turn_count:2,evidence_label:"多次一致表达",evidence:[{source_type:"thread_message",source_id:"message-1",source_label:"用户消息",excerpt:"请尽量简洁"},{source_type:"thread_message",source_id:"message-2",source_label:"用户消息",excerpt:"再次要求减少解释"}],status:"PENDING",accepted_revision_id:null,reason:"用户多次明确表达",created_at:"" } as const;

beforeEach(() => {
  vi.clearAllMocks();
  api.getMemoryOverview.mockResolvedValue({entries:[active,archived],proposals:[proposal],episodes:[]});
  api.createMemoryEntry.mockResolvedValue({...active,id:"memory-new",content:"不要使用表情"});
  api.updateMemoryEntry.mockResolvedValue({...active,content:"修改后的内容",revision_id:"revision-2",revision_no:2});
  api.decideMemoryProposal.mockResolvedValue({...proposal,status:"ACCEPTED"});
  api.archiveMemoryEntry.mockResolvedValue({...active,status:"ARCHIVED"});
  api.restoreMemoryEntry.mockResolvedValue({...archived,status:"ACTIVE"});
  api.purgeMemoryEntry.mockResolvedValue(undefined);
});

it("shows natural-language evidence and sends edited approval with version and idempotency", async () => {
  render(<MemoryPage csrfToken="csrf"/>);
  expect(await screen.findByText("多次一致表达")).toBeTruthy();
  expect(screen.queryByText("91%")).toBeNull();
  fireEvent.click(screen.getByText("查看来源依据"));
  expect(screen.getByText("用户消息：请尽量简洁")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name:"编辑后确认"}));
  fireEvent.change(screen.getByLabelText("修改建议 proposal-1"), {target:{value:"偏好一句话回答"}});
  fireEvent.click(screen.getByRole("button", {name:"确认修改"}));
  await waitFor(() => expect(api.decideMemoryProposal).toHaveBeenCalledWith("proposal-1", expect.objectContaining({accept:true,accepted_content:"偏好一句话回答",expected_version:3,idempotency_key:expect.any(String)}), "csrf"));
});

it("uses explicit entry editing and keeps the failed draft", async () => {
  api.updateMemoryEntry.mockRejectedValueOnce(new Error("版本冲突，请重试"));
  render(<MemoryPage csrfToken="csrf"/>);
  fireEvent.click(await screen.findByRole("button", {name:"编辑"}));
  const editor = screen.getByLabelText("编辑 memory-1") as HTMLTextAreaElement;
  fireEvent.change(editor, {target:{value:"尚未成功保存的修改"}});
  fireEvent.click(screen.getByRole("button", {name:"保存"}));
  expect((await screen.findByRole("alert")).textContent).toContain("版本冲突，请重试");
  expect((screen.getByLabelText("编辑 memory-1") as HTMLTextAreaElement).value).toBe("尚未成功保存的修改");
});

it("reuses the idempotency key when the same failed edit is retried", async () => {
  api.updateMemoryEntry.mockRejectedValueOnce(new Error("网络中断"));
  render(<MemoryPage csrfToken="csrf"/>);
  fireEvent.click(await screen.findByRole("button", {name:"编辑"}));
  fireEvent.change(screen.getByLabelText("编辑 memory-1"), {target:{value:"重试同一修改"}});
  fireEvent.click(screen.getByRole("button", {name:"保存"}));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", {name:"保存"}));
  await waitFor(() => expect(api.updateMemoryEntry).toHaveBeenCalledTimes(2));
  expect(api.updateMemoryEntry.mock.calls[0][3]).toBe(api.updateMemoryEntry.mock.calls[1][3]);
});

it("requires confirmation before permanent deletion and restores an archived entry", async () => {
  render(<MemoryPage csrfToken="csrf"/>);
  const deleteButtons = await screen.findAllByRole("button", {name:"永久删除"});
  fireEvent.click(deleteButtons[0]);
  expect(api.purgeMemoryEntry).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", {name:"永久删除记忆？"})).toBeTruthy();
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", {name:"永久删除"}));
  await waitFor(() => expect(api.purgeMemoryEntry).toHaveBeenCalledWith("memory-1", expect.any(String), "csrf"));
  fireEvent.click(screen.getByRole("button", {name:"恢复"}));
  await waitFor(() => expect(api.restoreMemoryEntry).toHaveBeenCalledWith("memory-2", expect.any(String), "csrf"));
});

it("offers an actionable undo after creating a memory", async () => {
  api.getMemoryOverview.mockResolvedValueOnce({entries:[active],proposals:[],episodes:[]}).mockResolvedValue({entries:[active],proposals:[],episodes:[]});
  render(<MemoryPage csrfToken="csrf"/>);
  fireEvent.change(await screen.findByLabelText("新增长期记忆"), {target:{value:"不要使用表情"}});
  fireEvent.click(screen.getByRole("button", {name:"记住"}));
  expect(await screen.findByText("已记住“不要使用表情”")).toBeTruthy();
  expect(api.createMemoryEntry).toHaveBeenCalledWith(expect.objectContaining({content:"不要使用表情",idempotency_key:expect.any(String)}), "csrf");
  fireEvent.click(screen.getByRole("button", {name:"撤销"}));
  await waitFor(() => expect(api.archiveMemoryEntry).toHaveBeenCalledWith("memory-new", expect.any(String), "csrf"));
});
