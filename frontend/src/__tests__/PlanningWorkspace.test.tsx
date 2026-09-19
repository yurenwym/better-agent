import {cleanup,fireEvent,render,screen,within} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import PlanningWorkspace from "../pages/PlanningWorkspace";
import {readRoute} from "../navigation";
vi.mock("../api",()=>({getGoalWorkspace:async()=>({plan_document_id:"p1",plan:{title:"阅读计划"},program:{id:"g1"}}),listGoalPrograms:async()=>({programs:[{id:"g1",source_plan_document_id:"p1"}]}),getGoalAction:async()=>({program:{id:"g1"}})}));
vi.mock("../pages/TodayPage",()=>({default:({onHelp}:{onHelp:(t:string,a:string)=>void})=><button onClick={()=>onHelp("t1","a1")}>测试求助</button>}));
vi.mock("../pages/PlanPage",()=>({default:()=> <div>方案内容</div>}));
vi.mock("../pages/GoalWorkspacePage",()=>({default:()=> <div>计划列表</div>}));
vi.mock("../pages/ChatPage",()=>({default:()=> <div>任务内对话</div>}));
afterEach(cleanup);
it("defaults to today and opens help without navigating away",()=>{
 window.history.replaceState({},"","/workspace");render(<PlanningWorkspace csrfToken="" route={readRoute()}/>);
 expect(within(screen.getByRole("navigation",{name:"计划视图"})).getByRole("link",{name:"今日"}).getAttribute("aria-current")).toBe("page");
 fireEvent.click(screen.getByText("测试求助"));expect(screen.getByText("任务内对话")).toBeTruthy();
 expect(window.location.pathname).toBe("/workspace");fireEvent.click(screen.getByLabelText("关闭求助，返回任务"));expect(screen.queryByText("任务内对话")).toBeNull();
});
it("restores selected plan tab and origin from URL",async()=>{
 window.history.replaceState({},"","/workspace/p1?tab=plan&from=all");render(<PlanningWorkspace csrfToken="" route={readRoute()}/>);
 expect(await screen.findByText("方案内容")).toBeTruthy();expect(screen.getByRole("heading",{name:"阅读计划"})).toBeTruthy();
 expect(screen.getByText("返回全部计划").getAttribute("href")).toBe("/workspace?view=all");
 expect(within(screen.getByRole("navigation",{name:"计划内容"})).getByText("方案").getAttribute("aria-current")).toBe("page");
});
