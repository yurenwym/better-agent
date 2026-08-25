import { expect, test } from "@playwright/test";

type GoalAction = { id: string; scheduled_date: string; status: string; version: number };
type GoalProgram = { id: string; version: number; actions: GoalAction[] };

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
  const bootstrap = await json(await request.get("/api/bootstrap"));
  const csrf = String(bootstrap.csrf_token);
  const headers = (key: string) => ({
    "X-CSRF-Token": csrf,
    "Content-Type": "application/json",
    "Idempotency-Key": key,
  });
  const startDate = dateInShanghai();
  const endDate = dateInShanghai(1);

  const plans = await json(await request.get("/api/plans"));
  const source = plans.plans[0] as { id: string; title: string };
  expect(source.title).toBe("两天行动计划");

  await page.goto(`/plans/${source.id}`);
  await expect(page.getByRole("heading", { name: "两天行动计划" })).toBeVisible();
  await expect(page.getByText("用两天完成一个可验证的小目标。")).toBeVisible();

  const program = await json(await request.post(`/api/plans/${source.id}/program-preview`, {
    headers: headers("preview"),
    data: { start_date: startDate, requested_end_date: endDate, timezone: "Asia/Shanghai", daily_minutes: 60 },
  })) as GoalProgram;
  const active = await json(await request.post(`/api/programs/${program.id}/activate`, {
    headers: headers("activate"), data: { expected_version: program.version },
  })) as GoalProgram;

  await page.goto("/today");
  await expect(page.getByRole("heading", { name: "两天行动计划" })).toBeVisible();
  await expect(page.getByText("FULL SCHEDULE")).toBeVisible();

  const first = active.actions.find((action) => action.scheduled_date === startDate);
  expect(first).toBeDefined();
  await json(await request.post(`/api/actions/${first!.id}/defer`, {
    headers: headers("defer"), data: { expected_version: first!.version, scheduled_date: endDate },
  }));

  await expect.poll(async () => {
    const today = await json(await request.get(`/api/today?date=${encodeURIComponent(startDate)}`));
    return today.programs[0]?.review?.status ?? "missing";
  }, { timeout: 10_000 }).toBe("COMPLETED");
  await page.reload();
  await expect(page.getByText("今天的复盘")).toBeVisible();
  await expect(page.getByText("执行调整预览")).toBeVisible();

  const today = await json(await request.get(`/api/today?date=${encodeURIComponent(startDate)}`));
  const proposal = today.programs[0].review.proposal as { id: string; version: number };
  const accepted = await json(await request.post(`/api/adjustments/${proposal.id}/accept`, {
    headers: headers("accept-adjustment"), data: { expected_version: proposal.version },
  })) as { program: GoalProgram };

  for (const action of accepted.program.actions.filter((item) => item.status === "SCHEDULED")) {
    await json(await request.post(`/api/actions/${action.id}/complete`, {
      headers: headers(`complete-${action.id}`), data: { expected_version: action.version },
    }));
  }
  const ready = await json(await request.get(`/api/programs/${accepted.program.id}`)) as GoalProgram;
  const completed = await json(await request.post(`/api/programs/${accepted.program.id}/complete`, {
    headers: headers("complete-program"), data: { expected_version: ready.version },
  }));
  expect(completed.status).toBe("COMPLETED");
  expect(completed.completion_episode_id).toMatch(/^episode_/);

  await page.goto("/memory");
  await expect(page.getByText("最近经历")).toBeVisible();
  await expect(page.getByText(/已完成目标/)).toBeVisible();
});
