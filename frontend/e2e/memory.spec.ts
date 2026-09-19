import { expect, test } from "@playwright/test";


test("memory can be created, archived, and restored", async ({ page }) => {
  await page.goto("/memory");

  await page.getByLabel("新增长期记忆").fill("回答时先给结论");
  await page.getByRole("button", { name: "记住" }).click();
  await expect(page.getByText("已记住“回答时先给结论”")).toBeVisible();

  const active = page.getByRole("article").filter({ hasText: "回答时先给结论" }).last();
  await active.getByRole("button", { name: "停用" }).click();
  await expect(page.getByText("长期记忆已停用", { exact: true })).toBeVisible();

  const archived = page.getByRole("article").filter({ hasText: "回答时先给结论" }).last();
  await archived.getByRole("button", { name: "恢复" }).click();
  await expect(page.getByText("长期记忆已恢复", { exact: true })).toBeVisible();
  await expect(page.getByRole("article").filter({ hasText: "回答时先给结论" }).last()).toBeVisible();
});
