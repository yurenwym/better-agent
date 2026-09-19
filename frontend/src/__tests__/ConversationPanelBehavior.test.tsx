import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AskCard from "../components/AskCard";
import ConversationThread from "../components/ConversationThread";
import ResearchSourcesPanel from "../components/ResearchSourcesPanel";
import type { PendingAsk } from "../types";

const api = vi.hoisted(() => ({ getResearchSources: vi.fn() }));
vi.mock("../api", () => ({ ...api }));

afterEach(cleanup);

const ask: PendingAsk = {
  id: "ask-focus",
  turn_id: "turn-focus",
  questions: [{
    id: "goal",
    header: "目标",
    question: "你想优先实现什么？",
    options: [{ label: "计划", description: "先生成方案" }],
    multi_select: false,
    allow_free_text: false,
  }],
  status: "PENDING",
  continuation_turn_id: null,
  created_at: "2026-09-10T00:00:00Z",
  answered_at: null,
};

describe("conversation panel a11y behaviors", () => {
  it("focuses the first interactive element when the ask card mounts", async () => {
    render(<AskCard ask={ask} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "目标：计划" })));
    expect(screen.getByRole("region", { name: "等待你的回答" }).getAttribute("aria-label")).toBe("等待你的回答");
  });

  it("closes the skills panel on Escape and on outside mousedown, but keeps it open for inside clicks", () => {
    const view = render(
      <ConversationThread
        messages={[]}
        onSubmit={() => undefined}
        skills={[{ name: "reflection", title: "执行复盘", description: "从结果中提炼记忆。", enabled: true }]}
        selectedSkills={[]}
        onToggleSkill={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));
    expect(screen.getByRole("dialog", { name: "当前对话技能" })).toBeTruthy();

    fireEvent.mouseDown(screen.getByRole("dialog", { name: "当前对话技能" }));
    expect(screen.getByRole("dialog", { name: "当前对话技能" })).toBeTruthy();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("dialog", { name: "当前对话技能" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "当前对话技能" })).toBeNull();

    view.unmount();
  });

  it("ignores mousedown on the skill trigger so its own toggle stays in charge", () => {
    render(
      <ConversationThread
        messages={[]}
        onSubmit={() => undefined}
        skills={[{ name: "reflection", title: "执行复盘", description: "从结果中提炼记忆。", enabled: true }]}
        selectedSkills={[]}
        onToggleSkill={() => undefined}
      />,
    );

    const trigger = screen.getByRole("button", { name: /技能/ });
    fireEvent.click(trigger);
    fireEvent.mouseDown(trigger);
    expect(screen.getByRole("dialog", { name: "当前对话技能" })).toBeTruthy();

    fireEvent.click(trigger);
    expect(screen.queryByRole("dialog", { name: "当前对话技能" })).toBeNull();
  });

  it("shows a distinguishable failure state instead of hiding the sources panel", async () => {
    api.getResearchSources.mockRejectedValue(new Error("backend unavailable"));
    render(<ResearchSourcesPanel jobId="job-1" />);

    const status = await screen.findByRole("status");
    expect(status.textContent).toContain("来源暂时无法加载");
    expect(status.closest(".research-sources")).toBeTruthy();
  });
});
