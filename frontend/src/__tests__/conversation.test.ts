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
  });

  it("turns clarification JSON into a readable answer without exposing raw output", () => {
    const view = presentMessage(message("assistant", '{"needs_clarification":true}'));

    expect(view.summary).toContain("还需要更多信息");
    expect(view).not.toHaveProperty("raw");
  });

  it("turns a plan response into a summary with steps", () => {
    const view = presentMessage(message("assistant", JSON.stringify({
      summary: "整理本周发布计划",
      steps: [
        { title: "收集输入", description: "梳理已有素材和限制" },
        { title: "生成草案", description: "产出一版可以直接使用的计划" },
      ],
    })));

    expect(view.summary).toBe("整理本周发布计划");
    expect(view.bullets).toEqual(["收集输入：梳理已有素材和限制", "生成草案：产出一版可以直接使用的计划"]);
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
  });

  it("shows the model output instead of the generic completed label", () => {
    const view = presentMessage(message("assistant", JSON.stringify({
      action: "complete_step",
      summary: "completed",
      output: "先按基础代谢估算热量，再给出一周菜单。",
    })));

    expect(view.summary).toBe("先按基础代谢估算热量，再给出一周菜单。");
  });
});
