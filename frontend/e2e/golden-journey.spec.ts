import { expect, test } from "@playwright/test";

type GoalAction = { scheduled_date: string; title: string };
type GoalProgram = { actions: GoalAction[] };

async function json(response: { ok(): boolean; status(): number; json(): Promise<unknown> }) {
  expect(response.ok(), `request failed with ${response.status()}`).toBeTruthy();
  return response.json() as Promise<Record<string, any>>;
}

function dateInShanghai(offsetDays = 0): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const date = new Date(`${values.year}-${values.month}-${values.day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

test("golden journey completes the durable goal loop", async ({ page, request }) => {
  const startDate = dateInShanghai();
  const endDate = dateInShanghai(1);

  const plans = await json(await request.get("/api/plans"));
  const source = plans.plans[0] as { id: string; title: string };
  expect(source.title).toBe("两天行动计划");

  await page.goto(`/plans/${source.id}`);
  await expect(page.getByRole("heading", { name: "两天行动计划" }).last()).toBeVisible();
  await expect(page.getByText("用两天完成一个可验证的小目标。")).toBeVisible();

  await page.getByRole("button", { name: "开始执行" }).click();
  await page.getByLabel("执行开始日期").fill(startDate);
  await page.getByLabel("执行结束日期").fill(endDate);
  await page.getByRole("button", { name: "生成预览" }).click();
  await page.getByRole("button", { name: "确认并激活" }).click();
  await expect(page.getByText("ACTIVE · v2")).toBeVisible();

  const active = (await json(await request.get("/api/programs"))).programs[0] as GoalProgram;

  await page.goto("/today");
  await expect(page.getByRole("heading", { name: "两天行动计划" })).toBeVisible();
  await expect(page.getByText("FULL SCHEDULE")).toBeVisible();

  const first = active.actions.find((action) => action.scheduled_date === startDate);
  expect(first).toBeDefined();
  const firstCard = page.getByRole("article").filter({ has: page.getByRole("heading", { name: first!.title }) });
  await firstCard.getByLabel(`${first!.title} 延期日期`).fill(endDate);
  await firstCard.getByRole("button", { name: "确认延期" }).click();

  await expect.poll(async () => {
    const today = await json(await request.get(`/api/today?date=${encodeURIComponent(startDate)}`));
    return today.programs[0]?.review?.status ?? "missing";
  }, { timeout: 10_000 }).toBe("COMPLETED");
  await page.reload();
  await expect(page.getByRole("heading", { name: "今天的复盘" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "执行调整预览" })).toBeVisible();
  await page.getByRole("button", { name: "接受调整" }).click();
  await expect(page.getByRole("button", { name: "接受调整" })).toBeHidden();

  const schedule = page.getByRole("region", { name: "完整执行日程" });
  for (const details of await schedule.locator("details").all()) {
    await details.evaluate((element: HTMLDetailsElement) => { element.open = true; });
  }
  while (await page.getByRole("checkbox", { name: /^标记完成：/ }).count()) {
    const checkbox = page.getByRole("checkbox", { name: /^标记完成：/ }).first();
    const label = await checkbox.getAttribute("aria-label");
    await checkbox.click();
    await expect(page.getByRole("checkbox", { name: label!.replace("标记完成：", "已完成：") })).toBeChecked();
  }
  await page.getByRole("button", { name: "完成目标" }).click();
  await page.getByRole("dialog", { name: "确认完成目标？" }).getByRole("button", { name: "确认完成" }).click();
  await expect(page.getByText("目标已完成，执行总结已保存到记忆")).toBeVisible();

  await page.goto("/memory");
  await expect(page.getByRole("heading", { name: "最近经历" })).toBeVisible();
  await expect(page.getByText(/已完成目标/)).toBeVisible();

  await page.goto("/growth");
  await expect(page.getByRole("heading", { name: "可信改进闭环" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "提示词候选" })).toBeVisible();
  await expect(page.getByText("3 条证据")).toBeVisible();
  await page.getByRole("button", { name: "批准候选" }).click();
  await expect(page.getByText("已批准")).toBeVisible();
  await page.getByRole("button", { name: "开始 Canary" }).click();
  await expect(page.getByText("灰度中")).toBeVisible();
  await page.getByRole("button", { name: "回滚 Canary" }).click();
  await expect(page.getByText("已回滚", { exact: true })).toBeVisible();
});
