import { router } from "./router";

export type WorkspacePage = "chat" | "workspace" | "today" | "plan" | "trajectory" | "research" | "schedules" | "memory" | "growth" | "models" | "usage" | "evaluation" | "skills";

export const pagePaths: Record<WorkspacePage, string> = {
  chat: "/", workspace: "/workspace", today: "/today", plan: "/plans",
  trajectory: "/trajectory", research: "/research", schedules: "/schedules",
  memory: "/memory", growth: "/growth", models: "/models", usage: "/usage",
  evaluation: "/evaluations", skills: "/skills",
};

export interface RouteInfo {
  page: WorkspacePage;
  threadId: string | null;
  planId: string | null;
  workspaceResourceId: string | null;
  evaluationId: string | null;
  programId: string | null;
  actionId: string | null;
  jobId: string | null;
  growthView: "agent" | "personal";
}

export function readRoute(location: Pick<Location, "pathname" | "search"> = window.location): RouteInfo {
  const [root, resource] = location.pathname.split("/").filter(Boolean);
  const query = new URLSearchParams(location.search);
  const page = root === "threads" ? (query.get("view") === "activity" ? "trajectory" : "chat")
    : (Object.entries(pagePaths).find(([, path]) => path === `/${root}`)?.[0] ?? "chat") as WorkspacePage;
  return {
    page,
    threadId: root === "threads" ? resource ?? null : null,
    planId: root === "plans" ? resource ?? null : null,
    workspaceResourceId: root === "workspace" ? resource ?? null : null,
    evaluationId: root === "evaluations" ? resource ?? null : null,
    programId: query.get("program"), actionId: query.get("action"),
    jobId: query.get("job"), growthView: query.get("view") === "agent" ? "agent" as const : "personal" as const,
  };
}

export function todayPath(programId?: string | null, actionId?: string | null) {
  const query = new URLSearchParams();
  if (programId) query.set("program", programId);
  if (actionId) query.set("action", actionId);
  return `/today${query.size ? `?${query}` : ""}`;
}

// 驱动 React Router 单例导航；router 内部会同步 history 并通知订阅者（App 的 router.subscribe）。
// 保留同源校验与"目标与当前相同时不导航"的行为，与旧实现一致。
export function navigateTo(path: string, replace = false) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin) return;
  const next = url.pathname + url.search + url.hash;
  if (next === window.location.pathname + window.location.search + window.location.hash) return;
  void router.navigate(next, { replace });
}
