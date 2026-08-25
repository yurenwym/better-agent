import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import MarkdownMessage from "../components/MarkdownMessage";

afterEach(cleanup);

describe("MarkdownMessage", () => {
  it("renders readable semantic Markdown instead of source markers", () => {
    render(
      <MarkdownMessage
        content={[
          "## 2天桂林游",
          "",
          "### 费用预算",
          "",
          "**经济型**：人均约 ¥700–900",
          "",
          "| 项目 | 预算 |",
          "| --- | --- |",
          "| 门票 | ¥180–250 |",
          "",
          "- 提前订票",
          "- 带好雨衣",
          "",
          "> 以官方平台实时信息为准。",
        ].join("\n")}
      />,
    );

    expect(screen.getByRole("heading", { name: "2天桂林游" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "费用预算" })).toBeTruthy();
    expect(screen.getByText("经济型").tagName).toBe("STRONG");
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "项目" })).toBeTruthy();
    expect(screen.getByRole("list")).toBeTruthy();
    expect(screen.getByRole("blockquote")).toBeTruthy();
    expect(screen.queryByText("## 2天桂林游")).toBeNull();
    expect(screen.queryByText("**经济型**：人均约 ¥700–900")).toBeNull();
    expect(screen.queryByText("| 项目 | 预算 |")).toBeNull();
  });

  it("renders fenced code and keeps raw HTML as text", () => {
    const fence = String.fromCharCode(96).repeat(3);
    render(
      <MarkdownMessage
        content={fence + "json\n{\"state\":\"COMPLETED\"}\n" + fence + "\n\n<script>alert(\"x\")</script>"}
      />,
    );

    expect(screen.getByRole("code").textContent).toBe('{"state":"COMPLETED"}');
    expect(screen.queryByRole("script")).toBeNull();
    expect(screen.getByText('<script>alert("x")</script>')).toBeTruthy();
  });

  it("renders model br tags as line breaks without enabling raw HTML", () => {
    render(<MarkdownMessage content={'第一行<br>第二行<br/>第三行<br />第四行<script>alert("x")</script>'} />);

    expect(screen.getByText(/第一行/).querySelectorAll("br")).toHaveLength(3);
    expect(screen.queryByText(/<br\s*\/?\s*>/)).toBeNull();
    expect(screen.getByText(/<script>alert\("x"\)<\/script>/)).toBeTruthy();
    expect(screen.queryByRole("script")).toBeNull();
  });

  it("degrades incomplete blocks to visible code without throwing", () => {
    const fence = String.fromCharCode(96).repeat(3);
    render(<MarkdownMessage content={fence + "\npartial output"} />);

    expect(screen.getByRole("code").textContent).toBe("partial output");
  });
});
