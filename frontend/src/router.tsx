import { lazy, Suspense } from "react";
import { createBrowserRouter, RouterProvider, Outlet } from "react-router-dom";
import App from "./App";

const ChatPage = lazy(() => import("./pages/ChatPage"));
const PlanningWorkspace = lazy(() => import("./pages/PlanningWorkspace"));
const TrajectoryPage = lazy(() => import("./pages/TrajectoryPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const ResearchPage = lazy(() => import("./pages/ResearchPage"));
const SchedulesPage = lazy(() => import("./pages/SchedulesPage"));
const GrowthPage = lazy(() => import("./pages/GrowthPage"));
const ModelsPage = lazy(() => import("./pages/ModelsPage"));
const UsagePage = lazy(() => import("./pages/UsagePage"));
const EvaluationPage = lazy(() => import("./pages/EvaluationPage"));
const SkillsPage = lazy(() => import("./pages/SkillsPage"));

function PageLoading() {
  return (
    <div className="workspace-page-loading" role="status" aria-live="polite">
      <span>加载中…</span>
    </div>
  );
}

function LazyPage({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageLoading />}>{children}</Suspense>;
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <LazyPage><ChatPage /></LazyPage> },
      { path: "threads/:threadId", element: <LazyPage><ChatPage /></LazyPage> },
      { path: "workspace", element: <LazyPage><PlanningWorkspace /></LazyPage> },
      { path: "workspace/:workspaceResourceId", element: <LazyPage><PlanningWorkspace /></LazyPage> },
      { path: "today", element: <LazyPage><PlanningWorkspace /></LazyPage> },
      { path: "plans", element: <LazyPage><PlanningWorkspace /></LazyPage> },
      { path: "plans/:planId", element: <LazyPage><PlanningWorkspace /></LazyPage> },
      { path: "trajectory", element: <LazyPage><TrajectoryPage /></LazyPage> },
      { path: "memory", element: <LazyPage><MemoryPage /></LazyPage> },
      { path: "research", element: <LazyPage><ResearchPage /></LazyPage> },
      { path: "schedules", element: <LazyPage><SchedulesPage /></LazyPage> },
      { path: "growth", element: <LazyPage><GrowthPage /></LazyPage> },
      { path: "models", element: <LazyPage><ModelsPage /></LazyPage> },
      { path: "usage", element: <LazyPage><UsagePage /></LazyPage> },
      { path: "evaluations", element: <LazyPage><EvaluationPage /></LazyPage> },
      { path: "evaluations/:evaluationId", element: <LazyPage><EvaluationPage /></LazyPage> },
      { path: "skills", element: <LazyPage><SkillsPage /></LazyPage> },
    ],
  },
]);

export function AppRouter() {
  return <RouterProvider router={router} />;
}
