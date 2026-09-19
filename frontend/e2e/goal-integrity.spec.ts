import { expect, test } from "@playwright/test";

for (const viewport of [{width:1440,height:1000},{width:375,height:812}]) {
  test(`30-day calendar and partial feedback at ${viewport.width}px`,async({page,request},testInfo)=>{
    await page.setViewportSize(viewport);
    await page.emulateMedia({reducedMotion:"reduce"});
    const bootstrap=await (await request.get("/api/bootstrap")).json();
    const headers={"Origin":"http://127.0.0.1:61129","Content-Type":"application/json","X-CSRF-Token":bootstrap.csrf_token};
    const plans=await (await request.get("/api/plans")).json();
    const source=plans.plans.find((plan:any)=>plan.title===`Calendar acceptance ${viewport.width}`);
    await page.goto(`/plans/${source.id}`);
    await page.getByRole("button",{name:"开始执行",exact:true}).click();
    await page.getByLabel("执行开始日期").fill("2026-09-14");
    await page.getByLabel("执行结束日期").fill("2026-10-13");
    await page.getByLabel("执行时区").fill("Asia/Shanghai");
    await page.getByRole("checkbox",{name:"周日",exact:true}).uncheck();
    await page.getByRole("button",{name:"生成预览",exact:true}).click();
    await expect(page.getByText("30 个自然日 · 26 个可用学习日 · 4 个休息日")).toBeVisible();
    const panel=page.getByRole("region",{name:"执行设置"});
    await panel.screenshot({path:testInfo.outputPath("calendar.png")});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
    await page.getByRole("button",{name:"确认并激活"}).click();
    await expect(page.getByText("已启用 · v2")).toBeVisible();
    const programs=await (await request.get("/api/programs")).json();
    const program=programs.programs.find((item:any)=>item.status==="ACTIVE"&&item.source_plan_document_id===source.id);
    expect(program.actions).toHaveLength(26);
    await page.route("**/api/today",async route=>{
      const response=await request.get("/api/today?date=2026-09-14");
      await route.fulfill({response});
    });
    await page.goto("/today");
    const action=program.actions[0];
    const card=page.getByRole("article").filter({has:page.getByRole("heading",{name:action.title,exact:true})}).first();
    await card.locator(".daily-feedback-details > summary").click();
    await card.getByLabel(`${action.title} 实际分钟`).fill("80");
    await card.getByText("记录部分进度",{exact:true}).click();
    await card.getByLabel("已完成部分",{exact:true}).fill("Jupyter 已安装");
    await card.getByLabel("待完成或待验收",{exact:true}).fill("运行 pandas 验证代码");
    await card.getByRole("button",{name:"保存部分进度"}).click();
    await expect(card.getByRole("status")).toContainText("部分完成 · 80 分钟");
    await page.reload();
    await expect(card.getByRole("status")).toContainText("部分完成 · 80 分钟");
    await card.screenshot({path:testInfo.outputPath("partial-progress.png")});
    const persisted=await (await request.get(`/api/programs/${program.id}`)).json();
    expect(persisted.progress.required_completed).toBe(0);
    expect(persisted.actions[0].progress.remaining_work).toBe("运行 pandas 验证代码");
    const cancelled=await request.post(`/api/programs/${program.id}/cancel`,{headers:{...headers,"Idempotency-Key":crypto.randomUUID()},data:{expected_version:persisted.version}});
    expect(cancelled.ok()).toBeTruthy();
  });
}
