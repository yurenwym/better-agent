import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ConversationThread from "../components/ConversationThread";
import type { MessageRecord } from "../types";

afterEach(cleanup);

const streamingMessage: MessageRecord = {
  id: "message-streaming",
  run_id: "run-1",
  interaction_id: null,
  role: "assistant",
  content: "正在生成的回答",
  created_at: "2026-09-10T00:00:00Z",
  streaming: true,
};

const finishedMessage: MessageRecord = { ...streamingMessage, streaming: false };

describe("ConversationThread streaming feedback", () => {
  it("marks a streaming message with a cursor and aria-busy, and clears both when it completes", () => {
    const view = render(<ConversationThread messages={[streamingMessage]} busy={false} onSubmit={() => undefined} />);

    const row = document.querySelector(".message-row");
    expect(row?.getAttribute("aria-busy")).toBe("true");
    expect(document.querySelector(".stream-cursor")).toBeTruthy();

    view.rerender(<ConversationThread messages={[finishedMessage]} busy={false} onSubmit={() => undefined} />);

    expect(document.querySelector(".message-row")?.getAttribute("aria-busy")).toBeNull();
    expect(document.querySelector(".stream-cursor")).toBeNull();
  });

  it("keeps completed messages free of streaming affordances", () => {
    render(<ConversationThread messages={[{ ...streamingMessage, id: "message-done", streaming: false }]} busy={false} onSubmit={() => undefined} />);

    expect(document.querySelector(".stream-cursor")).toBeNull();
    expect(document.querySelector(".message-row")?.getAttribute("aria-busy")).toBeNull();
  });

  it("updates messages with the auto-scroll sentinel without crashing in jsdom", () => {
    const view = render(<ConversationThread messages={[finishedMessage]} busy={false} onSubmit={() => undefined} />);
    expect(document.querySelector(".conversation-scroll-sentinel")).toBeTruthy();

    view.rerender(<ConversationThread messages={[{ ...finishedMessage, id: "message-base", content: "追加的流式内容" }, streamingMessage]} busy={false} onSubmit={() => undefined} />);
    view.rerender(<ConversationThread messages={[{ ...finishedMessage, id: "message-final", content: "完成后的回答" }]} busy={false} onSubmit={() => undefined} />);

    expect(document.querySelector(".stream-cursor")).toBeNull();
  });
});
