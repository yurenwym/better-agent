import { expect, test, type Page, type Route } from "@playwright/test";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

test("A17 renders one completed conversation and its concise timed trajectory", async ({ page }) => {
  const thread = {
    id: "a17-thread",
    title: "A17 完整对话",
    version: 2,
    active_turn_id: "a17-turn",
    next_event_seq: 5,
    created_at: "2026-09-06T00:00:00Z",
    updated_at: "2026-09-06T00:00:04Z",
    turns: [{
      id: "a17-turn", thread_id: "a17-thread", client_turn_id: "a17-client",
      parent_turn_id: null, status: "COMPLETED", policy: "answer",
      content_shape: "general", reason_code: "content_only", version: 2,
      skill_names: [], materialized_goal_id: null, materialized_run_id: null,
      direction_action: null, direction_idempotency_key: null,
      metrics: { queue_wait_ms: 320, context_ms: 40, model_ttft_ms: 1234, stream_ms: 2100, answer_wait_ms: 1574, total_ms: 3694, model_attempt_count: 1 },
      created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:04Z",
    }],
  };
  const messages = [
    { id: "a17-user", thread_id: thread.id, turn_id: "a17-turn", role: "user", content: "请给出一个简短回答", status: "ready", generation: 1, content_length: 9, presentation: "standard", created_at: "2026-09-06T00:00:00Z", completed_at: "2026-09-06T00:00:00Z" },
    { id: "a17-answer", thread_id: thread.id, turn_id: "a17-turn", role: "assistant", content: "PostgreSQL 中持久化的完整回答。", status: "ready", generation: 1, content_length: 21, presentation: "standard", created_at: "2026-09-06T00:00:02Z", completed_at: "2026-09-06T00:00:04Z" },
  ];
  const events = [
    { schema_version: 1, event_id: "a17-event-1", seq: 1, thread_id: thread.id, turn_id: "a17-turn", type: "turn.started", occurred_at: "2026-09-06T00:00:01Z", actor: "worker", data: { queue_wait_ms: 320 } },
    { schema_version: 1, event_id: "a17-event-2", seq: 2, thread_id: thread.id, turn_id: "a17-turn", type: "memory.retrieval.completed", occurred_at: "2026-09-06T00:00:02Z", actor: "worker", data: { retrieval_mode: "semantic", semantic_candidate_count: 1 } },
    { schema_version: 1, event_id: "a17-event-3", seq: 3, thread_id: thread.id, turn_id: "a17-turn", type: "message.completed", occurred_at: "2026-09-06T00:00:04Z", actor: "worker", data: { message_id: "a17-answer", finish_reason: "stop" } },
    { schema_version: 1, event_id: "a17-event-4", seq: 4, thread_id: thread.id, turn_id: "a17-turn", type: "turn.metrics.updated", occurred_at: "2026-09-06T00:00:04Z", actor: "worker", data: { queue_wait_ms: 320, context_ms: 40, model_ttft_ms: 1234, stream_ms: 2100, total_ms: 3694 } },
  ];

  await page.route("**/api/bootstrap", route => json(route, { csrf_token: "a17-csrf", version: "a17", api_key_env: "AGENT_MODEL_API_KEY", api_key_configured: true, human_mode: false }));
  await page.route("**/api/threads", route => json(route, { threads: [thread] }));
  await page.route("**/api/threads/a17-thread/events/stream?**", route => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.route("**/api/threads/a17-thread/events?**", route => json(route, { events }));
  await page.route("**/api/threads/a17-thread/messages", route => json(route, { messages }));
  await page.route("**/api/threads/a17-thread/expert-runs/latest", route => json(route, { detail: "not found" }, 404));
  await page.route("**/api/threads/a17-thread/plan", route => json(route, { plan: null }));
  await page.route("**/api/threads/a17-thread", route => json(route, thread));
  await page.route("**/api/research/jobs?**", route => json(route, { jobs: [] }));
  await page.route("**/api/skills", route => json(route, { skills: [] }));

  await page.goto("/threads/a17-thread");
  await expect(page.getByText("请给出一个简短回答")).toBeVisible();
  await expect(page.getByText("PostgreSQL 中持久化的完整回答。")).toBeVisible();

  await page.getByRole("tab", { name: "轨迹" }).click();
  await expect(page.getByRole("complementary", { name: "当前对话轨迹" })).toContainText("本轮对话已开始");
  await expect(page.getByRole("complementary", { name: "当前对话轨迹" })).toContainText("已完成语义记忆检索");
  await expect(page.getByRole("complementary", { name: "当前对话轨迹" })).toContainText("回答生成完成");

  await page.getByRole("button", { name: "查看完整轨迹" }).click();
  await expect(page.getByText("3 个关键节点，原始事件仍保留在调试视图")).toBeVisible();
  for (const value of ["0.32s", "0.04s", "1.23s", "2.1s"]) {
    await expect(page.getByText(value, { exact: true })).toBeVisible();
  }
});
