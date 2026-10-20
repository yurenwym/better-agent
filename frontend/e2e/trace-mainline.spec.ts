import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

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

async function readAllTrace(request: APIRequestContext, tracePath: string) {
  const events: Array<Record<string, any>> = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let page = 0; page < 20; page += 1) {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const body = await json(await request.get(`${tracePath}${query}`));
    for (const event of body.events ?? []) {
      if (seen.has(event.event_id)) continue;
      seen.add(event.event_id);
      events.push(event);
    }
    const next = (body.next_cursor ?? null) as string | null;
    if (!next) return events;
    if (next === cursor) throw new Error(`repeated trace cursor: ${tracePath}`);
    cursor = next;
  }
  throw new Error(`trace pagination exceeded 20 pages: ${tracePath}`);
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

test("R02 worker lifecycle events stay linked across API, page and permissions", async ({ page, request, browser }) => {
  const dataRoot = process.env.BETTER_AGENT_E2E_DATA_ROOT;
  expect(dataRoot, "BETTER_AGENT_E2E_DATA_ROOT").toBeTruthy();
  const seed = JSON.parse(fs.readFileSync(path.join(dataRoot as string, "e2e-seed.json"), "utf8"));
  const { archive, embedding, learning } = seed.worker_scenarios;
  const sent = seed.worker_scenarios.notification.sent;
  const unknown = seed.worker_scenarios.notification.unknown;
  const threadId: string = archive.thread_id;

  // 2. manifest DB evidence
  expect(archive.coverage_before).toBe(0);
  expect(archive.coverage_after).toBeGreaterThan(0);
  expect(archive.status).toBe("COMPLETED");
  expect(String(archive.summary).length).toBeGreaterThan(0);
  expect((archive.source_message_ids ?? []).length).toBeGreaterThan(0);
  expect(embedding.status).toBe("COMPLETED");
  expect(embedding.provider_calls).toBe(1);
  expect(embedding.revision).toBeTruthy();
  expect(embedding.profile).toBeTruthy();
  expect(learning.status).toBe("UNKNOWN");
  expect(learning.dispatched).toBe(true);
  expect(learning.root_count).toBe(1);
  expect(learning.old_dispatch_rejected).toBe(true);
  expect(learning.effect).toBe("unknown");
  expect(learning.retryable).toBe(false);
  expect(sent.status).toBe("SENT");
  expect(unknown.status).toBe("UNKNOWN");
  expect(sent.provider_calls).toBe(1);
  expect(unknown.provider_calls).toBe(1);
  expect(sent.replay_same_delivery).toBe(true);
  expect(unknown.replay_same_delivery).toBe(true);
  expect(sent.delivery_id).not.toBe(unknown.delivery_id);

  const archiveStatus = await json(await request.get(`/api/threads/${threadId}/archive`));
  expect(archiveStatus.archived_through_seq).toBe(archive.coverage_after);
  expect((archiveStatus.jobs ?? []).find((job: { id: string }) => job.id === archive.job_id)?.status).toBe("COMPLETED");

  // 1. exact event_id association on the shared worker thread
  const events = await readAllTrace(request, `/api/threads/${threadId}/trace`);
  const byId = new Map<string, Record<string, any>>(events.map((event) => [event.event_id, event]));
  const cases: Array<{ event_id: string; type: string; source: string; turn_id: string; payload: Record<string, any> }> = [
    { event_id: archive.claimed_event_id, type: "archive.claimed", source: "archive", turn_id: archive.turn_id, payload: { job_id: archive.job_id } },
    { event_id: archive.committed_event_id, type: "archive.committed", source: "archive", turn_id: archive.turn_id, payload: { job_id: archive.job_id } },
    { event_id: embedding.claimed_event_id, type: "embedding.claimed", source: "embedding", turn_id: archive.turn_id, payload: { job_id: embedding.job_id, revision_id: embedding.revision, profile_id: embedding.profile } },
    { event_id: embedding.committed_event_id, type: "embedding.committed", source: "embedding", turn_id: archive.turn_id, payload: { job_id: embedding.job_id, revision_id: embedding.revision, profile_id: embedding.profile } },
    { event_id: learning.claimed_event_id, type: "learning.claimed", source: "learning", turn_id: learning.turn_id, payload: { job_id: learning.job_id } },
    { event_id: learning.recovered_event_id, type: "learning.recovered", source: "learning", turn_id: learning.turn_id, payload: { job_id: learning.job_id } },
    { event_id: sent.claimed_event_id, type: "notification.claimed", source: "notification", turn_id: sent.turn_id, payload: { delivery_id: sent.delivery_id, job_id: sent.report_job_id } },
    { event_id: sent.result_event_id, type: "notification.sent", source: "notification", turn_id: sent.turn_id, payload: { delivery_id: sent.delivery_id, job_id: sent.report_job_id } },
    { event_id: unknown.claimed_event_id, type: "notification.claimed", source: "notification", turn_id: unknown.turn_id, payload: { delivery_id: unknown.delivery_id, job_id: unknown.report_job_id } },
    { event_id: unknown.result_event_id, type: "notification.unknown", source: "notification", turn_id: unknown.turn_id, payload: { delivery_id: unknown.delivery_id, job_id: unknown.report_job_id } },
  ];
  const traceMap = new Map<string, string[]>();
  for (const item of cases) {
    const event = byId.get(item.event_id);
    expect(event, item.event_id).toBeTruthy();
    expect(event!.type).toBe(item.type);
    expect(event!.source).toBe(item.source);
    expect(event!.stream_id).toBe(threadId);
    expect(event!.context.context.thread_id).toBe(threadId);
    expect(event!.context.context.turn_id).toBe(item.turn_id);
    for (const [key, value] of Object.entries(item.payload)) {
      expect(event!.payload[key], `${item.event_id}.${key}`).toBe(value);
    }
    const traceId = event!.context.context.trace_id as string;
    traceMap.set(traceId, [...(traceMap.get(traceId) ?? []), item.event_id]);
  }
  for (const [traceId, ids] of traceMap) {
    const byTrace = await readAllTrace(request, `/api/traces/${traceId}`);
    const found = new Set(byTrace.map((event) => event.event_id));
    for (const id of ids) expect(found.has(id), `${traceId}:${id}`).toBe(true);
  }

  // 3. page rows, debug view, conservative UNKNOWN semantics
  await openTrajectory(page, threadId);
  const region = page.getByRole("region", { name: "统一轨迹", exact: true });
  await region.getByRole("button", { name: "调试" }).click();
  const rows: Array<[string, string]> = [
    [archive.claimed_event_id, "归档已开始"],
    [archive.committed_event_id, "历史摘要已提交"],
    [embedding.claimed_event_id, "向量任务已领取"],
    [embedding.committed_event_id, "向量已写入"],
    [learning.claimed_event_id, "学习任务已领取"],
    [learning.recovered_event_id, "已发送的学习任务已停止"],
    [sent.claimed_event_id, "通知已开始发送"],
    [sent.result_event_id, "通知已确认送达"],
    [unknown.claimed_event_id, "通知已开始发送"],
    [unknown.result_event_id, "通知结果未知"],
  ];
  for (const [eventId, title] of rows) {
    const article = region.locator(`article[data-event-id="${eventId}"]`);
    await expect(article).toBeVisible();
    await expect(article.getByRole("heading", { level: 4, name: title, exact: true })).toBeVisible();
  }
  const learningRow = region.locator(`article[data-event-id="${learning.recovered_event_id}"]`);
  await expect(learningRow).toContainText("结果未知");
  await expect(learningRow).toContainText("不能重试");
  await expect(learningRow).toContainText("不会自动再次付费");
  const unknownRow = region.locator(`article[data-event-id="${unknown.result_event_id}"]`);
  await expect(unknownRow).toContainText("结果未知");
  await expect(unknownRow).toContainText("不能重试");
  await learningRow.getByText("查看原始事件").click();
  await unknownRow.getByText("查看原始事件").click();
  await expect(learningRow.locator("pre")).toBeVisible();
  await expect(region).not.toContainText("R02_SENSITIVE_LEARNING_BODY_20261019");
  await expect(region).not.toContainText("r02-fixed-offline");

  // 4. foreign owner is refused on thread, byTrace and report tasks
  const expectedEventIds = cases.map((item) => item.event_id);
  const forbidden = ["R02_SENSITIVE_LEARNING_BODY_20261019", "r02-fixed-offline"];
  const foreign = { "x-owner-id": "other-user" };
  async function expectRefused(pathname: string) {
    const response = await request.get(pathname, { headers: foreign });
    expect(response.status(), pathname).toBe(404);
    const text = await response.text();
    for (const id of expectedEventIds) expect(text).not.toContain(id);
    for (const marker of forbidden) expect(text).not.toContain(marker);
  }
  await expectRefused(`/api/threads/${threadId}/trace`);
  for (const traceId of traceMap.keys()) await expectRefused(`/api/traces/${traceId}`);
  await expectRefused(`/api/tasks/research/${sent.report_job_id}/trace`);
  await expectRefused(`/api/tasks/research/${unknown.report_job_id}/trace`);
  const foreignContext = await browser.newContext({
    baseURL: test.info().project.use.baseURL,
    extraHTTPHeaders: foreign,
  });
  const foreignPage = await foreignContext.newPage();
  await foreignPage.goto(`/threads/${threadId}?view=activity`);
  await expect(foreignPage.getByRole("region", { name: "统一轨迹", exact: true })).toBeVisible();
  await expect(foreignPage.getByRole("alert").filter({ hasText: "404" })).toBeVisible();
  await expect(foreignPage.locator("article[data-event-id]")).toHaveCount(0);
  await foreignContext.close();

  // 5. historical thread stays partial with connected R02 sources
  const history = await threadTrace(request, seed.thread_id);
  expect(history.completeness.status).toBe("partial");
  expect((history.completeness.reasons ?? []).some((reason: { code: string }) => reason.code === "historical_unlinked")).toBe(true);
  for (const domain of ["archive", "embedding", "learning", "notification"]) {
    expect(history.sources?.connected ?? []).toContain(domain);
  }

  // 6. authorized delete removes the worker thread once
  const bootstrap = await json(await request.get("/api/bootstrap"));
  const origin = new URL(test.info().project.use.baseURL ?? page.url()).origin;
  const deleted = await request.delete(`/api/threads/${threadId}`, {
    headers: { "content-type": "application/json", "x-csrf-token": bootstrap.csrf_token, origin },
    data: "{}",
  });
  expect(deleted.status()).toBe(204);
  const gone = await request.get(`/api/threads/${threadId}/trace`);
  expect(gone.status()).toBe(404);
  const goneText = await gone.text();
  for (const id of expectedEventIds) expect(goneText).not.toContain(id);
  for (const traceId of traceMap.keys()) expect((await request.get(`/api/traces/${traceId}`)).status()).toBe(404);
  for (const jobId of [sent.report_job_id, unknown.report_job_id]) {
    expect((await request.get(`/api/tasks/research/${jobId}/trace`)).status()).toBe(404);
  }
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "404" })).toBeVisible();
  await expect(page.locator("article[data-event-id]")).toHaveCount(0);
});
