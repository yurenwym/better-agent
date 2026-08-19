import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationThread from "../components/ConversationThread";

afterEach(cleanup);

describe("V2 conversation acceptance UI", () => {
  it("keeps ordinary answers free of generic execution controls", () => {
    render(
      <ConversationThread
        messages={[{
          id: "assistant-1",
          run_id: "thread-1",
          interaction_id: null,
          role: "assistant",
          content: "## 一次性回答\n\n这是可直接阅读的内容。",
          created_at: "2026-08-19T00:00:00Z",
        }]}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByRole("heading", { name: "一次性回答" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "继续执行" })).toBeNull();
    expect(screen.queryByRole("button", { name: "修改方案" })).toBeNull();
  });

  it("renders only task-specific choices for an execution preview", () => {
    const onPrimary = vi.fn();
    const onSecondary = vi.fn();
    render(
      <ConversationThread
        messages={[]}
        onSubmit={() => undefined}
        decision={{
          title: "这项请求需要确认",
          description: "确认后才会开始执行。",
          primaryLabel: "继续执行",
          secondaryLabel: "修改方案",
          onPrimary,
          onSecondary,
        }}
        composerDisabled
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "继续执行" }));
    fireEvent.click(screen.getByRole("button", { name: "修改方案" }));
    expect(onPrimary).toHaveBeenCalledOnce();
    expect(onSecondary).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "转为目标" })).toBeNull();
  });
});
