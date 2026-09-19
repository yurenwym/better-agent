import { expect, test } from "@playwright/test";

for (const width of [375, 1100, 1440, 2326]) {
  test(`research records and report keep a coherent layout at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const created = "2026-09-12T10:00:00Z";
    const jobs = Array.from({ length: 16 }, (_, index) => ({
      id: `research-layout-${index}`, thread_id: "layout-thread", title: `PostgreSQL 索引类型与 EXPLAIN ANALYZE 事务回滚研究记录 ${index}`,
      topic: "索引研究", status: index === 0 ? "FAILED" : "COMPLETED", phase: index === 0 ? "failed" : "completed",
      source_count: 2, evidence_count: 12, attempts: 1, created_at: created, updated_at: created, failure_reason_code: "topiccoverageerror",
    }));
    await page.route("**/api/**", route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown;
      if (path === "/api/bootstrap") body = { csrf_token: "test", api_key_configured: true };
      else if (path === "/api/threads") body = { threads: [] };
      else if (path === "/api/research/jobs") body = { jobs };
      else if (path.endsWith("/report")) body = { markdown: "# 研究正文\n\n" + "一段可核验的研究结论。\n\n".repeat(80) };
      else if (path.endsWith("/sources")) body = { sources: [] };
      else {
        body = jobs.find(job => path.endsWith(`/${job.id}`));
        if (!body) return route.fulfill({ status: 404, body: "{}" });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto("/research?job=research-layout-0");
    await expect(page.getByText("这次研究没有完成")).toBeVisible();
    const measure = await page.evaluate(() => {
      const list = document.querySelector(".research-list")!;
      const title = document.querySelector(".research-list-item strong")!;
      const progress = document.querySelector(".research-detail-content > .research-progress")!;
      const overflow = [...document.querySelectorAll("main button,main strong,main summary")].filter(element => element.checkVisibility())
        .filter(element => { const box = element.getBoundingClientRect(); return box.right > innerWidth + 1 || box.left < -1; });
      return { listWidth: list.getBoundingClientRect().width, titlePadding: getComputedStyle(title).paddingRight,
        titleHeight: title.getBoundingClientRect().height, progressPosition: getComputedStyle(progress).position, overflow: overflow.length };
    });
    expect(measure.progressPosition).toBe("static");
    expect(measure.overflow).toBe(0);
    if (width > 767) {
      expect(measure.listWidth).toBeGreaterThanOrEqual(280);
      expect(measure.titlePadding).toBe("0px");
      expect(measure.titleHeight).toBeLessThanOrEqual(43);
      await expect(page.locator(".research-list-scroll")).toHaveCSS("overflow-y", "auto");
    } else {
      await expect(page.locator(".research-list")).toBeHidden();
      await page.getByRole("button", { name: "返回研究记录" }).click();
      await expect(page.locator(".research-list")).toBeVisible();
      await expect(page.getByRole("button", { name: "新研究", exact: true })).toBeVisible();
    }
    await page.goto("/research?job=research-layout-1");
    await expect(page.getByRole("heading", { name: "研究正文" })).toBeVisible();
    const alignment = await page.evaluate(() => {
      const report = document.querySelector(".research-report")!.getBoundingClientRect();
      const header = document.querySelector(".research-result-heading")!.getBoundingClientRect();
      const sources = document.querySelector(".research-source-disclosure")!.getBoundingClientRect();
      return { aligned: Math.abs(report.x - header.x) < 2 && Math.abs(sources.width - report.width) < 2, width: report.width };
    });
    expect(alignment.aligned).toBe(true);
    expect(alignment.width).toBeLessThanOrEqual(920);
    await page.screenshot({ path: testInfo.outputPath(`research-${width}.png`) });
  });
}
