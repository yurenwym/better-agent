import { expect, test } from "@playwright/test";

test("plan to conversation navigation survives reload and browser back", async({page,request})=>{
  const {plans}=await (await request.get("/api/plans")).json();
  const plan=plans[0];
  await page.goto(`/plans/${plan.id}`);
  await expect(page.locator(".plan-document-hero")).toBeVisible();
  await page.getByRole("navigation",{name:"工作区导航"}).getByRole("link",{name:"对话",exact:true}).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByLabel("输入消息")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("输入消息")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/plans/${plan.id}$`));
  await expect(page.locator(".plan-document-hero")).toBeVisible();
});

test("mobile drawer retains history and returns keyboard focus",async({page})=>{
  await page.setViewportSize({width:375,height:812});
  await page.goto("/today");
  const trigger=page.getByRole("button",{name:"打开导航"});
  await trigger.click();
  const drawer=page.getByRole("dialog",{name:"工作区导航抽屉"});
  await expect(drawer.getByRole("navigation",{name:"会话历史"})).toBeVisible();
  await expect(drawer.getByRole("link",{name:/黄金闭环测试/})).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await drawer.getByRole("link",{name:"计划",exact:true}).click();
  await expect(drawer).toHaveCount(0);
  await expect(page.getByRole("heading",{name:"全部目标"})).toBeVisible();
});

test("initial today failure is recoverable and does not masquerade as loading",async({page})=>{
  let fail=true;
  await page.route("**/api/today",route=>fail?route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({detail:"暂时不可用"})}):route.continue());
  await page.goto("/today");
  await expect(page.getByRole("heading",{name:"今日行动暂时无法加载"})).toBeVisible();
  await expect(page.getByText("正在加载今天的行动…")).toHaveCount(0);
  fail=false;
  await page.getByRole("button",{name:"重试加载"}).click();
  await expect(page.locator(".today-inbox")).toBeVisible();
});

test("workbench fits compact screens without hiding overflowing controls",async({page},testInfo)=>{
  for(const width of [320,375,414,768,1440]){
    await page.setViewportSize({width,height:812});
    await page.goto("/today");
    await expect(page.locator(".today-inbox")).toBeVisible();
    const toolbar = await page.locator(".today-page-toolbar").boundingBox();
    const switcher = await page.getByRole("group", { name: "行动视图" }).boundingBox();
    expect(Math.abs(switcher!.x - toolbar!.x)).toBeLessThan(2);
    const overflow=await page.locator("main button,main h2,main h3").evaluateAll(elements=>elements.filter(element=>element.checkVisibility()).filter(element=>{const box=element.getBoundingClientRect();return box.right>innerWidth+1||box.left< -1;}).map(element=>element.textContent));
    expect(overflow).toEqual([]);
    await page.screenshot({path:testInfo.outputPath(`today-${width}.png`)});
  }
});
