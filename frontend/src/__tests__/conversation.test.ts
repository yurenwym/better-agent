import { describe, expect, it } from "vitest";
import { presentMessage } from "../conversation";
import type { MessageRecord } from "../types";

function message(role: MessageRecord["role"], content: string): MessageRecord {
  return {
    id: "message-1",
    run_id: "run-1",
    interaction_id: "interaction-1",
    role,
    content,
    created_at: "2026-08-18T00:00:00Z",
  };
}

describe("conversation message presentation", () => {
  it("keeps user messages as authored", () => {
    const view = presentMessage(message("user", "请先确认目标范围"));

    expect(view.summary).toBe("请先确认目标范围");
    expect(view.raw).toBeNull();
  });

  it("turns clarification JSON into a readable answer and keeps raw output", () => {
    const raw = '{"needs_clarification":true}';
    const view = presentMessage(message("assistant", raw));

    expect(view.summary).toContain("还需要更多信息");
    expect(view.raw).toBe(raw);
  });

  it("turns a plan response into a summary with steps", () => {
    const view = presentMessage(message("assistant", JSON.stringify({
      summary: "整理本周发布计划",
      steps: [{ title: "收集输入" }, { title: "生成草案" }],
    })));

    expect(view.summary).toBe("整理本周发布计划");
    expect(view.bullets).toEqual(["收集输入", "生成草案"]);
  });

  it("summarizes ReAct decisions and preserves non-JSON responses", () => {
    const decision = presentMessage(message("assistant", JSON.stringify({
      action: "tool_call",
      summary: "读取本地说明",
      tool_call: { name: "read_note" },
    })));
    const plain = presentMessage(message("assistant", "我已经完成第一步。"));

    expect(decision.summary).toContain("读取本地说明");
    expect(decision.detail).toContain("read_note");
    expect(plain.summary).toBe("我已经完成第一步。");
    expect(plain.raw).toBeNull();
  });
});
