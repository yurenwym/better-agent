import { useQuery } from "@tanstack/react-query";
import { getBootstrap, listThreads } from "./api";

// 服务端状态的 query key 集中在这里，避免各页面各写一份字符串导致失效不生效。
export const queryKeys = {
  bootstrap: ["bootstrap"] as const,
  threads: ["threads"] as const,
};

export function useBootstrapQuery() {
  return useQuery({ queryKey: queryKeys.bootstrap, queryFn: () => getBootstrap() });
}

export function useThreadsQuery() {
  return useQuery({ queryKey: queryKeys.threads, queryFn: () => listThreads() });
}
