import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PlanVisualEditor from "../components/PlanVisualEditor";

afterEach(cleanup);

describe("PlanVisualEditor", () => {
  it("edits rendered content and emits Markdown", () => {
    const onChange = vi.fn();
    render(<PlanVisualEditor markdown={"# Trip\n\n- Train"} onChange={onChange} />);

    expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();
    const heading = screen.getByRole("heading", { name: "Trip" });
    heading.textContent = "Updated trip";
    fireEvent.input(heading);
    const item = screen.getByRole("textbox", { name: "列表项 1" });
    item.textContent = "Updated train";
    fireEvent.input(item);

    expect(onChange).toHaveBeenLastCalledWith("# Updated trip\n\n- Updated train");
  });

  it("deletes a block and can add an editable paragraph", () => {
    const onChange = vi.fn();
    render(<PlanVisualEditor markdown={"# Trip\n\nOverview"} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "删除第 1 个内容块" }));
    expect(screen.queryByRole("heading", { name: "Trip" })).toBeNull();
    expect(onChange).toHaveBeenLastCalledWith("Overview");

    fireEvent.click(screen.getByRole("button", { name: "添加段落" }));
    expect(screen.getByRole("textbox", { name: "段落 2" })).toBeTruthy();
  });

  it("renders tables and code blocks as editable visual structures", () => {
    render(<PlanVisualEditor markdown={"| Day | Place |\n| --- | --- |\n| 1 | 桂林 |\n\n```text\nhello\n```"} onChange={vi.fn()} />);

    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "桂林" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "代码块" })).toBeTruthy();
  });

  it("edits the plan title as part of the rendered editor", () => {
    const onTitleChange = vi.fn();
    render(<PlanVisualEditor title="Trip" onTitleChange={onTitleChange} markdown="Overview" onChange={vi.fn()} />);

    const title = screen.getByRole("textbox", { name: "计划名称" });
    fireEvent.change(title, { target: { value: "Updated trip" } });

    expect(onTitleChange).toHaveBeenCalledWith("Updated trip");
  });
});
