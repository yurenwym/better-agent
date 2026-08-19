import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AskCard from "../components/AskCard";
import type { PendingAsk } from "../types";

const ask: PendingAsk = {
  id: "ask-1",
  turn_id: "turn-1",
  questions: [{
    id: "training_level",
    header: "训练水平",
    question: "你目前的训练水平是什么？",
    options: [
      { label: "新手", description: "刚开始训练" },
      { label: "有基础", description: "已有训练习惯" },
    ],
    multi_select: false,
    allow_free_text: true,
  }],
  status: "PENDING",
  continuation_turn_id: null,
  created_at: "2026-08-19T00:00:00Z",
  answered_at: null,
};

afterEach(cleanup);

describe("AskCard", () => {
  it("requires an answer, supports a single option, and submits normalized data", () => {
    const onSubmit = vi.fn();
    render(<AskCard ask={ask} onSubmit={onSubmit} onCancel={() => undefined} />);

    const submit = screen.getByRole("button", { name: "提交回答" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "训练水平：新手" }));
    expect(submit.hasAttribute("disabled")).toBe(false);
    fireEvent.click(submit);

    expect(onSubmit).toHaveBeenCalledWith([
      { question_id: "training_level", selected_options: ["新手"], free_text: "" },
    ]);
  });

  it("supports multi-select and free-text answers", () => {
    const onSubmit = vi.fn();
    const multiAsk: PendingAsk = {
      ...ask,
      questions: [{
        ...ask.questions[0],
        multi_select: true,
        options: [
          { label: "力量", description: "提升力量" },
          { label: "耐力", description: "提升耐力" },
        ],
      }],
    };
    render(<AskCard ask={multiAsk} onSubmit={onSubmit} onCancel={() => undefined} />);

    fireEvent.click(screen.getByRole("button", { name: "训练水平：力量" }));
    fireEvent.click(screen.getByRole("button", { name: "训练水平：耐力" }));
    fireEvent.change(screen.getByRole("textbox", { name: "训练水平：补充信息" }), { target: { value: "每周三次" } });
    fireEvent.click(screen.getByRole("button", { name: "提交回答" }));

    expect(onSubmit).toHaveBeenCalledWith([
      { question_id: "training_level", selected_options: ["力量", "耐力"], free_text: "每周三次" },
    ]);
  });

  it("calls stop without submitting an answer", () => {
    const onCancel = vi.fn();
    render(<AskCard ask={ask} onSubmit={() => undefined} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "停止询问" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
