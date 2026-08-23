import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import HumanBubbles from "../components/HumanBubbles";
import { splitHumanBubbles } from "../humanBubbles";

describe("human bubbles", () => {
  it("merges every segment after the third into the third bubble", () => {
    expect(splitHumanBubbles("一[[next]]二[[next]]三[[next]]四")).toEqual(["一", "二", "三\n\n四"]);
  });
  it("shows historical bubbles immediately", () => {
    render(<HumanBubbles messageId="m" content="你好[[next]]继续" origin="history" complete />);
    expect(screen.getByText("你好")).toBeTruthy();
    expect(screen.getByText("继续")).toBeTruthy();
  });
});
