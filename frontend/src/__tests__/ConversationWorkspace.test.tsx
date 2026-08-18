import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
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
  it("shows the assistant result without exposing raw model JSON", () => {
    render(<ConversationThread messages={messages} busy={false} onSubmit={() => undefined} />);

    expect(screen.getByText("请先确认目标范围")).toBeTruthy();
    expect(screen.getByText("我还需要更多信息，才能继续推进这个目标。")).toBeTruthy();
    expect(screen.queryByText("查看模型原始结果")).toBeNull();
    expect(screen.queryByText('{"needs_clarification":true}')).toBeNull();
    expect(screen.getByRole("textbox", { name: "继续推动目标" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeTruthy();
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
        composerHint="请使用上方按钮选择是否继续"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "批准计划并继续" }));
    fireEvent.click(screen.getByRole("button", { name: "修改计划" }));
    expect(approved).toBe(1);
    expect(edited).toBe(1);
    expect(screen.getByRole("textbox", { name: "继续推动目标" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("请使用上方按钮选择是否继续")).toBeTruthy();
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
