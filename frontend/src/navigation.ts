import { useNavigate, useLocation, useSearchParams } from "react-router-dom";

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

export function navigateTo(path: string, replace = false) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin) return;
  const next = url.pathname + url.search + url.hash;
  if (next !== window.location.pathname + window.location.search + window.location.hash) {
    window.history[replace ? "replaceState" : "pushState"]({}, "", next);
  }
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useAppNavigate() {
  const navigate = useNavigate();
  return (path: string, replace = false) => {
    navigate(path, { replace });
  };
}

export function useRouteInfo(): RouteInfo {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [root, resource] = location.pathname.split("/").filter(Boolean);
  const page = root === "threads" ? (searchParams.get("view") === "activity" ? "trajectory" : "chat")
    : (Object.entries(pagePaths).find(([, path]) => path === `/${root}`)?.[0] ?? "chat") as WorkspacePage;
  return {
    page,
    threadId: root === "threads" ? resource ?? null : null,
    planId: root === "plans" ? resource ?? null : null,
    workspaceResourceId: root === "workspace" ? resource ?? null : null,
    evaluationId: root === "evaluations" ? resource ?? null : null,
    programId: searchParams.get("program"), actionId: searchParams.get("action"),
    jobId: searchParams.get("job"), growthView: searchParams.get("view") === "agent" ? "agent" as const : "personal" as const,
  };
}
