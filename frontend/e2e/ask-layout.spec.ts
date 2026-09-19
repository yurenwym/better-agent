import { expect, test } from "@playwright/test";

for (const width of [375, 1440, 2326]) {
  test(`clarification remains in the conversation column at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 375 ? 812 : 1226 });
    const id = "layout-thread";
    const turnId = "layout-turn";
    const created = "2026-09-12T10:00:00Z";
    const questions = ["出行方式与节奏", "时间与预算", "同行人", "最想体验什么"].map((header, index) => ({
      id: `q${index}`, header, question: `${header}：你更倾向于哪种安排？`,
      multi_select: index === 3, allow_free_text: true,
      options: ["第一种安排", "第二种安排", "还没有确定"].map(label => ({ label, description: "根据实际时间和偏好安排，不必在这里决定所有细节。" })),
    }));
    const thread = { id, title: "旅行计划", version: 1, active_turn_id: turnId, next_event_seq: 2, created_at: created, updated_at: created,
      turns: [{ id: turnId, thread_id: id, status: "WAITING_ASK", version: 1, created_at: created, updated_at: created, materialized_run_id: null }] };
    await page.route("**/api/**", route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/api/bootstrap") body = { csrf_token: "layout", api_key_configured: true };
      else if (path === "/api/threads") body = { threads: [thread] };
      else if (path === `/api/threads/${id}`) body = thread;
      else if (path.endsWith("/messages")) body = { messages: [
        { id: "m1", thread_id: id, turn_id: turnId, role: "user", content: "请帮我安排一次旅行", status: "ready", created_at: created },
        { id: "m2", thread_id: id, turn_id: turnId, role: "assistant", content: "可以，请补充下面的信息。", status: "ready", created_at: created },
      ] };
      else if (path.endsWith("/events")) body = { events: [{ schema_version: 1, event_id: "e1", seq: 1, thread_id: id, turn_id: turnId, type: "ask.requested", occurred_at: created, actor: "assistant", data: { ask_id: "ask-layout", questions } }] };
      else if (path.endsWith("/stream")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
      else if (path.endsWith("/expert-runs/latest")) return route.fulfill({ status: 404, body: "{}" });
      else if (path.endsWith("/skills")) body = { skills: [] };
      else if (path.endsWith("/plan")) body = { plan: null };
      else if (path.endsWith("/jobs")) body = { jobs: [] };
      else return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto(`/threads/${id}`);
    const ask = page.getByRole("region", { name: "等待你的回答" });
    await expect(ask).toBeVisible();
    const geometry = await page.evaluate(() => {
      const rect = (selector: string) => document.querySelector(selector)!.getBoundingClientRect();
      const ask = rect(".ask-card");
      const messages = rect(".conversation-thread");
      const lastMessage = document.querySelector(".message-row:last-of-type")!.getBoundingClientRect();
      const composer = rect(".conversation-composer");
      return { sameColumn: Math.abs(ask.x - messages.x) < 2 && Math.abs(ask.width - composer.width) < 2,
        afterMessages: ask.y >= lastMessage.bottom, composerVisible: composer.bottom <= innerHeight,
        singleColumn: getComputedStyle(document.querySelector(".ask-question-list")!).gridTemplateColumns.split(" ").length === 1 };
    });
    expect(geometry).toEqual({ sameColumn: true, afterMessages: true, composerVisible: true, singleColumn: true });
    await expect(page.getByRole("button", { name: "停止询问", exact: true })).toHaveCount(1);
    for (const question of questions) await page.getByRole("button", { name: `${question.header}：第一种安排`, exact: true }).click();
    await expect(page.getByRole("button", { name: "提交回答" })).toBeEnabled();
    await page.locator(".conversation-content").evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: testInfo.outputPath(`ask-${width}.png`) });
    await page.locator(".conversation-content").evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(page.getByRole("button", { name: "提交回答" })).toBeInViewport();
  });
}
