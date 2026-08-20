import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationThread from "../components/ConversationThread";

afterEach(cleanup);

describe("plan document chat reference", () => {
  it("shows the saved-version card and opens the stable plan id", () => {
    const openPlan = vi.fn();
    render(
      <ConversationThread
        messages={[{
          id: "assistant-plan",
          run_id: "thread-1",
          interaction_id: null,
          role: "assistant",
          content: "# Saved plan",
          created_at: "2026-08-19T00:00:00Z",
        }]}
        planReference={{ planDocumentId: "plan-1", version: 2, messageId: "assistant-plan" }}
        onOpenPlan={openPlan}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByText("已保存到计划 · v2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看 / 编辑计划" }));
    expect(openPlan).toHaveBeenCalledWith("plan-1");
  });
});

