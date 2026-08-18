import { cleanup, render, screen } from "@testing-library/react";
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
  it("shows the assistant result, raw disclosure, and a follow-up composer", () => {
    render(<ConversationThread messages={messages} busy={false} onSubmit={() => undefined} />);

    expect(screen.getByText("请先确认目标范围")).toBeTruthy();
    expect(screen.getByText("我还需要更多信息，才能继续推进这个目标。")).toBeTruthy();
    expect(screen.getByText("查看模型原始结果").closest("details")?.open).toBe(false);
    expect(screen.getByRole("textbox", { name: "继续推动目标" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeTruthy();
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
