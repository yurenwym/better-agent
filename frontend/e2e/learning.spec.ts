import { expect, test } from "@playwright/test";

test("learning policy can be saved and remains usable on mobile", async ({ page }) => {
  await page.route("https://fonts.googleapis.com/**", route => route.abort());
  await page.route("https://fonts.gstatic.com/**", route => route.abort());
  await page.goto("/growth?view=agent", { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: "自主学习政策与记录" });
  await expect(panel.getByRole("heading", { name: "设定一次，按证据学习" })).toBeVisible();
  await panel.getByLabel("启用自主学习").check();
  await panel.getByRole("button", { name: "保存学习政策" }).click();
  await expect(panel.getByRole("status")).toContainText("学习政策已保存");
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(panel.getByLabel("启用自主学习")).toBeChecked();
  await page.screenshot({ path: "test-results/learning-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(panel.getByRole("button", { name: "保存学习政策" })).toBeVisible();
  await page.screenshot({ path: "test-results/learning-mobile.png", fullPage: true });
});
