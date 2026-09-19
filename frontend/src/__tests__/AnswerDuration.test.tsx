import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import AnswerDuration from "../components/AnswerDuration";
afterEach(cleanup);
it("formats completed durations and hides missing timing", () => {
  const { rerender } = render(<AnswerDuration milliseconds={12300} />);
  expect(screen.getByText("总耗时：12.3 秒")).toBeTruthy();
  rerender(<AnswerDuration milliseconds={75000} />);
  expect(screen.getByText("总耗时：1 分 15.0 秒")).toBeTruthy();
  rerender(<AnswerDuration />);
  expect(screen.queryByText(/总耗时/)).toBeNull();
});
