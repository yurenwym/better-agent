import { expect, test, type Page, type Route } from "@playwright/test";

const bootstrap = {
  csrf_token: "csrf-control-plane",
  version: "e2e",
  api_key_env: "AGENT_MODEL_API_KEY",
  api_key_configured: true,
  human_mode: false,
};

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockShell(page: Page, threads: unknown[] = []) {
  await page.route("**/api/bootstrap", route => fulfillJson(route, bootstrap));
  await page.route("**/api/threads", route => fulfillJson(route, { threads }));
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect.poll(() => page.locator("html").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
}

async function expectResponsiveShell(page: Page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalOverflow(page);
  await expect(page.locator(".workspace-app")).toHaveCSS("display", "block");
  await expect(page.locator(".workspace-sidebar")).toHaveCSS("position", "relative");
  await expect.poll(async () => (await page.locator(".workspace-main").boundingBox())?.width ?? Infinity).toBeLessThanOrEqual(390);
}

async function openControlPage(page: Page, label: string, pathname: string) {
  await page.goto("/");
  await page.getByRole("button", { name: label, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${pathname.replace("/", "\\/")}$`));
}

const modelVersion = (id: string, overrides: Record<string, unknown> = {}) => ({
  id,
  profile_id: "profile-1",
  profile_name: "本地模型",
  version: 1,
  provider_protocol: "openai_compatible",
  provider_name: "Local Provider",
  base_url: "https://model.example.test/v1",
  model_name: "better-chat",
  credential_env_ref: "BETTER_TEST_KEY",
  credential_configured: true,
  capabilities: { text: true, streaming: true, tool_calling: true, json_object: true },
  context_window: 32768,
  max_output_tokens: 4096,
  timeout_seconds: 60,
  max_attempts: 2,
  config_digest: `digest-${id}`,
  status: "ACTIVE",
  verified_at: null,
  verification_status: "UNVERIFIED",
  verification_error_kind: null,
  created_at: "2026-08-28T00:00:00Z",
  ...overrides,
});

test("models recovers from loading failure and verifies a registered model", async ({ page }) => {
  await mockShell(page);
  let loadAttempts = 0;
  let verified = false;
  await page.route("**/api/model-profiles", async route => {
    if (route.request().method() !== "GET") return fulfillJson(route, {}, 405);
    loadAttempts += 1;
    if (loadAttempts === 1) return fulfillJson(route, { detail: "模型服务暂时不可用" }, 503);
    const version = modelVersion("model-version-1", verified ? {
      verification_status: "VERIFIED",
      verified_at: "2026-08-28T01:00:00Z",
    } : {});
    return fulfillJson(route, { profiles: [{ id: "profile-1", name: "本地模型", status: "ACTIVE", created_at: version.created_at, updated_at: version.created_at, versions: [version] }] });
  });
  await page.route("**/api/model-routing-policies", route => fulfillJson(route, { policies: [] }));
  await page.route("**/api/model-profile-versions/model-version-1/verify", async route => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["x-csrf-token"]).toBe(bootstrap.csrf_token);
    verified = true;
    await fulfillJson(route, { status: "VERIFIED" });
  });

  await openControlPage(page, "模型", "/models");
  await expect(page.getByRole("heading", { name: "模型控制台" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("模型服务暂时不可用");
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("Local Provider · better-chat")).toBeVisible();
  await expect(page.getByText("尚未验证")).toBeVisible();
  await page.getByRole("button", { name: "验证连接" }).click();
  await expect(page.getByText("连接已验证")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("模型连接验证成功");

  await expectResponsiveShell(page);
});

test("usage renders authoritative metrics and reports a rejected then saved budget", async ({ page }) => {
  await mockShell(page);
  const summaries: Record<string, { limit_microusd: number; reserved_microusd: number; charged_microusd: number }> = {
    INVOCATION: { limit_microusd: 100_000, reserved_microusd: 2_000, charged_microusd: 8_000 },
    DAILY: { limit_microusd: 1_000_000, reserved_microusd: 50_000, charged_microusd: 250_000 },
    MONTHLY: { limit_microusd: 20_000_000, reserved_microusd: 50_000, charged_microusd: 4_000_000 },
  };
  await page.route("**/api/cost/summary?**", route => {
    const kind = new URL(route.request().url()).searchParams.get("period_kind") ?? "DAILY";
    return fulfillJson(route, summaries[kind]);
  });
  await page.route("**/api/usage/summary", route => fulfillJson(route, { groups: [{
    role: "planner", provider: "Local Provider", profile_version_id: "model-version-1", attempts: 10, succeeded: 9,
    fallbacks: 1, cost_microusd: 123_456, unknown_cost_attempts: 0, success_rate: 0.9, fallback_rate: 0.1,
    ttft_seconds: 0.42, tps: 31.25, p95_latency_seconds: 2.7,
  }] }));
  let saves = 0;
  await page.route("**/api/cost/budgets", async route => {
    expect(route.request().method()).toBe("PUT");
    const payload = JSON.parse(route.request().postData() ?? "{}") as { period_kind: string; limit_microusd: number };
    expect(payload).toMatchObject({ period_kind: "DAILY", limit_microusd: 1_500_000 });
    saves += 1;
    if (saves === 1) return fulfillJson(route, { detail: "预算版本冲突，请重试" }, 409);
    summaries.DAILY = { ...summaries.DAILY, limit_microusd: payload.limit_microusd };
    return fulfillJson(route, summaries.DAILY);
  });

  await openControlPage(page, "用量", "/usage");
  await expect(page.getByRole("heading", { name: "用量与预算" })).toBeVisible();
  await expect(page.getByRole("table")).toContainText("planner");
  await expect(page.getByRole("table")).toContainText("90.0%");
  const daily = page.locator("form").filter({ hasText: "DAILY 上限" });
  await daily.getByRole("spinbutton").fill("1500000");
  await daily.getByRole("button", { name: "保存预算" }).click();
  await expect(page.getByRole("alert")).toContainText("预算版本冲突，请重试");
  await daily.getByRole("button", { name: "保存预算" }).click();
  await expect(page.getByRole("status")).toContainText("DAILY 预算已保存");

  await expectResponsiveShell(page);
});

test("evaluation detail exposes progress and confirms cancellation through the UI", async ({ page }) => {
  await mockShell(page);
  let status = "RUNNING";
  let cancelAttempts = 0;
  const run = () => ({
    id: "evaluation-1", suite_id: "release-60", baseline_bundle_id: "stable", candidate_bundle_id: "candidate",
    status, budget_microusd: 1_000_000, attempts: 1, created_at: "2026-08-28T00:00:00Z", updated_at: null,
    finished_at: status === "CANCELLED" ? "2026-08-28T01:00:00Z" : null,
    cancel_requested_at: status === "CANCELLED" ? "2026-08-28T01:00:00Z" : null,
  });
  await page.route("**/api/evaluation-runs/evaluation-1", route => fulfillJson(route, run()));
  await page.route("**/api/evaluation-runs/evaluation-1/events?**", route => fulfillJson(route, { events: [{
    seq: 1, type: "evaluation.case.finished", case_id: "case-01", partition: "DEV", domain: "conversation", execution_order: "baseline_first",
  }] }));
  await page.route("**/api/evaluation-runs/evaluation-1/events/stream?**", route => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.route("**/api/evaluation-runs/evaluation-1/cancel", async route => {
    expect(route.request().method()).toBe("POST");
    cancelAttempts += 1;
    if (cancelAttempts === 1) return fulfillJson(route, { detail: "评测正在结算，请稍后重试" }, 409);
    status = "CANCELLED";
    return fulfillJson(route, run());
  });
  await page.route("**/api/evaluation-suites", route => fulfillJson(route, { suites: [{ id: "release-60", digest: "suite-digest", kind: "release", case_count: 60 }] }));
  const evaluationModels = ["baseline", "candidate", "quality-judge", "safety-judge"].map((id, index) => modelVersion(id, { model_name: id, version: index + 1 }));
  await page.route("**/api/model-profiles", route => fulfillJson(route, { profiles: [{ id: "evaluation-models", name: "评测模型", status: "ACTIVE", created_at: "2026-08-28T00:00:00Z", updated_at: "2026-08-28T00:00:00Z", versions: evaluationModels }] }));
  await page.route("**/api/evaluation-runs", async route => {
    expect(route.request().method()).toBe("POST");
    const payload = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
    expect(payload).toMatchObject({ suite_id: "release-60", baseline_bundle_id: "stable", candidate_bundle_id: "candidate" });
    return fulfillJson(route, run());
  });

  await openControlPage(page, "评测", "/evaluations");
  await page.getByLabel("基线 Bundle").fill("stable");
  await page.getByLabel("候选 Bundle").fill("candidate");
  for (const [label, value] of [["基线模型", "baseline"], ["候选模型", "candidate"], ["质量 Judge", "quality-judge"], ["安全 Judge", "safety-judge"]] as const) {
    await page.getByLabel(label).selectOption(value);
  }
  await page.getByRole("button", { name: "启动真实评测" }).click();
  await expect(page).toHaveURL(/\/evaluations\/evaluation-1$/);
  await expect(page.getByText("1 / 60")).toBeVisible();
  await expect(page.getByText("等待评测完成")).toBeVisible();
  await page.getByRole("button", { name: "取消评测" }).click();
  const firstDialog = page.getByRole("dialog", { name: "取消评测？" });
  await expect(firstDialog).toBeVisible();
  await firstDialog.getByRole("button", { name: "确认取消" }).click();
  await expect(page.getByRole("alert")).toContainText("评测正在结算，请稍后重试");
  await page.getByRole("button", { name: "取消评测" }).click();
  await page.getByRole("dialog", { name: "取消评测？" }).getByRole("button", { name: "确认取消" }).click();
  await expect(page.getByText("CANCELLED")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("评测取消请求已提交");

  await expectResponsiveShell(page);
});

test("skills reveals version permissions and safely toggles a fixed version", async ({ page }) => {
  await mockShell(page);
  const skill = { skill_id: "skill-1", name: "travel", title: "旅行计划", description: "生成可执行旅行计划", enabled: true, version_id: "skill-version-1", package_digest: "package-digest" };
  let enabled = true;
  const version = () => ({
    skill_id: skill.skill_id, version_id: skill.version_id, name: skill.name, version: "1.2.0", title: skill.title,
    description: skill.description, content: "prompt", package_digest: "package-digest", manifest_digest: "manifest-digest",
    requested_tools: ["web.search"], granted_tools: ["web.search"], connectors: ["tavily"], phases: ["research"],
    grant_digest: "grant-digest", status: enabled ? "ENABLED" : "DISABLED",
  });
  await page.route("**/api/skills?include_disabled=true", route => fulfillJson(route, { skills: [skill] }));
  await page.route("**/api/trusted-connectors", route => fulfillJson(route, { connectors: [{
    connector_id: "connector-1", version_id: "connector-version-1", name: "tavily", version: 1,
    base_url: "https://api.tavily.com", methods: ["POST"], paths: ["/search"], request_schema: {}, credential_env_ref: "TAVILY_API_KEY",
    timeout_seconds: 20, max_response_bytes: 100_000, risk: "READ", config_digest: "connector-digest", status: "ACTIVE", verified_at: "2026-08-28T00:00:00Z",
  }] }));
  await page.route("**/api/skills/skill-1/versions", route => fulfillJson(route, { versions: [version()] }));
  let toggleAttempts = 0;
  await page.route("**/api/skill-versions/skill-version-1/disable", async route => {
    expect(route.request().method()).toBe("POST");
    toggleAttempts += 1;
    if (toggleAttempts === 1) return fulfillJson(route, { detail: "版本正在被新运行绑定" }, 409);
    enabled = false;
    return fulfillJson(route, version());
  });

  await openControlPage(page, "Skill", "/skills");
  await expect(page.getByRole("heading", { name: "Skill 平台" })).toBeVisible();
  await page.getByRole("button", { name: /旅行计划/ }).click();
  await expect(page.getByText("已授权工具 1 / 1")).toBeVisible();
  await expect(page.getByText("web.search")).toBeVisible();
  await expect(page.getByText("tavily · v1")).toBeVisible();
  await page.getByRole("button", { name: "停用版本" }).click();
  await expect(page.getByRole("alert")).toContainText("版本正在被新运行绑定");
  await page.getByRole("button", { name: "停用版本" }).click();
  await expect(page.getByText("DISABLED")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Skill 已停用");

  await expectResponsiveShell(page);
});

test("trajectory search and Growth approval remain operable across control navigation", async ({ page }) => {
  const thread = { id: "thread-1", title: "骑行成长计划", version: 2, active_turn_id: "turn-1", next_event_seq: 3, created_at: "2026-08-28T00:00:00Z", updated_at: "2026-08-28T01:00:00Z", turns: [{ id: "turn-1", status: "COMPLETED" }] };
  await mockShell(page, [thread]);
  await page.route("**/api/threads/thread-1", route => fulfillJson(route, thread));
  await page.route("**/api/threads/thread-1/messages", route => fulfillJson(route, { messages: [] }));
  await page.route("**/api/threads/thread-1/events?**", route => fulfillJson(route, { events: [
    { schema_version: 1, event_id: "event-1", seq: 1, thread_id: thread.id, turn_id: "turn-1", type: "ask.requested", occurred_at: "2026-08-28T00:00:01Z", actor: "runtime", data: { ask_id: "ask-1", questions: [{ id: "goal", header: "目标", question: "你的目标是什么？", options: [{ label: "建立习惯", description: "稳定训练" }], multi_select: false, allow_free_text: true }] } },
    { schema_version: 1, event_id: "event-2", seq: 2, thread_id: thread.id, turn_id: "turn-1", type: "turn.completed", occurred_at: "2026-08-28T00:00:02Z", actor: "runtime", data: {} },
  ] }));
  await page.route("**/api/threads/thread-1/events/stream?**", route => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.route("**/api/threads/thread-1/expert-runs/latest", route => fulfillJson(route, { detail: "not found" }, 404));
  await page.route("**/api/threads/thread-1/plan", route => fulfillJson(route, { plan: null }));
  await page.route("**/api/research/jobs?**", route => fulfillJson(route, { jobs: [] }));
  await page.route("**/api/skills", route => fulfillJson(route, { skills: [] }));

  let candidateStatus = "PENDING_APPROVAL";
  let candidateVersion = 3;
  const candidate = () => ({
    id: "candidate-1", kind: "prompt", title: "减少无效追问", summary: "只在关键信息缺失时询问。",
    status: candidateStatus, version: candidateVersion, risk_level: "medium", evidence_count: 8, record_origin: "observed",
    reason: "多个长期目标出现了重复追问。", proposed_content: { prompts: "research-scope-bounded" },
    evaluation: { status: "COMPLETED", deterministic_pass: true, regressions: [], metrics: { passed: 12, total: 12, baseline_correct: 8, candidate_correct: 10, quality_delta: 0.2, safety_violations: 0 } },
    permission_diff: { added: [], removed: [], unchanged: ["conversation:read"] }, canary: null,
    created_at: "2026-08-28T00:00:00Z", updated_at: "2026-08-28T00:00:00Z",
  });
  await page.route("**/api/evolution/candidates", route => fulfillJson(route, { candidates: [candidate()] }));
  await page.route("**/api/growth/profile", route => fulfillJson(route, { owner_id: "local-user", metrics: { total_programs: 1, completed_programs: 1, completed_actions: 5, skipped_actions: 0, deferred_actions: 0, average_difficulty: 3, average_actual_minutes: 40, accepted_adjustments: 1 }, programs: [] }));
  await page.route("**/api/evolution/candidates/candidate-1/approve", async route => {
    expect(route.request().method()).toBe("POST");
    expect(JSON.parse(route.request().postData() ?? "{}")).toMatchObject({ expected_version: 3, actor: "local-user" });
    candidateStatus = "APPROVED";
    candidateVersion = 4;
    return fulfillJson(route, candidate());
  });

  await page.goto("/threads/thread-1");
  await page.getByRole("button", { name: "轨迹", exact: true }).click();
  await expect(page.getByRole("region", { name: "对话时间线" })).toBeVisible();
  await expect(page.getByText("问题已准备好")).toBeVisible();
  await page.getByLabel("搜索轨迹").fill("本轮对话完成");
  await expect(page.getByText("本轮对话完成")).toBeVisible();
  await expect(page.getByText("问题已准备好")).toBeHidden();

  await page.getByRole("button", { name: "成长", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent 正在怎样变得更好" })).toBeVisible();
  await expect(page.getByText("8 条证据")).toBeVisible();
  await page.getByRole("button", { name: "批准候选" }).click();
  await expect(page.getByRole("status")).toContainText("操作已记录，候选状态已更新");
  await expect(page.getByRole("button", { name: "开始 Canary" })).toBeVisible();

  await expectResponsiveShell(page);
});
