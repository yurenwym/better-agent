import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { renderApp } from "./renderApp";
import ConversationThread from "../components/ConversationThread";
import { navigateTo, readRoute, todayPath } from "../navigation";

afterEach(()=>{cleanup();window.history.replaceState({},"","/");});

// 页面已改为 React.lazy 懒加载：全量并行跑测试时动态 import 可能超过默认 1s 超时。
const LAZY = { timeout: 8000 };

it("places the renamed plan module before today without changing its route",()=>{
  renderApp();
  const links=within(screen.getByRole("navigation",{name:"工作区导航"})).getAllByRole("link");
  expect(links.map(link=>link.textContent)).toEqual(["对话","计划","研究"]);
  expect(links[1].getAttribute("href")).toBe("/workspace");
});

it("updates the URL when leaving a plan for conversation and restores it after remount",async ()=>{
  window.history.replaceState({},"","/plans/plan-regression");
  const view=renderApp();
  fireEvent.click(screen.getByRole("link",{name:"对话"}));
  expect(window.location.pathname).toBe("/");
  expect(await screen.findByLabelText("输入消息", undefined, LAZY)).toBeTruthy();
  view.unmount();renderApp();
  expect(await screen.findByLabelText("输入消息", undefined, LAZY)).toBeTruthy();
});

it("reads action, program, research and activity selection from URLs",()=>{
  expect(readRoute({pathname:"/today",search:"?program=p2&action=a2"})).toMatchObject({page:"today",programId:"p2",actionId:"a2"});
  expect(readRoute({pathname:"/research",search:"?job=old-report"})).toMatchObject({page:"research",jobId:"old-report"});
  expect(readRoute({pathname:"/threads/t2",search:"?view=activity"})).toMatchObject({page:"trajectory",threadId:"t2"});
  expect(todayPath("p/2","a&2")).toBe("/today?program=p%2F2&action=a%262");
});

it("restores back/forward page selection through popstate",async ()=>{
  renderApp();
  act(()=>navigateTo("/memory"));
  expect(await screen.findByRole("heading",{name:"长期记忆"}, LAZY)).toBeTruthy();
  act(()=>{window.history.replaceState({},"","/");window.dispatchEvent(new PopStateEvent("popstate"));});
  expect(await screen.findByLabelText("输入消息", undefined, LAZY)).toBeTruthy();
});

it("keeps drafts separate across conversations and component remounts",()=>{
  const props={messages:[],onSubmit:()=>false};
  const view=render(<ConversationThread {...props} draftKey="draft-a"/>);
  fireEvent.change(screen.getByLabelText("输入消息"),{target:{value:"尚未发送的输入"}});
  view.rerender(<ConversationThread {...props} draftKey="draft-b"/>);
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("");
  view.unmount();render(<ConversationThread {...props} draftKey="draft-a"/>);
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).value).toBe("尚未发送的输入");
});
