import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const baseURL = process.env.M1_UI_URL ?? "http://127.0.0.1:8124";
assert.ok(["127.0.0.1", "localhost"].includes(new URL(baseURL).hostname));
const output = new URL("../../docs/acceptance/m1-ui-recheck/", import.meta.url);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  executablePath: process.env.BETTER_AGENT_E2E_BROWSER ?? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(5000);
  const errors = [];
  const unexpected = [];
  page.on("pageerror", error => errors.push(error.message));
  const thread = { id: "m1", title: "M1 archive check", version: 1, active_turn_id: null, turns: [], created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z" };
  let status = "DEAD_LETTER";
  let retries = 0;
  let releaseRetry;
  const retryGate = new Promise(resolve => { releaseRetry = resolve; });
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") return route.abort();
    if (url.origin !== new URL(baseURL).origin) { unexpected.push(url.origin); return route.abort(); }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const path = url.pathname;
    let body;
    if (path === "/api/bootstrap") body = { csrf_token: "mock", version: "m1", api_key_configured: false };
    else if (path === "/api/threads") body = { threads: [thread] };
    else if (path === "/api/threads/m1") body = thread;
    else if (path.endsWith("/archive/job/retry")) {
      assert.equal(route.request().method(), "POST");
      assert.equal(route.request().headers()["x-csrf-token"], "mock");
      assert.deepEqual(route.request().postDataJSON(), { expected_updated_at: "v1" });
      retries++;
      await retryGate;
      status = "QUEUED";
      body = { id: "job", status };
    } else if (path.endsWith("/archive")) body = { archived_through_seq: 0, jobs: [{ id: "job", status, end_message_seq: 5, updated_at: "v1" }] };
    else if (path.endsWith("/messages")) body = { messages: [] };
    else if (path.endsWith("/events")) body = { events: [] };
    else if (path === "/api/skills") body = { skills: [] };
    else if (path === "/api/research/jobs") body = { jobs: [] };
    else if (path.endsWith("/latest")) return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    else if (path.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: "" });
    else { unexpected.push(path); return route.abort(); }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto(baseURL);
  await page.getByText(thread.title, { exact: true }).click();
  await page.getByRole("button", { name: "重试归档" }).waitFor();
  for (const [name, width, height] of [["desktop", 1440, 1000], ["mobile", 375, 812], ["landscape", 812, 375]]) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(350);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    const boxes = await page.evaluate(() => {
      const bounds = selector => {
        const r = document.querySelector(selector).getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
      };
      return { archive: bounds(".archive-status"), mark: bounds(".conversation-empty-mark"), starters: bounds(".conversation-starters"), composer: bounds(".conversation-composer"), scrollWidth: document.documentElement.scrollWidth };
    });
    assert.ok(boxes.archive.bottom <= boxes.mark.top, `${name}: archive overlaps empty state`);
    assert.ok(boxes.starters.bottom <= boxes.composer.top, `${name}: starters overlap composer`);
    assert.ok(boxes.scrollWidth <= width, `${name}: horizontal overflow`);
    assert.ok(boxes.composer.right <= width && boxes.archive.left >= 0);
    await page.screenshot({ path: fileURLToPath(new URL(`${name}.png`, output)), fullPage: true });
    await page.locator('.conversation-composer').scrollIntoViewIfNeeded();
    await page.waitForTimeout(350);
    const send = await page.getByRole('button', { name: '发送', exact: true }).boundingBox();
    assert.ok(send && send.y >= 0 && send.y + send.height <= height, `${name}: composer send is inaccessible`);
    await page.screenshot({ path: fileURLToPath(new URL(`${name}-composer.png`, output)) });
    console.log(name, JSON.stringify(boxes));
  }
  await page.setViewportSize({ width: 375, height: 812 });
  const retry = page.getByRole("button", { name: "重试归档" });
  await retry.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "正在提交…" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "正在提交…" }).isDisabled(), true);
  await page.screenshot({ path: fileURLToPath(new URL("retry-busy.png", output)), fullPage: true });
  releaseRetry();
  await page.getByText("正在整理历史上下文", { exact: true }).waitFor();
  assert.equal(retries, 1);
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  console.log("PASS: viewport layout, keyboard retry, busy state; all APIs mocked");
} finally {
  await browser.close();
}
