import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const TRACE_TOKEN = "ghp_E2ETraceToken123";
const FOREIGN_THREAD_ID = "thread_trace_foreign";
const RESUME_THREAD_ID = "thread_trace_resume";
const DEFERRED_SOURCES = "这些来源尚未接入";

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

async function planByTitle(request: APIRequestContext, title: string) {
  const plans = await json(await request.get("/api/plans"));
  const plan = plans.plans.find((item: { title: string }) => item.title === title);
  expect(plan, title).toBeTruthy();
  return plan as { id: string; thread_id: string; title: string };
}

async function threadTrace(request: APIRequestContext, threadId: string) {
  return json(await request.get(`/api/threads/${threadId}/trace`));
}

function readyVersions(trace: { events?: Array<{ type?: string; payload?: { version?: number } }> }) {
  return (trace.events ?? [])
    .filter((event) => event.type === "plan.document_ready")
    .map((event) => event.payload?.version);
}

async function openTrajectory(page: Page, threadId: string) {
  await page.goto(`/threads/${threadId}?view=activity`);
  await expect(page.getByRole("region", { name: "统一轨迹", exact: true })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

// 领域页面：编辑预置计划、激活、完成、按钮收尾和展示脱敏。不证明对话 Agent 创建计划或工具执行。
test("page journey keeps the saved plan, close and review aligned with the trace", async ({ page, request }) => {
  test.setTimeout(120_000);
  const source = await planByTitle(request, "两天行动计划");
  const before = readyVersions(await threadTrace(request, source.thread_id));
  expect(before).toContain(1);
  expect(before).not.toContain(2);

  await page.goto(`/plans/${source.id}`);
  await page.getByRole("button", { name: "编辑计划" }).click();
  await page.getByLabel("计划名称").fill(`两天行动计划 ${TRACE_TOKEN}`);
  await page.getByRole("button", { name: "保存计划" }).click();
  await expect(page.getByText("计划 v2 · 已保存")).toBeVisible();
  const savedPlan = await json(await request.get(`/api/plans/${source.id}`));
  expect(savedPlan.title).toBe(`两天行动计划 ${TRACE_TOKEN}`);

  const saved = await threadTrace(request, source.thread_id);
  expect(readyVersions(saved)).toContain(2);
  expect(JSON.stringify(saved)).not.toContain(TRACE_TOKEN);
  expect(JSON.stringify(saved)).toContain("<redacted-token>");
  expect(saved.events.some((event: { type: string }) => ["review.completed", "day.closed", "program.activated"].includes(event.type))).toBe(false);

  const startDate = dateInShanghai();
  const endDate = dateInShanghai(1);
  await page.getByRole("button", { name: "开启执行管理" }).click();
  await page.getByLabel("执行开始日期").fill(startDate);
  await page.getByLabel("执行结束日期").fill(endDate);
  await page.getByLabel("执行时区").fill("Asia/Shanghai");
  await page.getByRole("button", { name: "生成预览" }).click();
  await page.getByRole("button", { name: "确认并激活" }).click();
  await expect(page.getByText("已启用 · v2").first()).toBeVisible();

  const program = (await json(await request.get("/api/programs"))).programs.find(
    (item: { source_plan_document_id: string }) => item.source_plan_document_id === source.id,
  );
  expect(program?.status).toBe("ACTIVE");

  await page.goto(`/today?program=${program.id}`);
  const action = page.getByRole("checkbox", { name: "标记完成：第 1 天行动" }).first();
  await action.click();
  await expect(page.getByRole("checkbox", { name: "撤销完成：第 1 天行动" }).first()).toBeChecked();
  const closeDay = page.getByRole("button", { name: "今天先到这里" });
  await expect(closeDay).toBeEnabled();
  await closeDay.click();

  let summary = "";
  await expect.poll(async () => {
    const response = await request.get(`/api/programs/${program.id}/reviews/${startDate}`);
    if (response.status() === 204) return "missing";
    const review = await response.json();
    summary = String(review.summary ?? "");
    return review.status;
  }, { timeout: 20_000 }).toBe("COMPLETED");
  expect(summary).toContain("今天完成 1 项");

  await page.reload();
  await page.getByRole("button", { name: "查看复盘" }).click();
  await expect(page.getByRole("heading", { name: "今天的复盘" })).toBeVisible();
  await expect(page.getByText(summary)).toBeVisible();

  await page.getByRole("link", { name: /黄金闭环测试/ }).click();
  await page.getByRole("button", { name: "运行详情" }).click();
  const tracePanel = page.getByRole("region", { name: "统一轨迹", exact: true });
  await expect(tracePanel).toBeVisible();
  await expect(tracePanel.getByText("已保存到计划 · v2", { exact: false }).first()).toBeVisible();
  await expect(tracePanel.getByText("工具：<redacted-token>").first()).toBeVisible();
  await expect(tracePanel.getByText("历史事件缺少可关联的上下文").first()).toBeVisible();
  await expect(page.getByText(DEFERRED_SOURCES)).toHaveCount(0);
  await expect(page.getByRole("region", { name: "统一轨迹统计" })).toContainText("部分");
  await expect(tracePanel).not.toContainText(TRACE_TOKEN);
  await expect(tracePanel).not.toContainText("今天完成 1 项");

  const refreshed = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/threads/${source.thread_id}/trace` && !url.searchParams.has("cursor");
  });
  await page.getByRole("button", { name: "重新读取" }).click();
  expect((await refreshed).ok()).toBeTruthy();
  await expect(tracePanel.getByText("已保存到计划 · v2", { exact: false }).first()).toBeVisible();
});

// 无模型服务上的失败输入保留。不因确定性模型而删除。
test("failed send keeps the draft and shows the failure", async ({ page }) => {
  const draft = "请保留这段失败输入，不要清空。";
  await page.goto("/");
  const input = page.locator("#conversation-input");
  await expect(input).toBeEnabled();
  await input.fill(draft);
  await input.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "503" })).toBeVisible();
  await expect(input).toHaveValue(draft);
});

test("cancel from the page leaves the other plan trace readable", async ({ page, request }) => {
  test.setTimeout(90_000);
  const source = await planByTitle(request, "Calendar acceptance 1440");
  const startDate = dateInShanghai();
  await page.goto(`/plans/${source.id}`);
  await page.getByRole("button", { name: "开启执行管理" }).click();
  await page.getByLabel("执行开始日期").fill(startDate);
  await page.getByLabel("执行结束日期").fill(dateInShanghai(1));
  await page.getByLabel("执行时区").fill("Asia/Shanghai");
  await page.getByRole("button", { name: "生成预览" }).click();
  await page.getByRole("button", { name: "确认并激活" }).click();
  await expect(page.getByText("已启用 · v2").first()).toBeVisible();
  const program = (await json(await request.get("/api/programs"))).programs.find(
    (item: { source_plan_document_id: string }) => item.source_plan_document_id === source.id,
  );

  await page.goto(`/today?program=${program.id}`);
  await page.getByRole("button", { name: "取消目标" }).click();
  await page.getByRole("dialog", { name: "取消这个目标？" }).getByRole("button", { name: "确认取消" }).click();
  await expect(page.getByText("目标已取消").first()).toBeVisible();
  const cancelled = await json(await request.get(`/api/programs/${program.id}`));
  expect(cancelled.status).toBe("CANCELLED");

  const trace = await threadTrace(request, source.thread_id);
  expect(readyVersions(trace)).toContain(1);
  expect(trace.events.some((event: { type: string }) => event.type === "program.cancelled")).toBe(false);
  await openTrajectory(page, source.thread_id);
  await expect(page.getByRole("region", { name: "统一轨迹", exact: true }).getByText("计划已保存").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "统一轨迹", exact: true }).getByText("历史事件缺少可关联的上下文").first()).toBeVisible();
});

test("another user's thread is refused without its marker", async ({ page }) => {
  await openTrajectory(page, FOREIGN_THREAD_ID);
  await expect(page.getByRole("alert").filter({ hasText: "404" })).toBeVisible();
  await expect(page.getByText("foreign-only-marker")).toHaveCount(0);
});

// 空页游标续读。使用其他 owner 的记录，不是长会话归档。
test("empty pages stay resumable across refresh and thread switch", async ({ page, request }) => {
  test.setTimeout(120_000);
  const calendar = await planByTitle(request, "Calendar acceptance 375");
  await openTrajectory(page, RESUME_THREAD_ID);
  await expect(page.getByRole("button", { name: "继续读取" })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("等待第一条轨迹事件")).toBeVisible();
  await expect(page.getByText("这次扫描尚未读完，后面可能还有记录")).toBeVisible();
  await expect(page.getByText(DEFERRED_SOURCES)).toHaveCount(0);
  await expect(page.getByText("已保存到计划 · v9")).toHaveCount(0);

  await page.getByRole("button", { name: "继续读取" }).click();
  await expect(page.getByText("已保存到计划 · v9", { exact: false })).toBeVisible();
  await expect(page.getByText("历史事件缺少可关联的上下文").first()).toBeVisible();

  const refreshed = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/threads/${RESUME_THREAD_ID}/trace` && !url.searchParams.has("cursor");
  });
  await page.getByRole("button", { name: "重新读取" }).click();
  expect((await refreshed).ok()).toBeTruthy();
  await expect(page.getByText("已保存到计划 · v9")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "继续读取" })).toBeVisible({ timeout: 60_000 });

  await page.getByRole("button", { name: "继续读取" }).click();
  await page.goto(`/threads/${calendar.thread_id}?view=activity`);
  const tracePanel = page.getByRole("region", { name: "统一轨迹", exact: true });
  await expect(tracePanel.getByText("计划已保存").first()).toBeVisible();
  await expect(page.getByText("已保存到计划 · v9")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "继续读取" })).toHaveCount(0);
  await expect(tracePanel.getByText("历史事件缺少可关联的上下文").first()).toBeVisible();
});

const scriptedOrigin = `http://127.0.0.1:${process.env.BETTER_AGENT_E2E_SCRIPTED_PORT ?? "61130"}`;

async function sendMessage(page: Page, text: string) {
  const input = page.locator("#conversation-input");
  await expect(input).toBeEnabled({ timeout: 90_000 });
  await input.fill(text);
  const send = page.getByRole("button", { name: "发送" });
  await expect(send).toBeEnabled({ timeout: 90_000 });
  await send.click();
}

async function sendUntilArchive(page: Page, text: string): Promise<"sent" | "waiting"> {
  const input = page.locator("#conversation-input");
  const send = page.getByRole("button", { name: "发送" });
  const organizing = page.getByText("正在整理历史");
  if (await page.getByRole("button", { name: "停止生成" }).isVisible() && !await organizing.isVisible()) {
    await page.reload();
  }
  await expect(input).toBeEnabled({ timeout: 90_000 });
  await input.fill(text);
  let outcome = "busy";
  await expect.poll(async () => {
    if (await organizing.isVisible()) {
      outcome = "waiting";
      return outcome;
    }
    if (await send.isEnabled()) {
      outcome = "ready";
      return outcome;
    }
    return "busy";
  }, { timeout: 90_000 }).not.toBe("busy");
  if (outcome === "waiting") return "waiting";
  await send.click();
  return "sent";
}

async function approve(page: Page, name: string) {
  const card = page.getByRole("article", { name });
  await expect(card).toBeVisible({ timeout: 90_000 });
  await card.getByRole("button", { name: "批准" }).click();
}

test.describe("scripted model boundary", () => {
  test.use({ baseURL: scriptedOrigin });

  test("page input creates a plan and the agent reads the review", async ({ page, request }) => {
    test.setTimeout(180_000);
    const startDate = dateInShanghai();
    await page.goto("/");
    await sendMessage(page, "请创建并保存一份标题为页面创建的两天计划的计划。");
    await expect(page.getByText("用两天完成一个可验证的小目标。")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator("#conversation-input")).toBeEnabled({ timeout: 90_000 });

    const plan = await planByTitle(request, "页面创建的两天计划");
    const versions = await json(await request.get(`/api/plans/${plan.id}/versions`));
    expect(versions.versions.some((item: { version: number; actor: string; source_turn_id: string | null }) =>
      item.version === 1 && item.actor === "model" && item.source_turn_id)).toBeTruthy();

    await sendMessage(page, "请根据刚保存的计划生成执行预览并请求激活。从今天开始，到明天结束，时区 Asia/Shanghai，每天 60 分钟。");
    await approve(page, "开启执行管理 · 需要确认");
    await expect(page.getByText("执行已激活。")).toBeVisible({ timeout: 90_000 });
    const program = (await json(await request.get("/api/programs"))).programs.find(
      (item: { source_plan_document_id: string }) => item.source_plan_document_id === plan.id,
    );
    expect(program?.status).toBe("ACTIVE");

    await sendMessage(page, "请把今天的第 1 天行动标记为完成。");
    await approve(page, "完成行动 · 需要确认");
    await expect(page.getByText("今天的行动已标记完成。")).toBeVisible({ timeout: 90_000 });
    const today = await json(await request.get(`/api/today?date=${startDate}`));
    const group = today.programs.find((item: { program: { id: string } }) => item.program.id === program.id);
    expect(group.completed.length).toBeGreaterThan(0);
    expect(group.completed[0].status).toBe("COMPLETED");

    await sendMessage(page, "请结束今天并保存复盘。");
    await approve(page, "结束当天并复盘 · 需要确认");
    await expect(page.getByText("已提交今天的收尾。")).toBeVisible({ timeout: 90_000 });
    let summary = "";
    await expect.poll(async () => {
      const response = await request.get(`/api/programs/${program.id}/reviews/${startDate}`);
      if (response.status() === 204) return "missing";
      const review = await response.json();
      summary = String(review.summary ?? "");
      return review.status;
    }, { timeout: 20_000 }).toBe("COMPLETED");
    expect(summary).toContain("今天完成 1 项");

    await sendMessage(page, "请读取今天的复盘结果。");
    await expect(page.getByText(summary)).toBeVisible({ timeout: 90_000 });

    const trace = await threadTrace(request, plan.thread_id);
    const finished = (trace.events ?? []).filter((event: { type: string }) => event.type === "tool.execution.finished");
    expect(finished.length).toBeGreaterThan(0);
    const linked = finished.find((event: { context?: { context?: { trace_id?: string } }; payload?: { call_id?: string } }) =>
      event.context?.context?.trace_id && event.payload?.call_id);
    expect(linked, "tool execution event with trace identity").toBeTruthy();
    const byTrace = await json(await request.get(`/api/traces/${linked.context.context.trace_id}`));
    expect(byTrace.events.some((event: { event_id: string }) => event.event_id === linked.event_id)).toBeTruthy();
    expect(readyVersions(trace)).toContain(1);
    expect(trace.events.some((event: { type: string }) => ["review.completed", "day.closed", "program.activated"].includes(event.type))).toBe(false);

    await openTrajectory(page, plan.thread_id);
    const tracePanel = page.getByRole("region", { name: "统一轨迹", exact: true });
    await expect(tracePanel.getByText("已保存到计划 · v1", { exact: false }).first()).toBeVisible();
    await expect(tracePanel.getByText("工具执行完成").first()).toBeVisible();
    await expect(page.getByText(DEFERRED_SOURCES)).toHaveCount(0);
    await expect(page.getByRole("region", { name: "统一轨迹统计" })).toContainText("完整");
  });

  test("a long isolated history waits for archive and then continues", async ({ page, request }) => {
    test.setTimeout(180_000);
    await page.goto("/");
    expect(await sendUntilArchive(page, `归档填充：${"A".repeat(100_000)}`)).toBe("sent");
    await expect(page.getByText("已记下。")).toBeVisible({ timeout: 90_000 });

    let held = "归档填充：";
    const organizing = page.getByText("正在整理历史");
    for (let index = 1; index <= 5 && !await organizing.isVisible(); index += 1) {
      const text = `归档填充：短记录${index}`;
      const before = await page.getByText("已记下。").count();
      const outcome = await sendUntilArchive(page, text);
      held = text;
      if (outcome === "waiting") break;
      await expect.poll(async () => {
        if (await organizing.isVisible()) return "waiting";
        return String(await page.getByText("已记下。").count());
      }, { timeout: 90_000 }).not.toBe(String(before));
    }
    if (!await organizing.isVisible()) {
      const trigger = `归档触发句E2E${"B".repeat(20_000)}`;
      const outcome = await sendUntilArchive(page, trigger);
      held = "归档触发句E2E";
      if (outcome === "sent") await expect(organizing).toBeVisible({ timeout: 90_000 });
    }

    await expect(organizing).toBeVisible();
    const messages = page.getByLabel("消息列表");
    const draft = await page.locator("#conversation-input").inputValue();
    expect((await messages.getByText(held).count()) > 0 || draft.includes(held)).toBeTruthy();
    await expect(page).toHaveURL(/\/threads\/[^/?#]+/);
    const threadId = new URL(page.url()).pathname.split("/")[2];
    const waiting = await json(await request.get(`/api/threads/${threadId}/archive`));
    expect(waiting.archived_through_seq).toBe(0);
    expect(waiting.jobs.some((job: { status: string }) => job.status === "COMPLETED")).toBeFalsy();
    const notedBefore = await page.getByText("已记下。").count();
    expect((await request.post("/api/e2e/archive-release")).ok()).toBeTruthy();
    await expect.poll(async () => {
      const response = await request.get(`/api/threads/${threadId}/archive`);
      if (!response.ok()) return "missing";
      const body = await response.json();
      return body.jobs.some((job: { status: string }) => job.status === "COMPLETED") ? "done" : "waiting";
    }, { timeout: 90_000 }).toBe("done");

    const unsent = (await page.locator("#conversation-input").inputValue()).includes(held)
      && (await messages.getByText(held).count()) === 0;
    if (unsent) await sendMessage(page, held);
    else await sendMessage(page, "归档填充：续跑");
    await expect.poll(async () => page.getByText("已记下。").count(), { timeout: 90_000 }).toBeGreaterThan(notedBefore);
    expect(await messages.getByText(held).count()).toBeGreaterThan(0);

    const archive = await json(await request.get(`/api/threads/${threadId}/archive`));
    expect(archive.archived_through_seq).toBeGreaterThan(0);
    expect(archive.jobs.some((job: { status: string }) => job.status === "COMPLETED")).toBeTruthy();

    await openTrajectory(page, threadId);
    await expect(page.getByText(DEFERRED_SOURCES)).toHaveCount(0);
    await expect(page.getByRole("region", { name: "统一轨迹统计" })).toContainText("完整");
  });
});
