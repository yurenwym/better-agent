import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import ConversationThread, { moveConversationDraft } from "../components/ConversationThread";

afterEach(cleanup);

function deferred() {
  let resolve: (value: boolean) => void = () => undefined;
  const promise = new Promise<boolean>((res) => { resolve = res; });
  return { promise, resolve };
}

// Resolve inside act so the awaited submit continuation flushes before we assert.
async function settle(send: { resolve: (value: boolean) => void }, value = true) {
  await act(async () => { send.resolve(value); await Promise.resolve(); });
}

const inputValue = () => (screen.getByLabelText("输入消息") as HTMLTextAreaElement).value;

// R02 race: an accepted send used to erase whatever was in the composer when it
// finally resolved, including text typed after the send started.
it("keeps a later edit typed while an earlier send is still in flight", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="race-inflight" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "第一条消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "第二条消息" } });
  await settle(send);
  expect(inputValue()).toBe("第二条消息");
  view.unmount();
  render(<ConversationThread messages={[]} draftKey="race-inflight" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("第二条消息");
});

// Ownership, not text: B holds the same text as the submitted A draft but must survive.
it("keeps an unrelated conversation's identical-text draft when an earlier send resolves", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="race-same-a" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "same text" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  view.rerender(<ConversationThread messages={[]} draftKey="race-same-b" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "same text" } });
  await settle(send);
  expect(inputValue()).toBe("same text");
  view.unmount();
  const other = render(<ConversationThread messages={[]} draftKey="race-same-b" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("same text");
  other.unmount();
  render(<ConversationThread messages={[]} draftKey="race-same-a" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("");
});

// The real new->thread move: an unchanged accepted draft clears both live and persisted value.
it("clears the moved new->thread draft only when unchanged", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="new" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "首条消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  moveConversationDraft("new", "race-move-thread");
  view.rerender(<ConversationThread messages={[]} draftKey="race-move-thread" onSubmit={() => send.promise} />);
  await settle(send);
  expect(inputValue()).toBe("");
  view.unmount();
  render(<ConversationThread messages={[]} draftKey="race-move-thread" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("");
});

it("keeps newer input on the moved thread when the earlier new-draft send resolves", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="new" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "被发送的首条" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  moveConversationDraft("new", "race-move-thread-2");
  view.rerender(<ConversationThread messages={[]} draftKey="race-move-thread-2" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "移动后新输入" } });
  await settle(send);
  expect(inputValue()).toBe("移动后新输入");
  view.unmount();
  render(<ConversationThread messages={[]} draftKey="race-move-thread-2" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("移动后新输入");
});

it("clears a no-key composer on acceptance and preserves later input", async () => {
  const clearSend = deferred();
  const view = render(<ConversationThread messages={[]} onSubmit={() => clearSend.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "无 key 发送" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await settle(clearSend);
  expect(inputValue()).toBe("");

  const keepSend = deferred();
  view.rerender(<ConversationThread messages={[]} onSubmit={() => keepSend.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "第一次" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "后来输入" } });
  await settle(keepSend);
  expect(inputValue()).toBe("后来输入");
});

it("clears the composer when the accepted send still owns the current draft", async () => {
  const send = deferred();
  render(<ConversationThread messages={[]} draftKey="race-clear" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "只发送这一条" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await settle(send);
  expect(inputValue()).toBe("");
});

it("preserves the draft when the send is rejected", async () => {
  const send = deferred();
  render(<ConversationThread messages={[]} draftKey="race-reject" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "失败也要保留" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await settle(send, false);
  expect(inputValue()).toBe("失败也要保留");
  expect(screen.queryByText("发送中")).toBeNull();
});

// P2: the moved record must clear wherever it lives, even after navigation moves on again.
it("clears a moved draft even when navigation changes again before the send resolves", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="new" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "same text" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  moveConversationDraft("new", "race-nav-thread");
  view.rerender(<ConversationThread messages={[]} draftKey="race-nav-thread" onSubmit={() => send.promise} />);
  view.rerender(<ConversationThread messages={[]} draftKey="race-nav-b" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "same text" } });
  await settle(send);
  expect(inputValue()).toBe("same text");
  view.unmount();
  const other = render(<ConversationThread messages={[]} draftKey="race-nav-b" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("same text");
  other.unmount();
  render(<ConversationThread messages={[]} draftKey="race-nav-thread" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("");
});

it("keeps an edit made on the moved thread before navigating away and resolving", async () => {
  const send = deferred();
  const view = render(<ConversationThread messages={[]} draftKey="new" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "被发送的首条" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  moveConversationDraft("new", "race-edit-thread");
  view.rerender(<ConversationThread messages={[]} draftKey="race-edit-thread" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "迁移后改为这条" } });
  view.rerender(<ConversationThread messages={[]} draftKey="race-edit-b" onSubmit={() => send.promise} />);
  fireEvent.change(screen.getByLabelText("输入消息"), { target: { value: "B 的内容" } });
  await settle(send);
  expect(inputValue()).toBe("B 的内容");
  view.unmount();
  const other = render(<ConversationThread messages={[]} draftKey="race-edit-b" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("B 的内容");
  other.unmount();
  render(<ConversationThread messages={[]} draftKey="race-edit-thread" onSubmit={() => undefined} />);
  expect(inputValue()).toBe("迁移后改为这条");
});
