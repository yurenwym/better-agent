import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App from "./App";

// catch-all：现有页面依赖 App 注入的 csrfToken/run/threadId 等 props，
// 不拆成独立 route element。路由解析仍由 App 内的 readRoute 完成。
// 模块级单例，供 navigation.ts 的 navigateTo 在非组件环境调用。
export const router = createBrowserRouter([
  { path: "*", element: <App /> },
]);

export function AppRouter() {
  return <RouterProvider router={router} />;
}
