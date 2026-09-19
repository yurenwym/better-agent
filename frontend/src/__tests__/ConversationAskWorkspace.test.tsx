import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ConversationThread from "../components/ConversationThread";
import type { PendingAsk } from "../types";

const ask: PendingAsk = {
  id: "ask-1",
  turn_id: "turn-1",
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
  created_at: "2026-08-19T00:00:00Z",
  answered_at: null,
};

afterEach(cleanup);

describe("conversation ask workspace", () => {
  it("shows AskCard and disables the ordinary composer while waiting", () => {
    render(
      <ConversationThread
        messages={[]}
        pendingAsk={ask}
        onAskAnswer={() => undefined}
        onAskCancel={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByRole("region", { name: "等待你的回答" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "等待你的回答" }).closest(".conversation-thread")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "你想实现什么？" })).toBeNull();
    expect(screen.getByText("输入框暂时锁定；如果想开始新的目标，请先停止询问。", { exact: false })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "目标：计划" }));
    expect(screen.getByRole("button", { name: "提交回答" }).hasAttribute("disabled")).toBe(false);
  });
  it("keeps one stop action and does not show response loading during an ask",()=>{
    render(<ConversationThread messages={[]} pendingAsk={ask} busy onCancel={()=>undefined} cancelLabel="停止询问" onAskAnswer={()=>undefined} onAskCancel={()=>undefined} onSubmit={()=>undefined}/>);
    expect(screen.getAllByRole("button",{name:"停止询问"})).toHaveLength(1);
    expect(screen.queryByText("正在等待模型返回下一步结果…")).toBeNull();
  });
});
