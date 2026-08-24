import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import ConfirmDialog from "../components/ConfirmDialog";

it("keeps destructive confirmation inside the application", () => {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  render(
    <ConfirmDialog
      open
      title="删除会话？"
      description="删除后无法恢复。"
      onCancel={onCancel}
      onConfirm={onConfirm}
    />,
  );

  expect(screen.getByRole("dialog", { name: "删除会话？" })).toBeTruthy();
  const cancel = screen.getByRole("button", { name: "取消" });
  const confirm = screen.getByRole("button", { name: "确认删除" });
  expect(document.activeElement).toBe(cancel);
  confirm.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(document.activeElement).toBe(cancel);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(onCancel).toHaveBeenCalledOnce();
  fireEvent.click(confirm);
  expect(onConfirm).toHaveBeenCalledOnce();
});
