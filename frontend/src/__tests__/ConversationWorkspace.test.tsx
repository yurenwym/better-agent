import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationThread from "../components/ConversationThread";
import WorkspaceSidebar from "../components/WorkspaceSidebar";
import type { Bootstrap, MessageRecord, Run } from "../types";

afterEach(cleanup);

const run: Run = {
  id: "run_12345678",
  goal_id: "goal-1",
  session_id: "session-1",
  state: "CLARIFYING",
  resume_state: null,
  current_plan_version_id: null,
  current_step_id: null,
  version: 2,
  budget: { react_iterations_remaining: 5 },
  pending_approvals: [],
};

const bootstrap: Bootstrap = {
  csrf_token: "csrf",
  version: "1.0.0",
  api_key_env: "AGENT_MODEL_API_KEY",
  api_key_configured: true,
};

const messages: MessageRecord[] = [
  {
    id: "message-user",
    run_id: run.id,
    interaction_id: "interaction-1",
    role: "user",
    content: "请先确认目标范围",
    created_at: "2026-08-18T00:00:00Z",
  },
  {
    id: "message-assistant",
    run_id: run.id,
    interaction_id: "interaction-1",
    role: "assistant",
    content: '{"needs_clarification":true}',
    created_at: "2026-08-18T00:00:01Z",
  },
];

describe("conversation workspace", () => {
  it("keeps assistant messages on the left and user messages on the right", () => {
    render(<ConversationThread messages={messages} busy={false} onSubmit={() => undefined} />);

    const userRow = document.querySelector(".message-user");
    const assistantRow = document.querySelector(".message-assistant");

    expect(userRow?.children[0].classList.contains("message-body")).toBe(true);
    expect(userRow?.children[1].classList.contains("message-avatar")).toBe(true);
    expect(assistantRow?.children[0].classList.contains("message-avatar")).toBe(true);
    expect(assistantRow?.children[1].classList.contains("message-body")).toBe(true);
  });

  it("centers the empty conversation prompt and names the desired outcome", () => {
    render(<ConversationThread messages={[]} busy={false} onSubmit={() => undefined} />);

    expect(screen.getByRole("heading", { name: "你想实现什么？" })).toBeTruthy();
    expect(screen.queryByText("把下一步交给对话")).toBeNull();
    expect(screen.getByRole("heading", { name: "你想实现什么？" }).closest(".conversation-empty")).toBeTruthy();
    expect(document.querySelector(".conversation-thread-empty")).toBeTruthy();
  });

  it("shows the assistant result without exposing raw model JSON", () => {
    render(<ConversationThread messages={messages} busy={false} onSubmit={() => undefined} />);

    expect(screen.getByText("请先确认目标范围")).toBeTruthy();
    expect(screen.getByText("我还需要更多信息，才能继续推进这个目标。")).toBeTruthy();
    expect(screen.queryByText("查看模型原始结果")).toBeNull();
    expect(screen.queryByText('{"needs_clarification":true}')).toBeNull();
    expect(screen.getByRole("textbox", { name: "输入消息" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeTruthy();
  });

  it("sends on Enter, keeps Shift+Enter for a newline, and selects skills for this conversation", async () => {
    const onSubmit = vi.fn().mockResolvedValue(true);
    const onToggleSkill = vi.fn();
    render(
      <ConversationThread
        messages={[]}
        busy={false}
        onSubmit={onSubmit}
        skills={[
          { name: "goal-planning", title: "目标规划", description: "把目标拆成可审批步骤。", enabled: true },
          { name: "reflection", title: "执行复盘", description: "从结果中提炼可确认记忆。", enabled: true },
        ]}
        selectedSkills={[]}
        onToggleSkill={onToggleSkill}
      />,
    );

    const input = screen.getByRole("textbox", { name: "输入消息" });
    expect(input.getAttribute("placeholder")).toBe("输入信息，Enter发送，Shift+Enter换行");
    expect(screen.queryByText("继续推动目标")).toBeNull();

    fireEvent.change(input, { target: { value: "继续" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: false });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("继续"));

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));
    expect(screen.getByText("已安装的 Skill")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "目标规划" }));
    expect(onToggleSkill).toHaveBeenCalledWith("goal-planning");
  });

  it("shows an empty state when no skills are installed", () => {
    render(<ConversationThread messages={[]} onSubmit={() => undefined} skills={[]} selectedSkills={[]} onToggleSkill={() => undefined} />);

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));

    expect(screen.getByText("暂无已安装 Skill")).toBeTruthy();
  });

  it("renders ordinary assistant Markdown as readable content", () => {
    render(
      <ConversationThread
        messages={[{
          id: "message-markdown",
          run_id: run.id,
          interaction_id: "interaction-1",
          role: "assistant",
          content: "## 行程建议\n\n**第一天**：游览象鼻山。\n\n| 项目 | 建议 |\n| --- | --- |\n| 门票 | 提前预订 |",
          created_at: "2026-08-18T00:00:02Z",
        }]}
        busy={false}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByRole("heading", { name: "行程建议" })).toBeTruthy();
    expect(screen.getByText("第一天").tagName).toBe("STRONG");
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.queryByText("## 行程建议")).toBeNull();
  });

  it("offers explicit approval choices instead of requiring a text reply", () => {
    let approved = 0;
    let edited = 0;
    render(
      <ConversationThread
        messages={messages}
        busy={false}
        onSubmit={() => undefined}
        decision={{
          title: "计划已经准备好",
          description: "批准后开始执行；需要调整步骤可以先修改计划。",
          primaryLabel: "批准计划并继续",
          secondaryLabel: "修改计划",
          busy: false,
          onPrimary: () => { approved += 1; },
          onSecondary: () => { edited += 1; },
        }}
        composerDisabled
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "批准计划并继续" }));
    fireEvent.click(screen.getByRole("button", { name: "修改计划" }));
    expect(approved).toBe(1);
    expect(edited).toBe(1);
    expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(true);
    expect(document.querySelector(".composer-hint")).toBeNull();
  });

  it("keeps navigation and settings discoverable in the sidebar", () => {
    render(
      <WorkspaceSidebar
        activePage="chat"
        bootstrap={bootstrap}
        run={run}
        onNavigate={() => undefined}
        onNewConversation={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "新建会话" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "对话" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: "轨迹" })).toBeTruthy();
    expect(screen.getByText("设置")).toBeTruthy();
    expect(screen.getByText("模型已连接")).toBeTruthy();
  });
});
